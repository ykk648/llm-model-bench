import asyncio
import json
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from model_bench.benchmark import measure, measure_samples
from model_bench.config import ConfigError, load_config
from model_bench.engine import BenchmarkEngine
from model_bench.ranking import Rankings
from model_bench.web import create_app


def write_config(tmp_path: Path, extra: str = "") -> Path:
    config = tmp_path / "model-bench.toml"
    config.write_text("""
[paths]
database = "results.sqlite3"
rankings = "rankings.json"
prices = "prices.json"

[benchmark]
samples = 2
max_output_tokens = 256
cooldown_hours = 72
top_n = 10

[web]
allow_benchmark = false

[[endpoints]]
id = "gateway"
base_url = "https://example.test/v1"
api_key_env = "TEST_GATEWAY_KEY"
protocol = "responses"
kind = "provider"

[[models]]
id = "Public-Name"
endpoint = "gateway"
request_model = "actual-server-id"
display_name = "Display Name"
protocol = "responses"
reasoning_effort = "high"
tags = ["text", "coding"]
""" + extra)
    (tmp_path / "rankings.json").write_text(json.dumps({"models": [{
        "id": "vendor/actual-server-id", "name": "Actual Server", "scores": [
            {"league": "eci", "score": 150, "source": "source", "sourceUrl": "https://example.test"},
            {"league": "livebench_coding", "score": 70, "source": "source", "sourceUrl": "https://example.test"},
        ],
    }]}))
    (tmp_path / "prices.json").write_text(json.dumps({"captured_at": "2026-01-01", "models": {"Public-Name": {"input": 1, "output": 2}}}))
    return config


