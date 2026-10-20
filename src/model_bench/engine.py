"""Serial orchestration, optional catalog discovery, and display-oriented data assembly."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .benchmark import Result, measure_samples
from .config import AppConfig, ConfigError
from .ranking import Rankings
from .storage import ResultStore


class BenchmarkEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        self.store = ResultStore(config.database_path)
        self.rankings = Rankings(config.rankings_path)
        self._lock = asyncio.Lock()

    def _prices(self) -> dict:
        path = self.config.prices_path
        if not path or not path.exists():
            return {"models": {}}
        return json.loads(path.read_text())

    def leaderboard(self) -> list[dict]:
        by_request = {
            row["id"]: row
            for row in self.rankings.rank(list(dict.fromkeys(model.request_model for model in self.config.models.values())))
        }
        results = self.store.latest_by_model()
        prices = self._prices().get("models", {})
        rows = []
        for model in self.config.models.values():
            row = copy.deepcopy(by_request[model.request_model])
            endpoint = self.config.endpoint_for(model.id)
            row["id"] = model.id
            row["name"] = model.display_name if row["name"] == model.request_model else row["name"]
            row["speed"] = results.get(model.id)
            row["price"] = prices.get(model.id)
            row["custom"] = endpoint.kind == "self-hosted"
            row["endpoint_type"] = endpoint.kind
            row["tags"] = list(model.tags)
            rows.append(row)
        rows.sort(key=lambda row: (row["score"] is None, -(row["score"] or 0), row["id"].lower()))
        for index, row in enumerate((row for row in rows if row["score"] is not None), start=1):
            row["rank"] = index
        return rows

    def is_due(self, model_id: str, force: bool = False) -> bool:
        if force:
            return True
        previous = self.store.latest(model_id)
        if previous is None:
            return True
        tested_at = datetime.fromisoformat(previous["tested_at"])
        return datetime.now(timezone.utc) - tested_at >= timedelta(hours=self.config.benchmark.cooldown_hours)

    async def benchmark_one(self, model_id: str, force: bool = True) -> Result | None:
        if model_id not in self.config.models:
            raise ConfigError(f"Unknown configured model: {model_id}")
        if not force and not self.is_due(model_id):
            return None
        model = self.config.models[model_id]
        endpoint = self.config.endpoint_for(model_id)
        api_key = self.config.api_key_for(endpoint)
        if endpoint.api_key_env and not api_key:
            raise ConfigError(f"Set {endpoint.api_key_env} before benchmarking endpoint {endpoint.id}")
        async with self._lock:
            timeout = httpx.Timeout(self.config.benchmark.request_timeout_seconds,
                                    connect=self.config.benchmark.connect_timeout_seconds)
            async with httpx.AsyncClient(timeout=timeout, trust_env=True) as client:
                result = await measure_samples(client, endpoint, api_key, model, self.config.benchmark)
            self.store.save(result)
            return result

    async def benchmark_many(self, model_ids: list[str], force: bool = False) -> list[Result]:
        results = []
        for model_id in model_ids:
            result = await self.benchmark_one(model_id, force=force)
            if result is not None:
                results.append(result)
        return results

    def default_selection(self) -> list[str]:
        rows = self.leaderboard()
        if self.config.benchmark.ranked_only:
            rows = [row for row in rows if row["rank"] is not None]
        return [row["id"] for row in rows[:self.config.benchmark.top_n]]

    def select(self, model_ids: list[str] | None = None, tags: list[str] | None = None) -> list[str]:
        selected = model_ids or self.default_selection()
        unknown = set(selected) - set(self.config.models)
        if unknown:
            raise ConfigError(f"Unknown configured model(s): {', '.join(sorted(unknown))}")
        if tags:
            wanted = set(tags)
            selected = [model_id for model_id in selected if wanted.intersection(self.config.models[model_id].tags)]
        return selected

    async def discover(self, endpoint_id: str) -> list[str]:
        endpoint = self.config.endpoints.get(endpoint_id)
        if endpoint is None:
            raise ConfigError(f"Unknown endpoint: {endpoint_id}")
        key = self.config.api_key_for(endpoint)
        if endpoint.api_key_env and not key:
            raise ConfigError(f"Set {endpoint.api_key_env} before discovery")
        async with httpx.AsyncClient(timeout=20, trust_env=True) as client:
            response = await client.get(f"{endpoint.base_url}/models", headers={"Authorization": f"Bearer {key}"})
            response.raise_for_status()
            data = response.json().get("data")
        if not isinstance(data, list):
            raise ConfigError("The /models response did not contain a data array")
        return sorted(item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str))
