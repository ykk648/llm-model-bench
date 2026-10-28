import asyncio
import json
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

import model_bench.ranking_sources as ranking_sources
from model_bench.benchmark import Result, measure, measure_samples
from model_bench.config import ConfigError, load_config
from model_bench.engine import BenchmarkEngine
from model_bench.ranking import Rankings
from model_bench.ranking_sources import RankingRefreshError, model_family, refresh_public_rankings
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
            {"league": "livebench_overall", "score": 70, "source": "source", "sourceUrl": "https://example.test"},
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
    assert config.network.benchmark_use_proxy is False
    assert config.network.fallback_proxy is None


def test_benchmark_proxy_policy_is_explicit_and_ignores_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy.invalid:9999")
    path = write_config(tmp_path)
    clients = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            clients.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def fake_measure(_client, _endpoint, _key, model, _settings):
        return Result(model.id, model.protocol, "ok")

    monkeypatch.setattr("model_bench.engine.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("model_bench.engine.measure_samples", fake_measure)

    asyncio.run(BenchmarkEngine(load_config(path)).benchmark_one("Public-Name"))
    assert clients[-1]["trust_env"] is False
    assert clients[-1]["proxy"] is None

    path.write_text(path.read_text().replace(
        "[web]",
        '[network]\nbenchmark_use_proxy = true\nfallback_proxy = "http://127.0.0.1:7897"\n\n[web]',
    ))
    asyncio.run(BenchmarkEngine(load_config(path)).benchmark_one("Public-Name"))
    assert clients[-1]["trust_env"] is False
    assert clients[-1]["proxy"] == "http://127.0.0.1:7897"


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


def test_messages_stream_uses_anthropic_endpoint_and_headers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret")
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace('protocol = "responses"', 'protocol = "messages"'))
    config = load_config(path)
    model = config.models["Public-Name"]
    endpoint = config.endpoint_for(model.id)
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, text=(
            'data: {"type":"message_start","message":{"usage":{"output_tokens":0}}}\n\n'
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"你好"}}\n\n'
            'data: {"type":"message_delta","usage":{"output_tokens":200}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        ))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await measure(client, endpoint, "secret", model, config.benchmark)

    result = asyncio.run(run())
    assert result.status == "ok"
    assert seen[0].url.path == "/v1/messages"
    assert seen[0].headers["x-api-key"] == "secret"
    assert seen[0].headers["anthropic-version"] == "2023-06-01"
    assert json.loads(seen[0].content)["model"] == "actual-server-id"


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


