"""Measure import / analyze / search / report overhead against DEVELOPMENT.md §16.

Usage:  python scripts/bench_overhead.py
Exit 1 when a measurement exceeds its target (2x grace on CI runners).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from faillab.analyze import analyze_offline  # noqa: E402
from faillab.domain.models import Incident, Trace  # noqa: E402
from faillab.ingest import import_file  # noqa: E402
from faillab.paths import WorkLayout  # noqa: E402
from faillab.report import build_report, write_html_report, write_json_report  # noqa: E402
from faillab.store import Store  # noqa: E402

TIMESTAMP = "2026-09-22T22:00:00+08:00"


def make_trace(trace_id: str = "tr-bench", *, error: str = "ModuleNotFoundError: no module named 'x'") -> Trace:
    return Trace(
        trace_id=trace_id,
        harness="bench",
        model="bench-model",
        task="bench 任务",
        error=error,
        final_result="[failed] bench",
    )


def make_incident(public_id: str, *, title: str = "bench") -> Incident:
    return Incident(
        public_id=public_id,
        slug="bench",
        title=title,
        task="t",
        failure="f",
        created_at=TIMESTAMP,
        updated_at=TIMESTAMP,
    )


def make_trace_file(directory: Path, name: str, *, error: str) -> Path:
    import json as json_module

    path = directory / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"kind": "task", "task": f"bench 任务 {name}", "model": "bench-model", "harness": "bench"},
        {"kind": "error", "message": error},
        {"kind": "final", "result": "bench run", "ok": False},
    ]
    path.write_text(
        "\n".join(json_module.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


TRACE_COUNT = 100
SEARCH_COUNT = 1000

TARGETS = {
    f"import {TRACE_COUNT} traces": 1.0,
    "analyze (offline, single)": 0.1,
    f"search over {SEARCH_COUNT} incidents": 0.2,
    "report (build + html + json)": 1.0,
}

REPEATS = 3


def best_of(action, repeats: int = REPEATS) -> tuple[float, object]:
    best = float("inf")
    result = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = action()
        best = min(best, time.perf_counter() - started)
    return best, result


def main() -> int:
    grace = 2.0 if os.environ.get("CI") else 1.0
    tmp = Path(tempfile.mkdtemp(prefix="faillab-bench-"))
    layout = WorkLayout(tmp / ".faillab")
    try:
        trace_files = [
            make_trace_file(
                tmp / "traces", f"trace-{index:03d}", error=f"ModuleNotFoundError: no module named 'pkg{index}'"
            )
            for index in range(TRACE_COUNT)
        ]
        with Store(layout.db_path) as store:
            import_seconds, _ = best_of(
                lambda: [import_file(path, store=store, force_new=True) for path in trace_files],
                repeats=1,
            )

            analyze_seconds, _ = best_of(lambda: analyze_offline(make_incident("F-9999"), make_trace(), store))

            for index in range(SEARCH_COUNT):
                store.insert_incident(make_incident(f"F-{index + 1000:04d}", title=f"批量事故 {index}"))
            search_seconds, _ = best_of(lambda: store.list_incidents(query="批量", limit=50))

            def render_report() -> None:
                report = build_report(store)
                write_json_report(report, layout.root / "report.json")
                write_html_report(report, layout.root / "report.html")

            report_seconds, _ = best_of(render_report, repeats=1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    measurements = {
        f"import {TRACE_COUNT} traces": import_seconds,
        "analyze (offline, single)": analyze_seconds,
        f"search over {SEARCH_COUNT} incidents": search_seconds,
        "report (build + html + json)": report_seconds,
    }
    print(f"{'operation':<34} {'actual':>8} {'target':>8}  verdict")
    failures = 0
    for name, actual in measurements.items():
        target = TARGETS[name]
        ok = actual <= target * grace
        failures += 0 if ok else 1
        print(f"{name:<34} {actual:>7.3f}s {target:>7.1f}s  {'PASS' if ok else 'FAIL'}")
    if failures:
        print(f"\n{failures} 项超过目标（grace x{grace:g}）")
        return 1
    print("\nall measurements within targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
