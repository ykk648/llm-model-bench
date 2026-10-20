"""Command-line interface for safe local benchmark operation."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shutil
import sys
from pathlib import Path

import uvicorn

from .config import ConfigError, load_config
from .engine import BenchmarkEngine
from .web import create_app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="model-bench", description="Serial, reproducible LLM API benchmarks")
    parser.add_argument("--config", default="model-bench.toml", help="TOML configuration path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="Validate configuration without API calls")
    init = commands.add_parser("init", help="Copy the generic example configuration")
    init.add_argument("--output", default="model-bench.toml")
    discover = commands.add_parser("discover", help="List endpoint /models without changing configuration")
    discover.add_argument("--endpoint", required=True)
    benchmark = commands.add_parser("benchmark", help="Run strictly serial benchmark requests")
    benchmark.add_argument("--model", action="append", default=[], help="Exact configured model id; repeatable")
    benchmark.add_argument("--tag", action="append", default=[], help="Select configured models by tag; repeatable")
    benchmark.add_argument("--all", action="store_true", help="Benchmark every configured model")
    benchmark.add_argument("--force", action="store_true", help="Ignore configured cooldown")
    export = commands.add_parser("export", help="Export latest results from SQLite")
    export.add_argument("--format", choices=("json", "csv"), default="json")
    export.add_argument("--output", default="-")
    serve = commands.add_parser("serve", help="Run the optional local dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    return parser


async def _run_benchmark(engine: BenchmarkEngine, args: argparse.Namespace) -> int:
    if args.all:
        selected = list(engine.config.models)
    elif args.model:
        selected = engine.select(args.model, args.tag)
    elif args.tag:
        selected = engine.select(list(engine.config.models), args.tag)
    else:
        selected = engine.default_selection()
    if not selected:
        print("No configured models selected.", file=sys.stderr)
        return 2
    print(f"Benchmarking {len(selected)} model(s) serially; each has {engine.config.benchmark.samples} sample(s).")
    results = await engine.benchmark_many(selected, force=args.force)
    for result in results:
        speed = f"{result.tokens_per_second} tok/s" if result.tokens_per_second is not None else result.status
        print(f"{result.model}: {result.status}; TTFT={result.ttft_ms} ms; {speed}")
    return 1 if any(result.status != "ok" for result in results) else 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        target = Path(args.output).expanduser()
        if target.exists():
            parser.error(f"Refusing to overwrite {target}")
        source = Path(__file__).resolve().parent / "templates" / "model-bench.example.toml"
        shutil.copyfile(source, target)
        print(f"Wrote {target}. Set the API key environment variable before benchmarking.")
        return 0
    try:
        config = load_config(args.config)
        engine = BenchmarkEngine(config)
        if args.command == "validate":
            print(f"Valid: {len(config.endpoints)} endpoint(s), {len(config.models)} model(s), serial samples={config.benchmark.samples}")
            return 0
        if args.command == "discover":
            print("\n".join(asyncio.run(engine.discover(args.endpoint))))
            return 0
        if args.command == "benchmark":
            return asyncio.run(_run_benchmark(engine, args))
        if args.command == "export":
            rows = engine.store.export_rows()
            if args.format == "json":
                output = json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
                if args.output == "-":
                    print(output, end="")
                else:
                    Path(args.output).write_text(output)
            else:
                destination = sys.stdout if args.output == "-" else Path(args.output).open("w", newline="")
                try:
                    writer = csv.DictWriter(destination, fieldnames=list(rows[0]) if rows else [])
                    if rows:
                        writer.writeheader()
                        writer.writerows(rows)
                finally:
                    if destination is not sys.stdout:
                        destination.close()
            return 0
        if args.command == "serve":
            uvicorn.run(create_app(args.config), host=args.host, port=args.port)
            return 0
    except (ConfigError, OSError, ValueError) as exc:
        print(f"model-bench: {exc}", file=sys.stderr)
        return 2
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