def test_empty_transport_error_reports_exception_type(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret")
    config = load_config(write_config(tmp_path))
    model = config.models["Public-Name"]
    endpoint = config.endpoint_for(model.id)

    def handler(request: httpx.Request):
        raise httpx.ConnectTimeout("", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await measure(client, endpoint, "secret", model, config.benchmark)

    result = asyncio.run(run())
    assert result.status == "error"
    assert result.error == "ConnectTimeout"


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


def test_leaderboard_api_selects_one_public_source(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret")
    with TestClient(create_app(write_config(tmp_path))) as client:
        epoch = client.get("/api/leaderboard").json()[0]
        livebench = client.get("/api/leaderboard?source=livebench").json()[0]
        invalid = client.get("/api/leaderboard?source=unknown")

    assert epoch["ranking_source"] == "epoch"
    assert epoch["metrics"][0]["metric"] == "eci"
    assert livebench["ranking_source"] == "livebench"
    assert livebench["metrics"][0]["metric"] == "livebench_overall"
    assert invalid.status_code == 422


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


def test_configuration_ui_refreshes_ranking_snapshot(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret-value")
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace("allow_benchmark = false", "allow_benchmark = false\nallow_config_ui = true"))
    with TestClient(create_app(path)) as client:
        assert client.get("/api/leaderboard").json()[0]["name"] == "Actual Server"

        def fake_refresh():
            rankings_path = tmp_path / "rankings.json"
            snapshot = json.loads(rankings_path.read_text())
            snapshot["generatedAt"] = "2026-10-28T01:00:00Z"
            snapshot["sources"] = [{"name": "Epoch AI", "status": "ok"}]
            snapshot["models"][0]["name"] = "Updated Server"
            rankings_path.write_text(json.dumps(snapshot))
            client.app.state.engine.reload_rankings()
            return snapshot

        client.app.state.engine.refresh_rankings = fake_refresh

        refreshed = client.post("/api/rankings/refresh")
        assert refreshed.status_code == 200
        assert refreshed.json()["ranked_count"] == 1
        assert refreshed.json()["sources"][0]["name"] == "Epoch AI"
        assert client.get("/api/leaderboard").json()[0]["name"] == "Updated Server"


def test_public_ranking_refresh_fetches_and_atomically_combines_sources(tmp_path: Path):
    path = tmp_path / "rankings.json"
    path.write_text(json.dumps({"models": [{
        "id": "claude-opus-4-6",
        "name": "Claude Opus 4.6",
        "scores": [{"league": "livebench_coding", "score": 78}],
    }]}))

    def handler(request: httpx.Request):
        url = str(request.url)
        if url.endswith("/eci_scores.csv"):
            return httpx.Response(200, text=(
                "Model,Display name,eci,model_versions\n"
                "Claude Opus 4.6,Claude Opus 4.6,160.5,claude-opus-4-6\n"
            ))
        if url.endswith("/eci_benchmarks.csv"):
            return httpx.Response(200, text=(
                "benchmark,performance,Model,model_version\n"
                "GPQA diamond,0.82,Claude Opus 4.6,claude-opus-4-6\n"
            ))
        if url == "https://livebench.ai/":
            return httpx.Response(200, text='<script src="./static/js/main.test.js"></script>')
        if url.endswith("/main.test.js"):
            return httpx.Response(200, text='const RELEASES=["20261014","20261025"];')
        if url.endswith("/data/20261025/categories.json"):
            return httpx.Response(200, json={
                "Long Agentic": ["tool_use"],
                "Reasoning": ["reasoning"],
                "_overall_weights": {"Long Agentic": 2},
            })
        if url.endswith("/data/20261025/table.csv"):
            return httpx.Response(200, text=(
                "model,tool_use,reasoning\n"
                "claude-opus-4-6-thinking-auto-high-effort,80,70\n"
            ))
        if request.url.host == "datasets-server.huggingface.co":
            return httpx.Response(200, json={
                "rows": [{"row": {
                    "model_name": "Claude Opus 4.6 (High)",
                    "rating": 1480.5,
                    "rank": 2,
                    "category": "overall",
                    "leaderboard_publish_date": "2026-10-22",
                }}],
                "num_rows_total": 1,
            })
        if "/data/20261014/" in url:
            return httpx.Response(404)
        raise AssertionError(f"unexpected URL: {url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshot = refresh_public_rankings(path, client)

    assert snapshot["sources"][1]["published_at"] == "2026-10-25"
    model = snapshot["models"][0]
    assert model["id"] == "claude-opus-4-6"
    assert {score["league"] for score in model["scores"]} == {
        "eci", "gpqa_diamond", "livebench_overall", "arena_text",
    }
    livebench = next(score for score in model["scores"] if score["league"] == "livebench_overall")
    assert livebench["score"] == 76.6667
    assert all(score["league"] != "livebench_coding" for score in model["scores"])
    assert snapshot["sources"][2]["name"] == "LMArena"
    assert snapshot["sources"][3]["status"] == "skipped"
    assert Rankings(path).match("claude-opus-4-6_0.4")["id"] == "claude-opus-4-6"
    assert model_family("openai/gpt_oss") == "gpt-oss"
    assert model_family("qwen3.8-max_xhigh") == "qwen3-8-max"
    assert model_family("Qwen 3.8 Max") == "qwen3-8-max"


def _ranking_success_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/eci_scores.csv"):
        return httpx.Response(200, text="Model,Display name,eci\nModel A,Model A,150\n")
    if url.endswith("/eci_benchmarks.csv"):
        return httpx.Response(200, text="benchmark,performance,Model\nGPQA diamond,0.8,Model A\n")
    if url == "https://livebench.ai/":
        return httpx.Response(200, text='<script src="./static/js/main.test.js"></script>')
    if url.endswith("/main.test.js"):
        return httpx.Response(200, text='const RELEASES=["20261025"];')
    if url.endswith("/data/20261025/categories.json"):
        return httpx.Response(200, json={
            "Long Agentic": ["agentic"],
            "Reasoning": ["reasoning"],
            "_overall_weights": {"Long Agentic": 2},
        })
    if url.endswith("/data/20261025/table.csv"):
        return httpx.Response(200, text="model,agentic,reasoning\nmodel-a,75,60\n")
    if request.url.host == "datasets-server.huggingface.co":
        return httpx.Response(200, json={
            "rows": [{"row": {
                "model_name": "Model A (High)",
                "rating": 1400.0,
                "rank": 1,
                "category": "overall",
                "leaderboard_publish_date": "2026-10-22",
            }}],
            "num_rows_total": 1,
        })
    raise AssertionError(f"unexpected URL: {url}")


def test_artificial_analysis_key_fetches_intelligence_index(tmp_path: Path):
    def handler(request: httpx.Request):
        if request.url.host == "artificialanalysis.ai":
            assert request.headers["x-api-key"] == "aa-secret"
            return httpx.Response(200, json={
                "status": 200,
                "last_updated": "2026-10-27T00:00:00Z",
                "data": [{
                    "model": {"name": "Model A", "slug": "model-a"},
                    "evaluations": {"artificial_analysis_intelligence_index": 72.4},
                }],
                "meta": {"total_pages": 1, "current_page": 1},
            })
        return _ranking_success_handler(request)

    path = tmp_path / "rankings.json"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshot = refresh_public_rankings(path, client, artificial_analysis_api_key="aa-secret")

    model = next(item for item in snapshot["models"] if item["id"] == "model-a")
    assert next(score for score in model["scores"] if score["league"] == "aa_intelligence")["score"] == 72.4
    assert snapshot["sources"][3]["status"] == "ok"


def test_artificial_analysis_accepts_current_flattened_model_shape(tmp_path: Path):
    def handler(request: httpx.Request):
        if request.url.host == "artificialanalysis.ai":
            return httpx.Response(200, json={
                "status": 200,
                "data": [{
                    "name": "Model A",
                    "slug": "model-a",
                    "evaluations": {"artificial_analysis_intelligence_index": 74.2},
                }],
            })
        return _ranking_success_handler(request)

    path = tmp_path / "rankings.json"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshot = refresh_public_rankings(path, client, artificial_analysis_api_key="aa-secret")

    model = next(item for item in snapshot["models"] if item["id"] == "model-a")
    score = next(item for item in model["scores"] if item["league"] == "aa_intelligence")
    assert score["score"] == 74.2


def test_incremental_ranking_refresh_reuses_successes_and_retries_skipped_source(tmp_path: Path):
    path = tmp_path / "rankings.json"
    with httpx.Client(transport=httpx.MockTransport(_ranking_success_handler)) as client:
        first = refresh_public_rankings(path, client, configured_models=["model-a"])
    assert [source["status"] for source in first["sources"]] == ["ok", "ok", "ok", "skipped"]

    requested_hosts = []

    def artificial_analysis_only(request: httpx.Request):
        requested_hosts.append(request.url.host)
        assert request.url.host == "artificialanalysis.ai"
        return httpx.Response(200, json={
            "status": 200,
            "data": [{
                "name": "Model A",
                "slug": "model-a",
                "evaluations": {"artificial_analysis_intelligence_index": 76.5},
            }],
        })

    with httpx.Client(transport=httpx.MockTransport(artificial_analysis_only)) as client:
        second = refresh_public_rankings(
            path,
            client,
            artificial_analysis_api_key="aa-secret",
            configured_models=["model-a"],
        )

    assert requested_hosts == ["artificialanalysis.ai"]
    assert [source["status"] for source in second["sources"]] == ["cached", "cached", "cached", "ok"]
    assert second["fetchedVia"] == "mixed"

    def no_requests_expected(request: httpx.Request):
        raise AssertionError(f"unexpected refresh request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(no_requests_expected)) as client:
        third = refresh_public_rankings(
            path,
            client,
            artificial_analysis_api_key="aa-secret",
            configured_models=["model-a"],
        )
    assert all(source["status"] == "cached" for source in third["sources"])
    assert third["fetchedVia"] == "cache"


def test_ranking_refresh_direct_success_does_not_open_proxy(tmp_path: Path, monkeypatch):
    calls = []

    def client_factory(proxy=None):
        calls.append(proxy)
        return httpx.Client(transport=httpx.MockTransport(_ranking_success_handler))

    monkeypatch.setattr(ranking_sources, "_ranking_client", client_factory)
    snapshot = refresh_public_rankings(tmp_path / "rankings.json", fallback_proxy="http://127.0.0.1:7897")

    assert calls == [None]
    assert snapshot["fetchedVia"] == "direct"


def test_ranking_refresh_retries_transport_failure_with_fallback_proxy(tmp_path: Path, monkeypatch):
    calls = []

    def client_factory(proxy=None):
        calls.append(proxy)
        if proxy is None:
            def fail(request: httpx.Request):
                raise httpx.ConnectError("direct connection failed", request=request)
            return httpx.Client(transport=httpx.MockTransport(fail))
        return httpx.Client(transport=httpx.MockTransport(_ranking_success_handler))

    monkeypatch.setattr(ranking_sources, "_ranking_client", client_factory)
    snapshot = refresh_public_rankings(tmp_path / "rankings.json", fallback_proxy="http://127.0.0.1:7897")

    assert calls == [None, "http://127.0.0.1:7897"]
    assert snapshot["fetchedVia"] == "fallback_proxy"


def test_ranking_refresh_parse_failure_does_not_retry_proxy(tmp_path: Path, monkeypatch):
    calls = []

    def client_factory(proxy=None):
        calls.append(proxy)

        def invalid(request: httpx.Request):
            if str(request.url).endswith("/eci_scores.csv"):
                return httpx.Response(200, text="wrong,columns\na,b\n")
            raise AssertionError("parse failure should stop before another request")

        return httpx.Client(transport=httpx.MockTransport(invalid))

    monkeypatch.setattr(ranking_sources, "_ranking_client", client_factory)
    try:
        refresh_public_rankings(tmp_path / "rankings.json", fallback_proxy="http://127.0.0.1:7897")
    except RankingRefreshError:
        pass
    else:
        raise AssertionError("expected ranking parse failure")

    assert calls == [None]


def test_public_ranking_refresh_preserves_previous_snapshot_on_failure(tmp_path: Path):
    path = tmp_path / "rankings.json"
    previous = {"generatedAt": "old", "models": [{"id": "old", "scores": []}]}
    path.write_text(json.dumps(previous))

    def handler(request: httpx.Request):
        if str(request.url).endswith("/eci_scores.csv"):
            return httpx.Response(200, text="Model,eci\nModel A,150\n")
        if str(request.url).endswith("/eci_benchmarks.csv"):
            return httpx.Response(200, text="benchmark,performance,Model\nGPQA diamond,0.8,Model A\n")
        return httpx.Response(503, text="unavailable")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        try:
            refresh_public_rankings(path, client)
        except RankingRefreshError:
            pass
        else:
            raise AssertionError("expected ranking refresh failure")

    assert json.loads(path.read_text()) == previous


def test_configuration_ui_saves_new_endpoint_key_without_exposing_it(tmp_path: Path, monkeypatch):
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace("allow_benchmark = false", "allow_benchmark = false\nallow_config_ui = true"))
    key_env = "MODEL_BENCH_PRIVATE_GATEWAY_KEY"
    monkeypatch.setenv(key_env, "")
    with TestClient(create_app(path)) as client:
        created = client.post("/api/endpoints", json={
            "id": "private-gateway",
            "base_url": "https://private.example.test/v1",
            "protocol": "chat",
            "kind": "provider",
            "api_key": "private-secret-value",
        })
        assert created.status_code == 200
        assert created.json()["api_key_configured"] is True
        assert "private-secret-value" not in created.text

        metadata = client.get("/api/config")
        endpoint = next(item for item in metadata.json()["endpoints"] if item["id"] == "private-gateway")
        assert endpoint["api_key_env"] == key_env
        assert endpoint["api_key_configured"] is True
        assert "private-secret-value" not in metadata.text

        async def fake_discover(endpoint_id):
            endpoint_config = client.app.state.engine.config.endpoints[endpoint_id]
            assert client.app.state.engine.config.api_key_for(endpoint_config) == "private-secret-value"
            return ["model-a", "model-b"]

        client.app.state.engine.discover = fake_discover
        discovered = client.post("/api/discover/private-gateway")
        assert discovered.json()["models"] == ["model-a", "model-b"]
        imported = client.post("/api/discover/private-gateway/import", json={"models": discovered.json()["models"]})
        assert imported.json()["imported"] == ["model-a", "model-b"]

    assert f"api_key_env = \"{key_env}\"" in path.read_text()
    assert "private-secret-value" not in path.read_text()
    assert "private-secret-value" in (tmp_path / ".env").read_text()
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_configuration_ui_imports_claude_models_with_messages_protocol(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret-value")
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace(
        "allow_benchmark = false",
        "allow_benchmark = false\nallow_config_ui = true",
    ))

    with TestClient(create_app(path)) as client:
        imported = client.post("/api/discover/gateway/import", json={
            "models": ["claude-opus-test", "anthropic/claude-sonnet-test", "gpt-test"],
        })
        assert imported.status_code == 200

    config = load_config(path)
    assert config.models["claude-opus-test"].protocol == "messages"
    assert config.models["anthropic/claude-sonnet-test"].protocol == "messages"
    assert config.models["gpt-test"].protocol == "responses"


def test_configuration_ui_round_trips_network_settings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret-value")
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace(
        "allow_benchmark = false",
        "allow_benchmark = false\nallow_config_ui = true",
    ))

    with TestClient(create_app(path)) as client:
        saved = client.put("/api/network", json={
            "benchmark_use_proxy": True,
            "fallback_proxy": "http://127.0.0.1:7897/",
        })
        assert saved.status_code == 200
        assert saved.json()["benchmark_use_proxy"] is True
        metadata = client.get("/api/config").json()
        assert metadata["network"] == {
            "benchmark_use_proxy": True,
            "fallback_proxy": "http://127.0.0.1:7897",
        }
        assert len(metadata["endpoints"]) == 1
        assert len(metadata["configured_models"]) == 1

    config = load_config(path)
    assert config.network.benchmark_use_proxy is True
    assert config.network.fallback_proxy == "http://127.0.0.1:7897"


def test_configuration_ui_saves_artificial_analysis_key_without_exposing_it(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "secret-value")
    monkeypatch.delenv("ARTIFICIAL_ANALYSIS_API_KEY", raising=False)
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace(
        "allow_benchmark = false",
        "allow_benchmark = false\nallow_config_ui = true",
    ))

    with TestClient(create_app(path)) as client:
        before = client.get("/api/config").json()
        source = next(item for item in before["ranking_sources"] if item["id"] == "artificial_analysis")
        assert source["api_key_configured"] is False

        saved = client.put("/api/ranking-sources/artificial-analysis", json={"api_key": "aa-secret-value"})
        assert saved.status_code == 200
        assert "aa-secret-value" not in saved.text

        after = client.get("/api/config")
        source = next(item for item in after.json()["ranking_sources"] if item["id"] == "artificial_analysis")
        assert source["api_key_configured"] is True
        assert "aa-secret-value" not in after.text
        assert "aa-secret-value" not in after.json()["toml"]

    dotenv = (tmp_path / ".env").read_text()
    assert "ARTIFICIAL_ANALYSIS_API_KEY" in dotenv
    assert "aa-secret-value" in dotenv


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


def test_unambiguous_tail_matching_and_selectable_source(tmp_path: Path):
    path = tmp_path / "ranking.json"
    path.write_text(json.dumps({"models": [
        {"id": "one/model", "name": "Model", "scores": [
            {"league": "eci", "score": 150, "source": "x", "sourceUrl": "https://x"},
            {"league": "livebench_overall", "score": 70, "source": "x", "sourceUrl": "https://x"},
        ]},
        {"id": "two/only-one", "name": "Only One", "scores": [
            {"league": "eci", "score": 155, "source": "x", "sourceUrl": "https://x"},
        ]},
    ]}))
    epoch_rows = Rankings(path).rank(["model", "only-one", "unknown"])
    assert [row["id"] for row in epoch_rows] == ["only-one", "model", "unknown"]
    assert [row["rank"] for row in epoch_rows] == [1, 2, None]

    livebench_rows = Rankings(path).rank(["model", "only-one", "unknown"], "livebench")
    assert livebench_rows[0]["id"] == "model" and livebench_rows[0]["rank"] == 1
    assert all(row["score"] is None for row in livebench_rows[1:])
