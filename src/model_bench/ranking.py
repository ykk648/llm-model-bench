"""Match configured models against one selected public leaderboard."""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from pathlib import Path

from .ranking_sources import model_family

RANKING_SOURCES = {
    "epoch": {"metric": "eci", "label": "Epoch AI", "metric_label": "Epoch ECI"},
    "livebench": {
        "metric": "livebench_overall",
        "label": "LiveBench",
        "metric_label": "LiveBench 综合能力",
    },
    "lmarena": {"metric": "arena_text", "label": "LMArena", "metric_label": "LMArena Text"},
    "artificial_analysis": {
        "metric": "aa_intelligence",
        "label": "Artificial Analysis",
        "metric_label": "Artificial Analysis Intelligence Index",
    },
}
DEFAULT_RANKING_SOURCE = "epoch"


def canonical(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


class Rankings:
    def __init__(self, path: Path | None):
        self.snapshot = {"models": []}
        if path and path.exists():
            self.snapshot = json.loads(path.read_text())
        self.models = self.snapshot.get("models", [])
        self.by_id = {canonical(model["id"]): model for model in self.models}
        self.by_family: dict[str, list[dict]] = {}
        self.by_tail: dict[str, list[dict]] = {}
        self.distributions: dict[str, list[float]] = {}
        for metric in {source["metric"] for source in RANKING_SOURCES.values()}:
            self.distributions[metric] = sorted(
                score["score"] for model in self.models for score in model.get("scores", [])
                if score.get("league") == metric and isinstance(score.get("score"), (int, float))
            )
        for model in self.models:
            aliases = [model["id"], *model.get("aliases", [])]
            for alias in aliases:
                self.by_tail.setdefault(canonical(alias.split("/")[-1]), []).append(model)
                family = model_family(alias)
                if family:
                    self.by_family.setdefault(family, []).append(model)

    def match(self, model_id: str) -> dict | None:
        exact = self.by_id.get(canonical(model_id))
        if exact:
            return exact
        candidates = self.by_tail.get(canonical(model_id.split("/")[-1]), [])
        unique = list({id(model): model for model in candidates}.values())
        if len(unique) == 1:
            return unique[0]
        family_candidates = self.by_family.get(model_family(model_id), [])
        unique = list({id(model): model for model in family_candidates}.values())
        return unique[0] if len(unique) == 1 else None

    def rank(self, model_ids: list[str], source: str = DEFAULT_RANKING_SOURCE) -> list[dict]:
        if source not in RANKING_SOURCES:
            raise ValueError(f"Unknown ranking source: {source}")
        source_config = RANKING_SOURCES[source]
        selected_metric = source_config["metric"]
        rows = []
        for model_id in dict.fromkeys(model_ids):
            match = self.match(model_id)
            metrics = []
            if match:
                score = next(
                    (item for item in match.get("scores", []) if item.get("league") == selected_metric),
                    None,
                )
                distribution = self.distributions[selected_metric]
                if score and distribution:
                    metrics.append({
                        "metric": selected_metric,
                        "label": source_config["metric_label"],
                        "value": score["score"],
                        "percentile": round(
                            100 * bisect_right(distribution, score["score"]) / len(distribution),
                            1,
                        ),
                        "source": score.get("source", ""),
                        "url": score.get("sourceUrl", ""),
                    })
            rows.append({
                "id": model_id,
                "name": match.get("name", model_id) if match else model_id,
                "matched_id": match.get("id") if match else None,
                "score": metrics[0]["percentile"] if metrics else None,
                "metrics": metrics,
                "ranking_source": source,
                "ranking_source_label": source_config["label"],
                "rank": None,
            })
        rows.sort(key=lambda row: (row["score"] is None, -(row["score"] or 0), row["id"].lower()))
        for index, row in enumerate((row for row in rows if row["score"] is not None), start=1):
            row["rank"] = index
        return rows
