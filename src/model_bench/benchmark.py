"""Streamed protocol adapters and comparable latency/throughput measurements."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from statistics import median

import httpx

from .config import BenchmarkSettings, EndpointConfig, ModelConfig

UNSUPPORTED = ("not support", "unsupported", "unknown endpoint", "not found", "invalid url", "unrecognized", "不支持")


@dataclass
class Result:
    model: str
    protocol: str
    status: str
    ttft_ms: float | None = None
    total_ms: float | None = None
    tokens_per_second: float | None = None
    output_tokens: int | None = None
    token_source: str | None = None
    error: str | None = None
    samples: int = 1
    confidence: str = "limited"
    effort: str | None = None


class UnsupportedProtocol(Exception):
    pass


def _error_text(exc: Exception) -> str:
    return (str(exc).strip() or type(exc).__name__)[:300]


def _output_tokens(payload: dict, protocol: str) -> int | None:
    usage = payload.get("usage") or {}
    raw = usage.get("output_tokens" if protocol in {"responses", "messages"} else "completion_tokens")
    if not isinstance(raw, int):
        return None
    details = usage.get("output_tokens_details" if protocol in {"responses", "messages"} else "completion_tokens_details") or {}
    reasoning = details.get("thinking_tokens" if protocol == "messages" else "reasoning_tokens", 0)
    return max(0, raw - reasoning) if isinstance(reasoning, int) else raw


def _event_text(payload: dict, protocol: str) -> str:
    if protocol == "responses":
        return payload.get("delta", "") if payload.get("type") == "response.output_text.delta" else ""
    if protocol == "messages":
        delta = payload.get("delta") or {}
        return delta.get("text", "") if payload.get("type") == "content_block_delta" and delta.get("type") == "text_delta" else ""
    choices = payload.get("choices") or []
    content = (choices[0].get("delta") or {}).get("content") if choices else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


async def _stream(client: httpx.AsyncClient, endpoint: EndpointConfig, api_key: str, model: ModelConfig,
                  protocol: str, settings: BenchmarkSettings, legacy: bool = False,
                  include_usage: bool = True) -> Result:
    if protocol == "responses":
        path = "responses"
        body = {"model": model.request_model, "input": settings.prompt, "stream": True,
                "max_output_tokens": settings.max_output_tokens}
        if model.reasoning_effort:
            body["reasoning"] = {"effort": model.reasoning_effort}
        headers = {"Authorization": f"Bearer {api_key}"}
    elif protocol == "messages":
        path = "messages"
        body = {"model": model.request_model, "messages": [{"role": "user", "content": settings.prompt}],
                "stream": True, "max_tokens": settings.max_output_tokens}
        if model.reasoning_effort:
            body["output_config"] = {"effort": model.reasoning_effort}
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        path = "chat/completions"
        body = {"model": model.request_model, "messages": [{"role": "user", "content": settings.prompt}],
                "stream": True, ("max_tokens" if legacy else "max_completion_tokens"): settings.max_output_tokens}
        if model.reasoning_effort:
            body["reasoning_effort"] = model.reasoning_effort
        if include_usage:
            body["stream_options"] = {"include_usage": True}
        headers = {"Authorization": f"Bearer {api_key}"}

    started = time.perf_counter()
    first_text = None
    text = ""
    token_count = None
    async with client.stream("POST", f"{endpoint.base_url}/{path}", headers=headers, json=body) as response:
        if response.status_code >= 400:
            message = (await response.aread()).decode("utf-8", "replace")[:500]
            if response.status_code in {404, 405, 501} or (
                response.status_code == 400 and any(value in message.lower() for value in UNSUPPORTED)
            ):
                raise UnsupportedProtocol(message)
            response.raise_for_status()
        event_data: list[str] = []
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                event_data.append(line[5:].lstrip())
            elif line == "" and event_data:
                raw = "\n".join(event_data)
                event_data = []
                if raw == "[DONE]":
                    break
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if protocol == "responses" and payload.get("type") in {"error", "response.failed"}:
                    raise RuntimeError(str(payload.get("error") or payload.get("response", {}).get("error") or "stream failed")[:500])
                if protocol == "chat" and payload.get("error"):
                    raise RuntimeError(str(payload["error"])[:500])
                if protocol == "messages" and payload.get("type") == "error":
                    raise RuntimeError(str(payload.get("error") or "message stream failed")[:500])
                delta = _event_text(payload, protocol)
                if delta:
                    first_text = first_text or time.perf_counter()
                    text += delta
                count = _output_tokens(payload, protocol)
                if protocol == "messages" and payload.get("type") == "message_start":
                    count = _output_tokens(payload.get("message") or {}, protocol)
                if count is not None:
                    token_count = count
                if protocol == "responses" and payload.get("type") == "response.completed":
                    token_count = _output_tokens(payload.get("response") or {}, protocol) or token_count
    ended = time.perf_counter()
    if first_text is None:
        return Result(model.id, protocol, "no_text", total_ms=round((ended - started) * 1000, 1),
                      output_tokens=token_count, error="The stream ended without visible output text.",
                      effort=model.reasoning_effort)
    estimated = token_count is None
    token_count = token_count if token_count is not None else max(1, round(len(text) / 2))
    return Result(
        model.id, protocol, "ok", round((first_text - started) * 1000, 1),
        round((ended - started) * 1000, 1), round(token_count / max(ended - first_text, 0.001), 1),
        token_count, "estimated" if estimated else "usage",
        confidence="limited" if estimated or token_count < 150 else "single", effort=model.reasoning_effort,
    )


async def measure(client: httpx.AsyncClient, endpoint: EndpointConfig, api_key: str, model: ModelConfig,
                  settings: BenchmarkSettings) -> Result:
    try:
        return await _stream(client, endpoint, api_key, model, model.protocol, settings)
    except UnsupportedProtocol:
        if model.protocol == "chat":
            return Result(model.id, "chat", "error", error="Chat Completions is not supported.", effort=model.reasoning_effort)
        try:
            return await _stream(client, endpoint, api_key, model, "chat", settings)
        except UnsupportedProtocol as exc:
            try:
                supports_stream_usage = "stream_options" not in str(exc).lower()
                return await _stream(
                    client, endpoint, api_key, model, "chat", settings,
                    legacy=supports_stream_usage,
                    include_usage=supports_stream_usage,
                )
            except Exception as retry_exc:
                return Result(model.id, "chat", "error", error=_error_text(retry_exc), effort=model.reasoning_effort)
        except Exception as exc:
            return Result(model.id, "chat", "error", error=_error_text(exc), effort=model.reasoning_effort)
    except Exception as exc:
        return Result(model.id, model.protocol, "error", error=_error_text(exc), effort=model.reasoning_effort)


async def measure_samples(client: httpx.AsyncClient, endpoint: EndpointConfig, api_key: str, model: ModelConfig,
                          settings: BenchmarkSettings) -> Result:
    observations: list[Result] = []
    for _ in range(settings.samples):
        result = await measure(client, endpoint, api_key, model, settings)
        if result.status != "ok":
            if observations:
                observations[0].error = f"Later sample failed: {result.error or result.status}"
                return observations[0]
            return result
        observations.append(result)
    first = observations[0]
    return Result(
        model.id, first.protocol if all(item.protocol == first.protocol for item in observations) else "mixed", "ok",
        round(median(item.ttft_ms for item in observations if item.ttft_ms is not None), 1),
        round(median(item.total_ms for item in observations if item.total_ms is not None), 1),
        round(median(item.tokens_per_second for item in observations if item.tokens_per_second is not None), 1),
        round(median(item.output_tokens for item in observations if item.output_tokens is not None)),
        "usage" if all(item.token_source == "usage" for item in observations) else "estimated",
        samples=len(observations),
        confidence="moderate" if len(observations) >= 2 and all(item.confidence == "single" for item in observations) else "limited",
        effort=model.reasoning_effort,
    )
