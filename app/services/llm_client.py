"""Claude tool calls with strict, content-addressed record/replay fixtures."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

PROMPTS = {
    "extract_invoice": "extraction.v1.md",
    "explain_findings": "explanation.v1.md",
}
DEFAULT_MODEL = "claude-sonnet-4-6"


class CacheMissError(RuntimeError):
    """No cached model response exists for the exact request."""


class LLMResponseError(RuntimeError):
    """The model did not produce the required structured tool response."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class LLMClient:
    def __init__(
        self,
        mode: Literal["live", "record", "replay"] = "replay",
        cache_dir: Path | str = "fixtures/llm_cache",
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
    ):
        if mode not in {"live", "record", "replay"}:
            raise ValueError("LLM mode must be live, record or replay")
        self.mode = mode
        self.cache_dir = Path(cache_dir)
        self.model = model
        self.api_key = api_key
        self._sdk = None

    def request(self, task: str, payload: dict, schema: dict | type[BaseModel]) -> dict:
        if task not in PROMPTS:
            raise ValueError(f"Unknown LLM task: {task}")
        prompt = (Path(__file__).parent / "prompts" / PROMPTS[task]).read_text(encoding="utf-8")
        return {
            "task": task,
            "prompt_file": PROMPTS[task],
            "prompt": prompt,
            "model": self.model,
            "payload": payload,
            "schema": schema.model_json_schema() if isinstance(schema, type) else schema,
        }

    @staticmethod
    def request_hash(request: dict) -> str:
        return hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()

    def cache_response(
        self,
        task: str,
        payload: dict,
        schema: dict | type[BaseModel],
        response: dict,
        *,
        source: Literal["synthetic", "recorded"] = "synthetic",
    ) -> Path:
        """Fixture authoring helper; complete(replay) never creates fixture files."""
        request = self.request(task, payload, schema)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{self.request_hash(request)}.json"
        if source == "synthetic" and path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("source") == "recorded":
                return path
        entry = {"source": source, "request": request, "response": response}
        path.write_text(
            json.dumps(entry, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        return path

    def complete(self, task: str, payload: dict, schema: dict | type[BaseModel]) -> dict:
        request = self.request(task, payload, schema)
        request_hash = self.request_hash(request)
        if self.mode == "replay":
            path = self.cache_dir / f"{request_hash}.json"
            if not path.is_file():
                raise CacheMissError(f"Replay cache miss for {task}: {request_hash}")
            entry = json.loads(path.read_text(encoding="utf-8"))
            if entry.get("request") != request:
                raise LLMResponseError(f"Cache request mismatch: {path.name}")
            if not isinstance(entry.get("response"), dict):
                raise LLMResponseError(f"Cache response must be an object: {path.name}")
            return entry["response"]
        if self._sdk is None:
            from anthropic import Anthropic

            api_key = self.api_key or os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY is required in live/record mode")
            self._sdk = Anthropic(api_key=api_key, timeout=60.0, max_retries=2)
        message = self._sdk.messages.create(
            model=self.model,
            max_tokens=8192,
            system=request["prompt"],
            tools=[
                {
                    "name": "submit_result",
                    "description": "Return only the requested structured result",
                    "input_schema": request["schema"],
                }
            ],
            tool_choice={"type": "tool", "name": "submit_result"},
            messages=[{"role": "user", "content": canonical_json(payload)}],
        )
        results = [
            block.input
            for block in message.content
            if block.type == "tool_use" and block.name == "submit_result"
        ]
        if len(results) != 1 or not isinstance(results[0], dict):
            raise LLMResponseError("Claude must return one submit_result tool call")
        result = results[0]
        if self.mode == "record":
            self.cache_response(task, payload, schema, result, source="recorded")
        return result
