"""Fetch and normalize public ranking data from official upstream sources."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

EPOCH_ECI_URL = "https://epoch.ai/data/eci_scores.csv"
EPOCH_BENCHMARKS_URL = "https://epoch.ai/data/eci_benchmarks.csv"
EPOCH_SOURCE_URL = "https://epoch.ai/eci"
LIVEBENCH_SOURCE_URL = "https://livebench.ai/"
LIVEBENCH_SCRIPT_PATTERN = re.compile(r'src=["\'](?:\./|/)?(static/js/main\.[^"\']+\.js)["\']')
LIVEBENCH_RELEASE_PATTERN = re.compile(r"20\d{6}")
LMARENA_FILTER_URL = "https://datasets-server.huggingface.co/filter"
LMARENA_ROWS_URL = "https://datasets-server.huggingface.co/rows"
LMARENA_SOURCE_URL = "https://lmarena.ai/leaderboard/text"
LMARENA_DATASET = "lmarena-ai/leaderboard-dataset"
ARTIFICIAL_ANALYSIS_MODELS_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
ARTIFICIAL_ANALYSIS_SOURCE_URL = "https://artificialanalysis.ai/leaderboards/models"
ARTIFICIAL_ANALYSIS_KEY_ENV = "ARTIFICIAL_ANALYSIS_API_KEY"
RANKING_CACHE_HOURS = 24


class RankingRefreshError(RuntimeError):
    """Raised when a source cannot produce a complete, valid snapshot."""


@dataclass
class _ModelRecord:
    id: str
    name: str
    aliases: set[str]
    scores: dict[str, dict[str, Any]]


def model_family(value: str) -> str:
    """Normalize provider and benchmark variants to a comparable model family."""
    value = value.strip().split("/")[-1]
    value = re.sub(r"\s*\((?:low|medium|high|xhigh|max)\)\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"_(?:none|low|medium|high|xhigh|max)(?:_effort)?$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"_\d+(?:\.\d+)?$", "", value)
    key = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    key = re.sub(r"^qwen-(\d)", r"qwen\1", key)
    patterns = (
        r"-(?:thinking|reasoning)(?:-auto)?(?:-\d+k)?-(?:low|medium|high|xhigh|max)-effort$",
        r"-(?:low|medium|high|xhigh|max)-effort$",
        r"-(?:thinking|reasoning)(?:-auto)?(?:-\d+k)?$",
        r"-(?:preview|latest)$",
        r"-\d{8}$",
    )
    previous = None
    while key and key != previous:
        previous = key
        for pattern in patterns:
            key = re.sub(pattern, "", key)
    return key


def _csv_rows(text: str, source: str) -> list[dict[str, str]]:
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except csv.Error as exc:
        raise RankingRefreshError(f"{source} CSV 无法解析：{exc}") from exc
    if not rows:
        raise RankingRefreshError(f"{source} 返回了空 CSV。")
    return rows


def _number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RankingRefreshError(f"{label} 包含无效分数：{value!r}") from exc
    if not number == number:  # NaN
        raise RankingRefreshError(f"{label} 包含 NaN 分数。")
    return number


def _get_text(client: httpx.Client, url: str, label: str) -> str:
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RankingRefreshError(f"{label} 拉取失败：{exc}") from exc
    return response.text


def _get_json(
    client: httpx.Client,
    url: str,
    label: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        response = client.get(url, params=params, headers=headers)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RankingRefreshError(f"{label} 拉取失败：{exc}") from exc
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RankingRefreshError(f"{label} 返回的不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise RankingRefreshError(f"{label} 返回的 JSON 结构无效。")
    return payload


def _record(records: dict[str, _ModelRecord], raw_id: str, name: str | None = None) -> _ModelRecord | None:
    key = model_family(raw_id)
    if not key:
        return None
    item = records.get(key)
    if item is None:
        item = _ModelRecord(id=key, name=(name or raw_id).strip(), aliases=set(), scores={})
        records[key] = item
    elif name and item.name == item.id:
        item.name = name.strip()
    item.aliases.update(alias for alias in {raw_id.strip(), (name or "").strip()} if alias)
    return item


def _set_score(
    item: _ModelRecord,
    metric: str,
    score: float,
    source: str,
    source_url: str,
    source_model: str,
    prefer_higher: bool = False,
) -> None:
    current = item.scores.get(metric)
    if current is not None and (not prefer_higher or current["score"] >= score):
        return
    item.scores[metric] = {
        "league": metric,
        "score": round(score, 4),
        "source": source,
        "sourceUrl": source_url,
        "sourceModel": source_model,
    }


def _load_epoch(client: httpx.Client, records: dict[str, _ModelRecord]) -> dict[str, Any]:
    eci_rows = _csv_rows(_get_text(client, EPOCH_ECI_URL, "Epoch ECI"), "Epoch ECI")
    if not {"Model", "eci"}.issubset(eci_rows[0]):
        raise RankingRefreshError("Epoch ECI CSV 缺少 Model 或 eci 字段。")
    eci_count = 0
    for row in eci_rows:
        raw_id = (row.get("Model") or "").strip()
        if not raw_id or not (row.get("eci") or "").strip():
            continue
        item = _record(records, raw_id, row.get("Display name") or raw_id)
        if item is None:
            continue
        versions = row.get("model_versions") or ""
        item.aliases.update(part.strip() for part in re.split(r"[,;|]", versions) if part.strip())
        _set_score(item, "eci", _number(row["eci"], "Epoch ECI"), "Epoch AI", EPOCH_SOURCE_URL, raw_id)
        eci_count += 1

    benchmark_rows = _csv_rows(
        _get_text(client, EPOCH_BENCHMARKS_URL, "Epoch benchmark results"),
        "Epoch benchmark results",
    )
    required = {"benchmark", "performance", "Model"}
    if not required.issubset(benchmark_rows[0]):
        raise RankingRefreshError("Epoch benchmark CSV 缺少 benchmark、performance 或 Model 字段。")
    gpqa_count = 0
    for row in benchmark_rows:
        if (row.get("benchmark") or "").strip().casefold() != "gpqa diamond":
            continue
        raw_id = (row.get("Model") or row.get("model_version") or "").strip()
        if not raw_id or not (row.get("performance") or "").strip():
            continue
        item = _record(records, raw_id, row.get("Model") or raw_id)
        if item is None:
            continue
        version = (row.get("model_version") or "").strip()
        if version:
            item.aliases.add(version)
        _set_score(
            item,
            "gpqa_diamond",
            100 * _number(row["performance"], "Epoch GPQA"),
            "Epoch AI",
            EPOCH_SOURCE_URL,
            version or raw_id,
            prefer_higher=True,
        )
        gpqa_count += 1
    if not eci_count or not gpqa_count:
        raise RankingRefreshError("Epoch 数据未产生 ECI 和 GPQA 成绩。")
    return {
        "id": "epoch",
        "name": "Epoch AI",
        "status": "ok",
        "metrics": ["eci", "gpqa_diamond"],
        "eci_rows": eci_count,
        "gpqa_rows": gpqa_count,
        "url": EPOCH_SOURCE_URL,
    }


def _try_text(client: httpx.Client, url: str, label: str) -> str | None:
    try:
        response = client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RankingRefreshError(f"{label} 拉取失败：{exc}") from exc
    return response.text


def _latest_livebench_files(client: httpx.Client) -> tuple[str, str, str]:
    homepage = _get_text(client, LIVEBENCH_SOURCE_URL, "LiveBench 官网")
    script_match = LIVEBENCH_SCRIPT_PATTERN.search(homepage)
    if script_match is None:
        raise RankingRefreshError("LiveBench 官网未暴露当前榜单脚本地址。")
    script_url = urljoin(LIVEBENCH_SOURCE_URL, script_match.group(1))
    script = _get_text(client, script_url, "LiveBench 官网脚本")
    candidates = sorted(set(LIVEBENCH_RELEASE_PATTERN.findall(script)), reverse=True)
    for date in candidates:
        base = urljoin(LIVEBENCH_SOURCE_URL, f"data/{date}/")
        categories_url = urljoin(base, "categories.json")
        table_url = urljoin(base, "table.csv")
        categories = _try_text(client, categories_url, f"LiveBench {date} 分类")
        if categories is None:
            continue
        table = _try_text(client, table_url, f"LiveBench {date} 排行榜")
        if table is not None:
            return date, table, categories
    raise RankingRefreshError("LiveBench 官网脚本中没有可下载的已发布榜单。")


def _load_livebench(client: httpx.Client, records: dict[str, _ModelRecord]) -> dict[str, Any]:
    date, table_text, categories_text = _latest_livebench_files(client)
    try:
        categories = json.loads(categories_text)
    except json.JSONDecodeError as exc:
        raise RankingRefreshError("LiveBench 分类文件不是有效 JSON。") from exc
    coding_columns = categories.get("Coding") if isinstance(categories, dict) else None
    if not isinstance(coding_columns, list) or not coding_columns or not all(isinstance(x, str) for x in coding_columns):
        raise RankingRefreshError("LiveBench 分类文件缺少 Coding 指标。")
    rows = _csv_rows(table_text, "LiveBench 排行榜")
    if "model" not in rows[0] or not set(coding_columns).issubset(rows[0]):
        raise RankingRefreshError("LiveBench CSV 缺少 model 或 Coding 列。")
    count = 0
    for row in rows:
        raw_id = (row.get("model") or "").strip()
        values = [
            _number(row[column], f"LiveBench {column}")
            for column in coding_columns
            if (row.get(column) or "").strip()
        ]
        if not raw_id or not values:
            continue
        item = _record(records, raw_id, raw_id)
        if item is None:
            continue
        _set_score(
            item,
            "livebench_coding",
            sum(values) / len(values),
            "LiveBench",
            LIVEBENCH_SOURCE_URL,
            raw_id,
            prefer_higher=True,
        )
        count += 1
    if not count:
        raise RankingRefreshError("LiveBench 数据未产生 Coding 成绩。")
    return {
        "id": "livebench",
        "name": "LiveBench",
        "status": "ok",
        "metrics": ["livebench_coding"],
        "rows": count,
        "published_at": f"{date[:4]}-{date[4:6]}-{date[6:8]}",
        "url": LIVEBENCH_SOURCE_URL,
    }


def _lmarena_rows(client: httpx.Client) -> list[dict[str, Any]]:
    common = {
        "dataset": LMARENA_DATASET,
        "config": "text_style_control",
        "split": "latest",
    }
    rows: list[dict[str, Any]] = []
    page_size = 100
    try:
        offset = 0
        for _ in range(20):
            payload = _get_json(
                client,
                LMARENA_FILTER_URL,
                "LMArena Text 数据集",
                params={
                    **common,
                    "where": '"category"=\'overall\'',
                    "orderby": '"rank"',
                    "offset": offset,
                    "length": page_size,
                },
            )
            page = payload.get("rows")
            if not isinstance(page, list):
                raise RankingRefreshError("LMArena Text 数据集缺少 rows 数组。")
            rows.extend(page)
            offset += len(page)
            total = payload.get("num_rows_total")
            if not page or (isinstance(total, int) and offset >= total):
                return rows
        raise RankingRefreshError("LMArena Text 数据行数异常，已停止分页。")
    except RankingRefreshError as filter_error:
        if _transport_failure(filter_error):
            raise

    # Dataset Viewer filter may be temporarily unavailable; the latest split is grouped by category.
    rows = []
    offset = 0
    saw_overall = False
    for _ in range(30):
        payload = _get_json(
            client,
            LMARENA_ROWS_URL,
            "LMArena Text 数据集",
            params={**common, "offset": offset, "length": page_size},
        )
        page = payload.get("rows")
        if not isinstance(page, list):
            raise RankingRefreshError("LMArena Text 数据集缺少 rows 数组。")
        overall = [
            item for item in page
            if isinstance(item, dict)
            and isinstance(item.get("row"), dict)
            and item["row"].get("category") == "overall"
        ]
        rows.extend(overall)
        saw_overall = saw_overall or bool(overall)
        offset += len(page)
        if not page or (saw_overall and not overall):
            return rows
        total = payload.get("num_rows_total")
        if isinstance(total, int) and offset >= total:
            return rows
    raise RankingRefreshError("LMArena Text 数据行数异常，已停止分页。")


def _load_lmarena(client: httpx.Client, records: dict[str, _ModelRecord]) -> dict[str, Any]:
    count = 0
    published_at = ""
    for entry in _lmarena_rows(client):
        row = entry.get("row") if isinstance(entry, dict) else None
        if not isinstance(row, dict):
            continue
        raw_id = row.get("model_name")
        rating = row.get("rating")
        if not isinstance(raw_id, str) or not raw_id.strip() or not isinstance(rating, (int, float)):
            continue
        item = _record(records, raw_id, raw_id)
        if item is None:
            continue
        _set_score(
            item,
            "arena_text",
            float(rating),
            "LMArena",
            LMARENA_SOURCE_URL,
            raw_id,
            prefer_higher=True,
        )
        date = row.get("leaderboard_publish_date")
        if isinstance(date, str) and date > published_at:
            published_at = date
        count += 1
    if not count:
        raise RankingRefreshError("LMArena Text 数据未产生有效成绩。")
    return {
        "id": "lmarena",
        "name": "LMArena",
        "status": "ok",
        "metrics": ["arena_text"],
        "rows": count,
        "published_at": published_at or None,
        "url": LMARENA_SOURCE_URL,
    }


def _load_artificial_analysis(
    client: httpx.Client,
    records: dict[str, _ModelRecord],
    api_key: str,
) -> dict[str, Any]:
    if not api_key:
        return {
            "id": "artificial_analysis",
            "name": "Artificial Analysis",
            "status": "skipped",
            "metrics": ["aa_intelligence"],
            "reason": "API Key 未配置",
            "url": ARTIFICIAL_ANALYSIS_SOURCE_URL,
        }
    count = 0
    last_updated = ""
    page = 1
    total_pages = 1
    while page <= total_pages and page <= 100:
        payload = _get_json(
            client,
            ARTIFICIAL_ANALYSIS_MODELS_URL,
            "Artificial Analysis",
            params={"page": page, "page_size": 100},
            headers={"x-api-key": api_key},
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise RankingRefreshError("Artificial Analysis 返回结果缺少 data 数组。")
        for row in data:
            if not isinstance(row, dict):
                continue
            nested_model = row.get("model")
            model = nested_model if isinstance(nested_model, dict) else row
            evaluations = row.get("evaluations")
            if not isinstance(evaluations, dict):
                continue
            raw_id = model.get("slug") or model.get("name")
            name = model.get("name") or raw_id
            score = evaluations.get("artificial_analysis_intelligence_index")
            if not isinstance(raw_id, str) or not isinstance(name, str) or not isinstance(score, (int, float)):
                continue
            item = _record(records, raw_id, name)
            if item is None:
                continue
            _set_score(
                item,
                "aa_intelligence",
                float(score),
                "Artificial Analysis",
                ARTIFICIAL_ANALYSIS_SOURCE_URL,
                raw_id,
                prefer_higher=True,
            )
            count += 1
        updated = payload.get("last_updated")
        if isinstance(updated, str) and updated > last_updated:
            last_updated = updated
        meta = payload.get("meta")
        total_pages = meta.get("total_pages", 1) if isinstance(meta, dict) else 1
        total_pages = total_pages if isinstance(total_pages, int) and total_pages > 0 else 1
        page += 1
    if not count:
        raise RankingRefreshError("Artificial Analysis 数据未产生 Intelligence Index 成绩。")
    return {
        "id": "artificial_analysis",
        "name": "Artificial Analysis",
        "status": "ok",
        "metrics": ["aa_intelligence"],
        "rows": count,
        "published_at": last_updated or None,
        "url": ARTIFICIAL_ANALYSIS_SOURCE_URL,
    }


def _snapshot(records: dict[str, _ModelRecord], sources: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    models = []
    for item in sorted(records.values(), key=lambda record: record.id):
        if not item.scores:
            continue
        models.append({
            "id": item.id,
            "name": item.name,
            "aliases": sorted(item.aliases),
            "scores": [item.scores[key] for key in sorted(item.scores)],
        })
    if not models:
        raise RankingRefreshError("上游数据没有生成任何模型排名。")
    return {"generatedAt": now, "sources": sources, "models": models}


def _atomic_write(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", dir=path.parent, delete=False)
    candidate = Path(handle.name)
    try:
        with handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        candidate.replace(path)
    except Exception:
        candidate.unlink(missing_ok=True)
        raise


def _ranking_client(proxy: str | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(45, connect=15),
        follow_redirects=True,
        trust_env=False,
        proxy=proxy,
        headers={"User-Agent": "llm-model-bench/0.2 ranking refresh"},
    )


def _transport_failure(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, httpx.TransportError):
            return True
        current = current.__cause__
    return False


def _fetch_snapshot(client: httpx.Client, artificial_analysis_api_key: str = "") -> dict[str, Any]:
    records: dict[str, _ModelRecord] = {}
    sources = [
        _load_epoch(client, records),
        _load_livebench(client, records),
        _load_lmarena(client, records),
        _load_artificial_analysis(client, records, artificial_analysis_api_key),
    ]
    return _snapshot(records, sources)


def _configured_models_hash(model_ids: list[str]) -> str:
    source = "\n".join(sorted(set(model_ids))).encode()
    return hashlib.sha256(source).hexdigest()


def _load_previous_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _records_from_snapshot(snapshot: dict[str, Any]) -> dict[str, _ModelRecord]:
    records: dict[str, _ModelRecord] = {}
    for model in snapshot.get("models", []):
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            continue
        item = _ModelRecord(
            id=model["id"],
            name=model.get("name") if isinstance(model.get("name"), str) else model["id"],
            aliases={alias for alias in model.get("aliases", []) if isinstance(alias, str)},
            scores={},
        )
        for score in model.get("scores", []):
            if isinstance(score, dict) and isinstance(score.get("league"), str):
                item.scores[score["league"]] = dict(score)
        records[item.id] = item
    return records


def _merge_records(target: dict[str, _ModelRecord], incoming: dict[str, _ModelRecord]) -> None:
    for key, source in incoming.items():
        item = target.get(key)
        if item is None:
            target[key] = source
            continue
        item.aliases.update(source.aliases)
        if source.name and source.name != source.id:
            item.name = source.name
        item.scores.update(source.scores)


def _remove_metrics(records: dict[str, _ModelRecord], metrics: list[str]) -> None:
    for item in records.values():
        for metric in metrics:
            item.scores.pop(metric, None)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _source_is_fresh(source: dict[str, Any], snapshot: dict[str, Any], now: datetime) -> bool:
    fetched_at = _timestamp(source.get("fetched_at")) or _timestamp(snapshot.get("generatedAt"))
    return fetched_at is not None and now - fetched_at < timedelta(hours=RANKING_CACHE_HOURS)


def _incremental_refresh(
    path: Path,
    configured_models: list[str],
    artificial_analysis_api_key: str,
    fallback_proxy: str | None,
    client: httpx.Client | None,
) -> dict[str, Any]:
    previous = _load_previous_snapshot(path)
    records = _records_from_snapshot(previous)
    previous_sources = {
        source["id"]: source
        for source in previous.get("sources", [])
        if isinstance(source, dict) and isinstance(source.get("id"), str)
    }
    model_hash = _configured_models_hash(configured_models)
    models_unchanged = previous.get("configuredModelsHash") == model_hash
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fetched_at = now.isoformat().replace("+00:00", "Z")
    source_specs = [
        ("epoch", ["eci", "gpqa_diamond"], lambda current, output: _load_epoch(current, output)),
        ("livebench", ["livebench_coding"], lambda current, output: _load_livebench(current, output)),
        ("lmarena", ["arena_text"], lambda current, output: _load_lmarena(current, output)),
        (
            "artificial_analysis",
            ["aa_intelligence"],
            lambda current, output: _load_artificial_analysis(current, output, artificial_analysis_api_key),
        ),
    ]
    sources: list[dict[str, Any]] = []
    modes: set[str] = set()
    direct_client = client or _ranking_client()
    proxy_client: httpx.Client | None = None
    try:
        for source_id, metrics, loader in source_specs:
            old_source = previous_sources.get(source_id, {})
            if (
                models_unchanged
                and old_source.get("status") in {"ok", "cached"}
                and _source_is_fresh(old_source, previous, now)
            ):
                cached = dict(old_source)
                cached["status"] = "cached"
                cached["fetched_via"] = "cache"
                sources.append(cached)
                modes.add("cache")
                continue

            incoming: dict[str, _ModelRecord] = {}
            mode = "provided_client" if client is not None else "direct"
            modes.add(mode)
            try:
                source = loader(direct_client, incoming)
            except RankingRefreshError as direct_error:
                if client is not None or not fallback_proxy or not _transport_failure(direct_error):
                    failed = dict(old_source) or {
                        "id": source_id,
                        "name": source_id.replace("_", " ").title(),
                        "metrics": metrics,
                    }
                    failed.update({
                        "status": "error",
                        "reason": str(direct_error),
                        "using_cache": bool(old_source),
                    })
                    sources.append(failed)
                    continue
                if proxy_client is None:
                    proxy_client = _ranking_client(fallback_proxy)
                incoming = {}
                try:
                    source = loader(proxy_client, incoming)
                    mode = "fallback_proxy"
                    modes.add(mode)
                except RankingRefreshError as proxy_error:
                    modes.add("fallback_proxy")
                    failed = dict(old_source) or {
                        "id": source_id,
                        "name": source_id.replace("_", " ").title(),
                        "metrics": metrics,
                    }
                    failed.update({
                        "status": "error",
                        "reason": str(proxy_error),
                        "using_cache": bool(old_source),
                    })
                    sources.append(failed)
                    continue

            if source.get("status") == "ok":
                _remove_metrics(records, metrics)
                _merge_records(records, incoming)
                source["fetched_at"] = fetched_at
                source["fetched_via"] = mode
            elif old_source and source.get("status") == "skipped":
                source["using_cache"] = True
            sources.append(source)
    finally:
        if client is None:
            direct_client.close()
        if proxy_client is not None:
            proxy_client.close()

    snapshot = _snapshot(records, sources)
    snapshot["configuredModelsHash"] = model_hash
    if "fallback_proxy" in modes:
        snapshot["fetchedVia"] = "fallback_proxy"
    elif modes <= {"cache"}:
        snapshot["fetchedVia"] = "cache"
    elif "cache" in modes:
        snapshot["fetchedVia"] = "mixed"
    else:
        snapshot["fetchedVia"] = "provided_client" if client is not None else "direct"
    _atomic_write(path, snapshot)
    return snapshot


def refresh_public_rankings(
    path: Path,
    client: httpx.Client | None = None,
    fallback_proxy: str | None = None,
    artificial_analysis_api_key: str = "",
    configured_models: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch every source directly, retry transport failures by proxy, then atomically replace the snapshot."""
    if configured_models is not None:
        return _incremental_refresh(
            path,
            configured_models,
            artificial_analysis_api_key,
            fallback_proxy,
            client,
        )
    if client is not None:
        snapshot = _fetch_snapshot(client, artificial_analysis_api_key)
        snapshot["fetchedVia"] = "provided_client"
        _atomic_write(path, snapshot)
        return snapshot

    direct_client = _ranking_client()
    try:
        snapshot = _fetch_snapshot(direct_client, artificial_analysis_api_key)
        snapshot["fetchedVia"] = "direct"
    except RankingRefreshError as direct_error:
        if not fallback_proxy or not _transport_failure(direct_error):
            raise
        proxy_client = _ranking_client(fallback_proxy)
        try:
            snapshot = _fetch_snapshot(proxy_client, artificial_analysis_api_key)
            snapshot["fetchedVia"] = "fallback_proxy"
        except RankingRefreshError as proxy_error:
            raise RankingRefreshError(
                f"官网直连失败，备用代理重试也失败：{proxy_error}"
            ) from proxy_error
        finally:
            proxy_client.close()
    finally:
        direct_client.close()

    _atomic_write(path, snapshot)
    return snapshot
