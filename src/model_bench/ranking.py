"""Transparent optional matching against a user-supplied public ranking snapshot."""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from pathlib import Path

from .ranking_sources import model_family

WEIGHTS = {
    "eci": 0.20,
    "livebench_coding": 0.15,
    "gpqa_diamond": 0.05,
    "aa_intelligence": 0.30,
    "arena_text": 0.30,
}
LABELS = {
    "eci": "Epoch ECI",
    "livebench_coding": "LiveBench Coding",
    "gpqa_diamond": "Epoch GPQA",
    "aa_intelligence": "Artificial Analysis",
    "arena_text": "LMArena Text",
}


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
        for metric in WEIGHTS:
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

    def rank(self, model_ids: list[str]) -> list[dict]:
        rows = []
        for model_id in dict.fromkeys(model_ids):
            match = self.match(model_id)
            metrics = []
            if match:
                for metric, weight in WEIGHTS.items():
                    score = next((item for item in match.get("scores", []) if item.get("league") == metric), None)
                    if score and self.distributions[metric]:
                        metrics.append({
                            "metric": metric,
                            "label": LABELS[metric],
                            "value": score["score"],
                            "percentile": round(100 * bisect_right(self.distributions[metric], score["score"]) / len(self.distributions[metric]), 1),
                            "source": score.get("source", ""),
                            "url": score.get("sourceUrl", ""),
                        })
            eligible = len(metrics) >= 2
            weighted = sum(item["percentile"] * WEIGHTS[item["metric"]] for item in metrics)
            total_weight = sum(WEIGHTS[item["metric"]] for item in metrics)
            rows.append({
                "id": model_id,
                "name": match.get("name", model_id) if match else model_id,
                "matched_id": match.get("id") if match else None,
                "score": round(weighted / total_weight, 1) if eligible else None,
                "metrics": metrics,
                "rank": None,
            })
        rows.sort(key=lambda row: (row["score"] is None, -(row["score"] or 0), row["id"].lower()))
        for index, row in enumerate((row for row in rows if row["score"] is not None), start=1):
            row["rank"] = index
        return rows