def test_config_separates_display_and_request_model(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret")
    config = load_config(write_config(tmp_path))
    model = config.models["Public-Name"]
    assert model.display_name == "Display Name"
    assert model.request_model == "actual-server-id"
    assert config.api_key_for(config.endpoint_for(model.id)) == "secret"


def test_config_rejects_unknown_endpoint(tmp_path: Path):
    path = tmp_path / "model-bench.toml"
    path.write_text("""
[[endpoints]]
id = "one"
base_url = "https://example.test/v1"
[[models]]
id = "bad"
endpoint = "missing"
""")
    try:
        load_config(path)
    except ConfigError as exc:
        assert "unknown endpoint" in str(exc)
    else:
        raise AssertionError("expected ConfigError")


def test_responses_stream_sends_mapped_model_and_effort(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret")
    config = load_config(write_config(tmp_path))
    model = config.models["Public-Name"]
    endpoint = config.endpoint_for(model.id)
    seen = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, text=(
            'data: {"type":"response.output_text.delta","delta":"你好"}\n\n'
            'data: {"type":"response.completed","response":{"usage":{"output_tokens":200}}}\n\n'
            "data: [DONE]\n\n"
        ))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await measure(client, endpoint, "secret", model, config.benchmark)

    result = asyncio.run(run())
    assert result.status == "ok"
    assert seen[0]["model"] == "actual-server-id"
    assert seen[0]["reasoning"] == {"effort": "high"}


def test_fallback_only_retries_unsupported_protocol(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret")
    config = load_config(write_config(tmp_path))
    model = config.models["Public-Name"]
    endpoint = config.endpoint_for(model.id)
    paths = []

    def handler(request: httpx.Request):
        paths.append(request.url.path)
        if request.url.path.endswith("/responses"):
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, text=(
            'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
            'data: {"choices":[],"usage":{"completion_tokens":200}}\n\n'
            "data: [DONE]\n\n"
        ))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await measure(client, endpoint, "secret", model, config.benchmark)

    result = asyncio.run(run())
    assert result.protocol == "chat" and result.status == "ok"
    assert paths == ["/v1/responses", "/v1/chat/completions"]


def test_samples_are_serial_and_median_is_saved(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret")
    config = load_config(write_config(tmp_path))
    model = config.models["Public-Name"]
    endpoint = config.endpoint_for(model.id)
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        return httpx.Response(200, text=(
            'data: {"type":"response.output_text.delta","delta":"足够长的文本"}\n\n'
            'data: {"type":"response.completed","response":{"usage":{"output_tokens":200}}}\n\n'
            "data: [DONE]\n\n"
        ))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await measure_samples(client, endpoint, "secret", model, config.benchmark)

    result = asyncio.run(run())
    assert calls == ["/v1/responses", "/v1/responses"]
    assert result.samples == 2 and result.confidence == "moderate"


def test_leaderboard_uses_local_optional_snapshots(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret")
    engine = BenchmarkEngine(load_config(write_config(tmp_path)))
    row = engine.leaderboard()[0]
    assert row["id"] == "Public-Name"
    assert row["name"] == "Actual Server"
    assert row["price"] == {"input": 1, "output": 2}


def test_same_request_model_on_different_endpoints_keeps_both_rows(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret")
    config = load_config(write_config(tmp_path, """
[[endpoints]]
id = "second-gateway"
base_url = "https://second.example.test/v1"
protocol = "responses"
kind = "self-hosted"

[[models]]
id = "Self-Hosted-Alias"
endpoint = "second-gateway"
request_model = "actual-server-id"
display_name = "Self Hosted Alias"
protocol = "responses"
tags = ["text"]
"""))
    rows = BenchmarkEngine(config).leaderboard()
    assert {row["id"] for row in rows} == {"Public-Name", "Self-Hosted-Alias"}
    assert len(rows) == 2


def test_configuration_ui_reloads_toml_without_exposing_environment_secret(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret-value")
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace("allow_benchmark = false", "allow_benchmark = false\nallow_config_ui = true"))
    with TestClient(create_app(path)) as client:
        read = client.get("/api/config")
        assert read.status_code == 200
        assert "secret-value" not in read.json()["toml"]
        saved = client.put("/api/config", json={"toml": read.json()["toml"]})
        assert saved.status_code == 200
        assert saved.json()["model_count"] == 1


def test_configuration_ui_imports_selected_discovered_models(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret-value")
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace("allow_benchmark = false", "allow_benchmark = false\nallow_config_ui = true"))
    with TestClient(create_app(path)) as client:
        metadata = client.get("/api/config")
        assert metadata.status_code == 200
        assert metadata.json()["endpoints"][0]["id"] == "gateway"
        imported = client.post("/api/discover/gateway/import", json={"models": ["new-text-model"]})
        assert imported.status_code == 200
        assert imported.json()["imported"] == ["new-text-model"]
        config = load_config(path)
        assert config.models["new-text-model"].endpoint == "gateway"
        assert config.models["new-text-model"].request_model == "new-text-model"


def test_browser_batch_benchmark_stays_disabled_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret")
    with TestClient(create_app(write_config(tmp_path))) as client:
        response = client.post("/api/benchmark", json={"models": ["Public-Name"]})
        assert response.status_code == 403


def test_browser_batch_benchmark_runs_models_serially(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret")
    path = write_config(tmp_path, """
[[models]]
id = "Second-Model"
endpoint = "gateway"
request_model = "second-server-id"
display_name = "Second Model"
protocol = "responses"
tags = ["text"]
""")
    path.write_text(path.read_text().replace("allow_benchmark = false", "allow_benchmark = true"))
    with TestClient(create_app(path)) as client:
        calls = []
        active = 0
        max_active = 0

        async def fake_benchmark(model_id, force=True):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            calls.append(model_id)
            await asyncio.sleep(0.01)
            active -= 1
            return None

        client.app.state.engine.benchmark_one = fake_benchmark
        response = client.post("/api/benchmark", json={"models": ["Public-Name", "Second-Model"]})
        assert response.status_code == 200
        for _ in range(50):
            if not client.get("/api/status").json()["running"]:
                break
            time.sleep(0.01)
        assert calls == ["Public-Name", "Second-Model"]
        assert max_active == 1


def test_unambiguous_tail_matching_and_minimum_scores(tmp_path: Path):
    path = tmp_path / "ranking.json"
    path.write_text(json.dumps({"models": [
        {"id": "one/model", "name": "Model", "scores": [
            {"league": "eci", "score": 150, "source": "x", "sourceUrl": "https://x"},
            {"league": "livebench_coding", "score": 70, "source": "x", "sourceUrl": "https://x"},
        ]},
        {"id": "two/only-one", "name": "Only One", "scores": [
            {"league": "eci", "score": 155, "source": "x", "sourceUrl": "https://x"},
        ]},
    ]}))
    rows = Rankings(path).rank(["model", "only-one", "unknown"])
    assert rows[0]["id"] == "model" and rows[0]["rank"] == 1
    assert all(row["score"] is None for row in rows[1:])
