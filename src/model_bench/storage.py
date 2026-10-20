"""SQLite persistence for reproducible benchmark observations."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .benchmark import Result


class ResultStore:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("""CREATE TABLE IF NOT EXISTS results (
            model TEXT PRIMARY KEY, tested_at TEXT NOT NULL, protocol TEXT NOT NULL,
            status TEXT NOT NULL, ttft_ms REAL, total_ms REAL, tokens_per_second REAL,
            output_tokens INTEGER, token_source TEXT, error TEXT, samples INTEGER,
            confidence TEXT, effort TEXT
        )""")
        columns = {row["name"] for row in con.execute("PRAGMA table_info(results)")}
        for name, kind in (("samples", "INTEGER"), ("confidence", "TEXT"), ("effort", "TEXT")):
            if name not in columns:
                con.execute(f"ALTER TABLE results ADD COLUMN {name} {kind}")
        con.commit()
        return con

    def save(self, result: Result) -> None:
        with self.connect() as con:
            con.execute("""INSERT OR REPLACE INTO results
                (model, tested_at, protocol, status, ttft_ms, total_ms, tokens_per_second,
                 output_tokens, token_source, error, samples, confidence, effort)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                result.model, datetime.now(timezone.utc).isoformat(), result.protocol, result.status,
                result.ttft_ms, result.total_ms, result.tokens_per_second, result.output_tokens,
                result.token_source, result.error, result.samples, result.confidence, result.effort,
            ))

    def latest(self, model_id: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM results WHERE model = ?", (model_id,)).fetchone()
        return dict(row) if row else None

    def latest_by_model(self) -> dict[str, dict]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM results").fetchall()
        return {row["model"]: dict(row) for row in rows}

    def export_rows(self) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM results ORDER BY tested_at DESC, model").fetchall()
        return [dict(row) for row in rows]


def result_dict(result: Result) -> dict:
    return asdict(result)
