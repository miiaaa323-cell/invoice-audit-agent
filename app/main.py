"""FastAPI entry point with one durable graph/checkpointer per application."""

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from anthropic import APIError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver

from app.api.review_page import router as review_router
from app.api.runs import router as runs_router
from app.config import ROOT, Settings, load_policy
from app.graph.builder import build_graph
from app.repo.repositories import Repository, ReservationConflict, RunConflict
from app.services.audit import AuditService
from app.services.explanation import ExplanationService
from app.services.extraction import ExtractionService
from app.services.llm_client import CacheMissError, LLMClient, LLMResponseError
from app.services.review import ReviewError


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        repo = Repository(settings.db_path)
        if settings.seed_on_start:
            repo.seed(settings.seed_dir)
        policy = load_policy(settings.policy_path)
        client = LLMClient(
            mode=settings.llm_mode,
            cache_dir=settings.cache_dir,
            model=settings.llm_model,
            api_key=settings.anthropic_api_key,
        )
        connection = sqlite3.connect(str(settings.checkpoint_path), check_same_thread=False)
        saver = SqliteSaver(connection)
        saver.setup()
        graph = build_graph(
            saver, repo, ExtractionService(client), ExplanationService(client), policy
        )
        app.state.settings = settings
        app.state.repo = repo
        app.state.audit = AuditService(graph, repo, settings, policy)
        try:
            yield
        finally:
            connection.close()
            repo.close()

    app = FastAPI(title="采购发票审核", version="0.1.0", lifespan=lifespan)
    app.include_router(runs_router)
    app.include_router(review_router)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "api/static"), name="static")
    app.mount("/sample-invoices", StaticFiles(directory=ROOT / "data/invoices"), name="samples")

    @app.exception_handler(ReviewError)
    async def review_error(request: Request, exc: ReviewError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(RunConflict)
    @app.exception_handler(ReservationConflict)
    async def conflict_error(request: Request, exc: Exception):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def input_error(request: Request, exc: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(CacheMissError)
    async def cache_error(request: Request, exc: CacheMissError):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "此发票没有离线演示记录。请上传示例发票，或配置 live/record 模式。",
                "code": "LLM_CACHE_MISS",
            },
        )

    @app.exception_handler(LLMResponseError)
    @app.exception_handler(APIError)
    async def model_error(request: Request, exc: LLMResponseError):
        return JSONResponse(
            status_code=502, content={"detail": "字段识别服务未返回有效结果，请稍后重试。"}
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/review")

    return app


app = create_app()
