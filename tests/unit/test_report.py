from __future__ import annotations

import io
import json
import re
from pathlib import Path

from rich.console import Console

from faillab.domain.enums import IncidentStatus, ReproStatus
from faillab.report import build_charts, build_report, write_html_report, write_json_report
from faillab.report.console import render_dashboard_brief, render_incident_detail, render_incident_list
from faillab.report.svg import bar_chart
from faillab.store import Store
from support import make_incident, make_repro_run

EXTERNAL_REF_RE = re.compile(r'(?:src|href)\s*=\s*"https?://')


def test_bar_chart_basic_and_escaping() -> None:
    svg = bar_chart([("上下文误判", 5.0), ("<b>bad</b>", 2.0)], title="分类分布")
    assert "<svg" in svg and "chart-bar" in svg
    assert "上下文误判" in svg
    assert "<b>bad</b>" not in svg and "&lt;b&gt;" in svg
    empty = bar_chart([], title="空")
    assert "无数据" in empty


def test_build_report_counts_and_order(tmp_path: Path) -> None:
    with Store(tmp_path / "faillab.db") as store:
        store.insert_incident(
            make_incident(
                "F-0001",
                category="context_misidentification",
                category_label="上下文误判",
                model="m-a",
                harness="h-a",
                occurrences=3,
            )
        )
        store.insert_incident(
            make_incident(
                "F-0002",
                category="context_misidentification",
                category_label="上下文误判",
                model="m-b",
                harness="h-a",
                status=IncidentStatus.FIXED,
                repro_status=ReproStatus.FIXED,
                occurrences=1,
                updated_at="2026-09-23T00:00:00+08:00",
            )
        )
        store.insert_incident(
            make_incident(
                "F-0003",
                category="tool_misuse",
                category_label="工具参数误用",
                model="m-a",
                harness="h-b",
                repro_status=ReproStatus.REPRODUCED,
                updated_at="2026-09-24T00:00:00+08:00",
            )
        )
        report = build_report(store)

    assert report.totals.incidents == 3
    assert report.totals.fixed == 1
    assert report.totals.unverified == 1
    assert report.by_category[0].category == "context_misidentification"
    assert report.by_category[0].count == 2
    assert {item.key for item in report.by_model} == {"m-a", "m-b"}
    assert report.top_signatures[0].public_id == "F-0001"  # occurrences=3 最多
    assert report.recent[0].public_id == "F-0003"  # 最新 updated_at
    assert 0.0 <= report.totals.repro_rate <= 1.0


def test_report_ties_are_deterministic(tmp_path: Path) -> None:
    with Store(tmp_path / "faillab.db") as store:
        store.insert_incident(make_incident("F-0002", model="m-b", harness="h-b"))
        store.insert_incident(make_incident("F-0001", model="m-a", harness="h-a"))
        report = build_report(store)
    assert [item.key for item in report.by_model] == ["m-a", "m-b"]
    assert [item.key for item in report.by_harness] == ["h-a", "h-b"]
    assert [item.public_id for item in report.recent] == ["F-0002", "F-0001"]  # updated_at 相同 → ID 兜底


def test_write_json_report_roundtrip(tmp_path: Path) -> None:
    with Store(tmp_path / "faillab.db") as store:
        store.insert_incident(make_incident())
        report = build_report(store)
    path = tmp_path / "report.json"
    write_json_report(report, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["tool"]["name"] == "faillab"
    assert payload["totals"]["incidents"] == 1
    assert "事故" in path.read_text(encoding="utf-8") or payload["totals"]["incidents"] == 1


def test_write_html_report_self_contained(tmp_path: Path) -> None:
    with Store(tmp_path / "faillab.db") as store:
        store.insert_incident(make_incident(title="示例事故 <script>", category_label="上下文误判"))
        report = build_report(store)
    path = tmp_path / "report.html"
    write_html_report(report, path)
    html = path.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert EXTERNAL_REF_RE.search(html) is None, "仪表盘必须自包含"
    assert "<svg" in html
    assert "分类分布" in html
    assert "<script>" not in html  # 标题中的标签被转义
    assert "事故总数" in html


def test_build_charts_three_sections(tmp_path: Path) -> None:
    with Store(tmp_path / "faillab.db") as store:
        store.insert_incident(make_incident())
        report = build_report(store)
    charts = build_charts(report)
    assert set(charts) == {"by_category", "by_model", "by_harness"}
    for svg in charts.values():
        assert "<svg" in svg


def test_console_renderers_smoke(tmp_path: Path) -> None:
    buffer = io.StringIO()
    console = Console(file=buffer, width=150)
    with Store(tmp_path / "faillab.db") as store:
        incident = make_incident(category_label="上下文误判")
        store.insert_incident(incident)
        report = build_report(store)
        render_incident_list(console, [incident])
        render_incident_detail(console, incident, [make_repro_run()])
        render_dashboard_brief(console, report)
    text = buffer.getvalue()
    assert "事故列表" in text
    assert "证据" in text
    assert "总览" in text
