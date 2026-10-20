"""Typed TOML configuration. Credentials are always read from environment variables."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

SUPPORTED_PROTOCOLS = {"responses", "chat", "messages"}


class ConfigError(ValueError):
    """Raised when a model-bench configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class EndpointConfig:
    id: str
    base_url: str
    api_key_env: str | None
    protocol: str = "responses"
    kind: str = "provider"


@dataclass(frozen=True)
class ModelConfig:
    id: str
    endpoint: str
    request_model: str
    display_name: str
    protocol: str
    reasoning_effort: str | None
    tags: tuple[str, ...]


@dataclass(frozen=True)
class BenchmarkSettings:
    prompt: str
    samples: int
    max_output_tokens: int
    request_timeout_seconds: float
    connect_timeout_seconds: float
    cooldown_hours: int
    top_n: int
    ranked_only: bool


@dataclass(frozen=True)
class WebSettings:
    allow_benchmark: bool
    allow_config_ui: bool
    admin_token_env: str | None


@dataclass(frozen=True)
class AppConfig:
    path: Path
    database_path: Path
    rankings_path: Path | None
    prices_path: Path | None
    endpoints: dict[str, EndpointConfig]
    models: dict[str, ModelConfig]
    benchmark: BenchmarkSettings
    web: WebSettings

    def api_key_for(self, endpoint: EndpointConfig) -> str:
        if not endpoint.api_key_env:
            return ""
        return os.getenv(endpoint.api_key_env, "").strip()

    def endpoint_for(self, model_id: str) -> EndpointConfig:
        try:
            return self.endpoints[self.models[model_id].endpoint]
        except KeyError as exc:
            raise ConfigError(f"Unknown configured model: {model_id}") from exc


DEFAULT_PROMPT = (
    "请用中文写一段约250字的连贯文字，描述秋天傍晚的城市街道。"
    "只输出正文，不要标题、列表或解释。"
)


def _require_string(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def _optional_path(root: Path, value: Any, label: str) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string path")
    return (root / value).resolve()


def _protocol(value: Any, label: str) -> str:
    protocol = value if isinstance(value, str) else "responses"
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ConfigError(f"{label} protocol must be one of: {', '.join(sorted(SUPPORTED_PROTOCOLS))}")
    return protocol


def load_config(path: str | Path | None = None) -> AppConfig:
    requested = Path(path or os.getenv("MODEL_BENCH_CONFIG", "model-bench.toml")).expanduser().resolve()
    if not requested.exists():
        raise ConfigError(
            f"Configuration not found: {requested}. Copy examples/model-bench.example.toml to model-bench.toml."
        )
    load_dotenv(requested.parent / ".env", override=False)
    try:
        raw = tomllib.loads(requested.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {requested}: {exc}") from exc

    root = requested.parent
    paths = raw.get("paths", {})
    if not isinstance(paths, dict):
        raise ConfigError("[paths] must be a table")
    database_path = _optional_path(root, paths.get("database", "data/results.sqlite3"), "paths.database")
    assert database_path is not None

    endpoints: dict[str, EndpointConfig] = {}
    for index, item in enumerate(raw.get("endpoints", [])):
        if not isinstance(item, dict):
            raise ConfigError(f"endpoints[{index}] must be a table")
        endpoint = EndpointConfig(
            id=_require_string(item, "id", f"endpoints[{index}]"),
            base_url=_require_string(item, "base_url", f"endpoints[{index}]").rstrip("/"),
            api_key_env=item.get("api_key_env") or None,
            protocol=_protocol(item.get("protocol"), f"endpoints[{index}]"),
            kind=item.get("kind", "provider"),
        )
        if endpoint.id in endpoints:
            raise ConfigError(f"Duplicate endpoint id: {endpoint.id}")
        endpoints[endpoint.id] = endpoint
    if not endpoints:
        raise ConfigError("At least one [[endpoints]] item is required")

    models: dict[str, ModelConfig] = {}
    request_keys: set[tuple[str, str]] = set()
    for index, item in enumerate(raw.get("models", [])):
        if not isinstance(item, dict):
            raise ConfigError(f"models[{index}] must be a table")
        model_id = _require_string(item, "id", f"models[{index}]")
        endpoint_id = _require_string(item, "endpoint", f"models[{index}]")
        endpoint = endpoints.get(endpoint_id)
        if endpoint is None:
            raise ConfigError(f"models[{index}] refers to unknown endpoint: {endpoint_id}")
        tags = item.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
            raise ConfigError(f"models[{index}].tags must be a list of strings")
        effort = item.get("reasoning_effort")
        if effort is not None and effort not in {"low", "medium", "high", "max"}:
            raise ConfigError(f"models[{index}].reasoning_effort must be low, medium, high, or max")
        model = ModelConfig(
            id=model_id,
            endpoint=endpoint_id,
            request_model=item.get("request_model", model_id),
            display_name=item.get("display_name", model_id),
            protocol=_protocol(item.get("protocol", endpoint.protocol), f"models[{index}]"),
            reasoning_effort=effort,
            tags=tuple(tags),
        )
        if model.id in models:
            raise ConfigError(f"Duplicate model id: {model.id}")
        request_key = (model.endpoint, model.request_model)
        if request_key in request_keys:
            raise ConfigError(f"Duplicate request_model on endpoint {model.endpoint}: {model.request_model}")
        request_keys.add(request_key)
        models[model.id] = model
    if not models:
        raise ConfigError("At least one [[models]] item is required")

    benchmark_raw = raw.get("benchmark", {})
    if not isinstance(benchmark_raw, dict):
        raise ConfigError("[benchmark] must be a table")
    samples = int(benchmark_raw.get("samples", 2))
    max_output_tokens = int(benchmark_raw.get("max_output_tokens", 512))
    if not 1 <= samples <= 10:
        raise ConfigError("benchmark.samples must be between 1 and 10")
    if not 32 <= max_output_tokens <= 8192:
        raise ConfigError("benchmark.max_output_tokens must be between 32 and 8192")
    benchmark = BenchmarkSettings(
        prompt=benchmark_raw.get("prompt", DEFAULT_PROMPT),
        samples=samples,
        max_output_tokens=max_output_tokens,
        request_timeout_seconds=float(benchmark_raw.get("request_timeout_seconds", 120)),
        connect_timeout_seconds=float(benchmark_raw.get("connect_timeout_seconds", 15)),
        cooldown_hours=max(0, int(benchmark_raw.get("cooldown_hours", 72))),
        top_n=max(1, int(benchmark_raw.get("top_n", 10))),
        ranked_only=bool(benchmark_raw.get("ranked_only", False)),
    )

    web_raw = raw.get("web", {})
    if not isinstance(web_raw, dict):
        raise ConfigError("[web] must be a table")
    web = WebSettings(
        allow_benchmark=bool(web_raw.get("allow_benchmark", False)),
        allow_config_ui=bool(web_raw.get("allow_config_ui", False)),
        admin_token_env=web_raw.get("admin_token_env") or None,
    )
    return AppConfig(
        path=requested,
        database_path=database_path,
        rankings_path=_optional_path(root, paths.get("rankings"), "paths.rankings"),
        prices_path=_optional_path(root, paths.get("prices"), "paths.prices"),
        endpoints=endpoints,
        models=models,
        benchmark=benchmark,
        web=web,
    )
