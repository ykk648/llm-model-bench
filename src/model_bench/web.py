"""Local dashboard with optional, authenticated configuration and serial benchmarking."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from .config import AppConfig, ConfigError, SUPPORTED_PROTOCOLS, load_config
from .engine import BenchmarkEngine

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class WebState:
    running: bool = False
    current_model: str = ""
    progress: str = ""
    last_error: str = ""
    task: asyncio.Task | None = None


def _token_is_valid(config: AppConfig, request: Request) -> bool:
    if not config.web.admin_token_env:
        return True
    expected = os.getenv(config.web.admin_token_env, "")
    return bool(expected) and request.headers.get("x-model-bench-token") == expected


def _toml_string(value: str) -> str:
    """JSON strings are valid TOML basic strings for the values we write."""
    return json.dumps(value, ensure_ascii=False)


def create_app(config_path: str | Path | None = None) -> FastAPI:
    config = load_config(config_path)
    engine = BenchmarkEngine(config)
    state = WebState()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine.store.connect().close()
        app.state.engine = engine
        app.state.benchmark_state = state
        try:
            yield
        finally:
            if state.task and not state.task.done():
                state.task.cancel()
                await asyncio.gather(state.task, return_exceptions=True)

    app = FastAPI(title="Model Bench", lifespan=lifespan)

    def require_config_access(request: Request) -> None:
        if not config.web.allow_config_ui:
            raise HTTPException(403, "The configuration page is disabled.")
        if not _token_is_valid(config, request):
            raise HTTPException(401, "Missing or invalid X-Model-Bench-Token.")

    def require_benchmark_access(request: Request) -> None:
        if not config.web.allow_benchmark:
            raise HTTPException(403, "Browser-triggered benchmarks are disabled. Enable them in [web].")
        if not _token_is_valid(config, request):
            raise HTTPException(401, "Missing or invalid X-Model-Bench-Token.")

    def ensure_idle(message: str) -> None:
        if state.running:
            raise HTTPException(409, message)

    def config_metadata() -> dict[str, Any]:
        return {
            "toml": config.path.read_text(),
            "endpoints": [
                {
                    "id": endpoint.id,
                    "base_url": endpoint.base_url,
                    "protocol": endpoint.protocol,
                    "kind": endpoint.kind,
                    "api_key_env": endpoint.api_key_env,
                }
                for endpoint in config.endpoints.values()
            ],
            "configured_models": [
                {
                    "id": model.id,
                    "endpoint": model.endpoint,
                    "request_model": model.request_model,
                }
                for model in config.models.values()
            ],
        }

    def replace_config(source: str) -> None:
        nonlocal config, engine
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".toml", dir=config.path.parent, delete=False
        ) as handle:
            candidate = Path(handle.name)
            handle.write(source)
        try:
            load_config(candidate)
            candidate.replace(config.path)
            config = load_config(config.path)
            engine = BenchmarkEngine(config)
            app.state.engine = engine
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            if candidate.exists():
                candidate.unlink()

    async def run_many(model_ids: list[str]) -> None:
        errors: list[str] = []
        total = len(model_ids)
        try:
            for index, model_id in enumerate(model_ids, start=1):
                state.current_model = model_id
                state.progress = f"{index}/{total} · {model_id}"
                try:
                    result = await engine.benchmark_one(model_id, force=True)
                    if result and result.status != "ok":
                        errors.append(f"{model_id}: {result.error or result.status}")
                except Exception as exc:  # Keep a batch moving after one endpoint/model failure.
                    errors.append(f"{model_id}: {str(exc)[:180]}")
            state.progress = f"完成 {total}/{total} 个模型"
            state.last_error = "；".join(errors)[:1000]
        except asyncio.CancelledError:
            state.progress = "测速已取消"
            raise
        finally:
            state.running = False
            state.current_model = ""

    def start_benchmark(model_ids: list[str]) -> None:
        ensure_idle(f"Benchmark already running: {state.current_model}")
        state.running = True
        state.current_model = model_ids[0]
        state.progress = f"准备测速 0/{len(model_ids)}"
        state.last_error = ""
        state.task = asyncio.create_task(run_many(model_ids))

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/settings")
    def settings_page():
        return FileResponse(STATIC_DIR / "settings.html")

    @app.get("/static/{file_name}")
    def static(file_name: str):
        if file_name not in {"style.css", "app.js", "settings.js"}:
            raise HTTPException(404)
        return FileResponse(STATIC_DIR / file_name)

    @app.get("/api/status")
    def status():
        rankings = engine.rankings.snapshot
        prices = engine._prices()
        return {
            "configured": True,
            "running": state.running,
            "current_model": state.current_model,
            "progress": state.progress,
            "last_error": state.last_error,
            "web_benchmark_enabled": config.web.allow_benchmark,
            "web_config_enabled": config.web.allow_config_ui,
            "web_benchmark_token_required": bool(config.web.admin_token_env),
            "top_n": config.benchmark.top_n,
            "ranked_only": config.benchmark.ranked_only,
            "interval_hours": config.benchmark.cooldown_hours,
            "max_tokens": config.benchmark.max_output_tokens,
            "samples": config.benchmark.samples,
            "snapshot_at": rankings.get("capturedAt") or rankings.get("generatedAt"),
            "price_snapshot_at": prices.get("captured_at"),
            "model_count": len(config.models),
            "endpoint_label": f"{len(config.endpoints)} configured endpoint(s)",
        }

    @app.get("/api/leaderboard")
    def leaderboard():
        return engine.leaderboard()

    @app.post("/api/benchmark")
    async def benchmark_many(request: Request):
        require_benchmark_access(request)
        body = await request.json()
        requested = body.get("models") if isinstance(body, dict) else None
        if not isinstance(requested, list) or not requested or not all(isinstance(item, str) for item in requested):
            raise HTTPException(422, "models must be a non-empty array of configured model IDs.")
        model_ids = list(dict.fromkeys(requested))
        unknown = [model_id for model_id in model_ids if model_id not in config.models]
        if unknown:
            raise HTTPException(404, f"Unknown configured model(s): {', '.join(unknown)}")
        start_benchmark(model_ids)
        return {"accepted": True, "models": model_ids, "serial": True}

    @app.post("/api/benchmark/{model_id:path}")
    async def benchmark(model_id: str, request: Request):
        require_benchmark_access(request)
        if model_id not in config.models:
            raise HTTPException(404, "Unknown configured model.")
        start_benchmark([model_id])
        return {"accepted": True, "model": model_id, "serial": True}

    @app.get("/api/config")
    def get_config(request: Request):
        require_config_access(request)
        return config_metadata()

    @app.put("/api/config")
    async def put_config(request: Request):
        require_config_access(request)
        ensure_idle("A benchmark is running; configuration cannot be replaced yet.")
        body = await request.json()
        source = body.get("toml") if isinstance(body, dict) else None
        if not isinstance(source, str) or not source.strip():
            raise HTTPException(422, "A non-empty TOML document is required.")
        replace_config(source)
        return {"saved": True, "model_count": len(config.models), "endpoint_count": len(config.endpoints)}

    @app.post("/api/endpoints")
    async def add_endpoint(request: Request):
        require_config_access(request)
        ensure_idle("A benchmark is running; configuration cannot be changed yet.")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(422, "A JSON endpoint definition is required.")
        endpoint_id = body.get("id")
        base_url = body.get("base_url")
        protocol = body.get("protocol", "responses")
        kind = body.get("kind", "provider")
        api_key_env = body.get("api_key_env") or None
        if not isinstance(endpoint_id, str) or not endpoint_id.strip():
            raise HTTPException(422, "Endpoint id is required.")
        if endpoint_id in config.endpoints:
            raise HTTPException(409, "An endpoint with this id already exists.")
        if not isinstance(base_url, str) or not base_url.strip():
            raise HTTPException(422, "Endpoint URL is required.")
        if protocol not in SUPPORTED_PROTOCOLS:
            raise HTTPException(422, f"Protocol must be one of: {', '.join(sorted(SUPPORTED_PROTOCOLS))}.")
        if not isinstance(kind, str) or not kind.strip():
            raise HTTPException(422, "Endpoint kind is required.")
        if api_key_env is not None and (not isinstance(api_key_env, str) or not api_key_env.strip()):
            raise HTTPException(422, "API key environment variable must be a non-empty name.")
        block = "\n[[endpoints]]\n" + "\n".join(
            [
                f"id = {_toml_string(endpoint_id.strip())}",
                f"base_url = {_toml_string(base_url.strip().rstrip('/'))}",
                f"protocol = {_toml_string(protocol)}",
                f"kind = {_toml_string(kind.strip())}",
            ] + ([f"api_key_env = {_toml_string(api_key_env.strip())}"] if api_key_env else [])
        ) + "\n"
        replace_config(config.path.read_text().rstrip() + block)
        return {"saved": True, "endpoint": endpoint_id.strip(), "endpoint_count": len(config.endpoints)}

    @app.post("/api/discover/{endpoint_id}")
    async def discover(endpoint_id: str, request: Request):
        require_config_access(request)
        ensure_idle("A benchmark is running; wait before refreshing models.")
        if endpoint_id not in config.endpoints:
            raise HTTPException(404, "Unknown configured endpoint.")
        try:
            discovered = await engine.discover(endpoint_id)
        except Exception as exc:
            raise HTTPException(502, str(exc)[:500]) from exc
        existing = [model.request_model for model in config.models.values() if model.endpoint == endpoint_id]
        return {"endpoint": endpoint_id, "models": discovered, "configured_models": existing}

    @app.post("/api/discover/{endpoint_id}/import")
    async def import_discovered(endpoint_id: str, request: Request):
        require_config_access(request)
        ensure_idle("A benchmark is running; configuration cannot be changed yet.")
        endpoint = config.endpoints.get(endpoint_id)
        if endpoint is None:
            raise HTTPException(404, "Unknown configured endpoint.")
        body = await request.json()
        requested = body.get("models") if isinstance(body, dict) else None
        if not isinstance(requested, list) or not requested or not all(isinstance(item, str) and item.strip() for item in requested):
            raise HTTPException(422, "models must be a non-empty array of model IDs.")
        requested = list(dict.fromkeys(item.strip() for item in requested))
        existing_ids = set(config.models)
        existing_requests = {model.request_model for model in config.models.values() if model.endpoint == endpoint_id}
        skipped = [model_id for model_id in requested if model_id in existing_ids or model_id in existing_requests]
        imports = [model_id for model_id in requested if model_id not in skipped]
        if not imports:
            return {"imported": [], "skipped": skipped, "model_count": len(config.models)}
        blocks = []
        for model_id in imports:
            blocks.append(
                "[[models]]\n"
                f"id = {_toml_string(model_id)}\n"
                f"endpoint = {_toml_string(endpoint_id)}\n"
                f"request_model = {_toml_string(model_id)}\n"
                f"display_name = {_toml_string(model_id)}\n"
                f"protocol = {_toml_string(endpoint.protocol)}\n"
                "tags = [\"text\"]\n"
            )
        replace_config(config.path.read_text().rstrip() + "\n\n" + "\n".join(blocks))
        return {"imported": imports, "skipped": skipped, "model_count": len(config.models)}

    return app


def _configuration_error_app(exc: ConfigError) -> FastAPI:
    app = FastAPI(title="Model Bench")

    @app.get("/")
    @app.get("/api/status")
    def unavailable():
        raise HTTPException(503, str(exc))

    return app


try:
    app = create_app()
except ConfigError as error:
    app = _configuration_error_app(error)
