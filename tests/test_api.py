from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app
from app.schemas import Invoice
from data.generate import write_invoice_pdf


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as value:
        yield value


def upload(client, number):
    path = ROOT / "data/invoices" / f"invoice_{number:04d}.pdf"
    return client.post("/runs", files={"file": (path.name, path.read_bytes(), "application/pdf")})


def test_health_review_static_and_samples(client):
    assert client.get("/health").json() == {"status": "ok"}
    response = client.get("/review")
    assert response.status_code == 200
    assert "采购发票审核" in response.text
    assert client.get("/static/review.js").status_code == 200
    assert client.get("/sample-invoices/invoice_0046.pdf").content.startswith(b"%PDF-")


def test_upload_query_list_and_audit(client):
    response = upload(client, 46)
    assert response.status_code == 201
    result = response.json()
    assert result["status"] == "APPROVED"
    run_id = result["run_id"]
    assert client.get(f"/runs/{run_id}").json() == result
    assert client.get("/runs?status=APPROVED").json()[0]["run_id"] == run_id
    assert client.get("/runs?status=WAITING_INFO").json() == []
    assert client.get(f"/runs/{run_id}/audit").json()


@pytest.mark.parametrize(
    "body,role,status",
    [
        ({"action": "approve"}, "finance_manager", 400),
        ({"action": "request_info", "note": "补料"}, "ap_clerk", 400),
        ({"action": "reject"}, "ap_clerk", 400),
        ({"action": "approve"}, "unknown", 400),
        ({"action": "approve", "extra": True}, "ap_clerk", 422),
        ({"action": "approve", "version": 999}, "ap_clerk", 409),
    ],
)
def test_invalid_resume_has_correct_http_status_and_changes_nothing(client, body, role, status):
    original = upload(client, 21).json()
    path = f"/runs/{original['run_id']}"
    audit = client.get(path + "/audit").json()
    response = client.post(path + "/resume", json=body, headers={"X-User-Role": role})
    assert response.status_code == status
    assert client.get(path).json() == original
    assert client.get(path + "/audit").json() == audit


def test_terminal_resume_and_unknown_run(client):
    result = upload(client, 46).json()
    assert (
        client.post(f"/runs/{result['run_id']}/resume", json={"action": "approve"}).status_code
        == 409
    )
    assert client.get("/runs/no-such-run").status_code == 404
    assert client.get("/runs/no-such-run/audit").status_code == 404
    assert client.post("/runs/no-such-run/resume", json={"action": "approve"}).status_code == 404


def test_invalid_pdf_is_rejected_before_registering(client):
    response = client.post("/runs", files={"file": ("bad.pdf", b"not a PDF", "application/pdf")})
    assert response.status_code == 422
    assert client.get("/runs").json() == []


def test_upload_size_limit(client):
    settings = client.app.state.settings
    settings.max_upload_bytes = 10
    assert upload(client, 46).status_code == 413
    assert client.get("/runs").json() == []


def test_document_requires_waiting_info_and_correct_role(client):
    result = upload(client, 161).json()
    run_id = result["run_id"]
    path = ROOT / "data/invoices/invoice_0046.pdf"
    content = path.read_bytes()
    original_uploads = set(Path(client.app.state.settings.upload_dir).glob("*"))
    response = client.post(f"/runs/{run_id}/documents", files={"file": (path.name, content)})
    assert response.status_code == 409
    assert set(Path(client.app.state.settings.upload_dir).glob("*")) == original_uploads
    response = client.post(
        f"/runs/{run_id}/resume",
        json={
            "action": "request_info",
            "note": "请补交正确数量的发票",
        },
    )
    assert response.json()["status"] == "WAITING_INFO"
    before = client.get(f"/runs/{run_id}").json()
    response = client.post(
        f"/runs/{run_id}/documents",
        files={"file": (path.name, content)},
        headers={"X-User-Role": "finance_manager"},
    )
    assert response.status_code == 400
    assert client.get(f"/runs/{run_id}").json() == before
    response = client.post(f"/runs/{run_id}/documents", files={"file": (path.name, content)})
    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"


def test_application_restart_can_resume_both_approval_levels(settings):
    with TestClient(create_app(settings)) as first:
        result = upload(first, 21).json()
    run_id = result["run_id"]
    with TestClient(create_app(settings)) as second:
        assert second.get(f"/runs/{run_id}").json()["pending"]["type"] == "level1_approval"
        response = second.post(f"/runs/{run_id}/resume", json={"action": "approve"})
        assert response.json()["pending"]["type"] == "level2_approval"
    with TestClient(create_app(settings)) as third:
        response = third.post(
            f"/runs/{run_id}/resume",
            json={"action": "approve"},
            headers={"X-User-Role": "finance_manager"},
        )
        assert response.json()["status"] == "APPROVED"


def test_unknown_offline_invoice_returns_actionable_error(client, tmp_path):
    import json

    truth = json.loads((ROOT / "data/labels.json").read_text())[45]["truth"]
    invoice = Invoice.model_validate(truth).model_copy(update={"invoice_number": "uncached"})
    pdf = write_invoice_pdf(tmp_path / "new.pdf", invoice)
    response = client.post("/runs", files={"file": ("new.pdf", pdf.read_bytes())})
    assert response.status_code == 422
    assert response.json()["code"] == "LLM_CACHE_MISS"


def test_provider_failure_returns_actionable_error_without_booking(client, monkeypatch):
    import httpx
    from anthropic import APIConnectionError

    from app.services.extraction import ExtractionService

    def unavailable(self, path):
        raise APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )

    monkeypatch.setattr(ExtractionService, "extract", unavailable)
    response = upload(client, 46)
    assert response.status_code == 502
    assert "识别服务" in response.json()["detail"]
    with client.app.state.repo.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ap_ledger").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM payment_queue").fetchone()[0] == 0
