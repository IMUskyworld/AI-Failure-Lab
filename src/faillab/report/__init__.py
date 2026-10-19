"""Dashboard assembly + serialization (DEVELOPMENT.md §12)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from faillab import __version__
from faillab.domain.enums import ReproStatus
from faillab.domain.models import (
    CategoryCount,
    IncidentBrief,
    KeyCount,
    Report,
    ReportTotals,
    ToolInfo,
    TopSignature,
)
from faillab.report.svg import bar_chart
from faillab.store import Store

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _instant(value: str) -> float:
    """Sort timestamps as instants; tolerate hand-written or legacy values."""
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def build_report(store: Store) -> Report:
    incidents = store.list_incidents()
    counts = store.status_counts()
    reproduced = counts.get(ReproStatus.REPRODUCED.value, 0)
    unverified = counts.get(ReproStatus.UNVERIFIED.value, 0)
    attempted = len(incidents) - unverified
    totals = ReportTotals(
        incidents=len(incidents),
        open=counts.get("open", 0),
        reproduced=counts.get("reproduced", 0),
        fixed=counts.get("fixed", 0),
        wontfix=counts.get("wontfix", 0),
        unverified=unverified,
        repro_rate=round(reproduced / attempted, 3) if attempted else 0.0,
    )

    category_counts: dict[str, tuple[str, int]] = {}
    model_counts: dict[str, int] = {}
    harness_counts: dict[str, int] = {}
    for incident in incidents:
        label, count = category_counts.get(incident.category, (incident.category_label, 0))
        category_counts[incident.category] = (label, count + 1)
        model_counts[incident.model] = model_counts.get(incident.model, 0) + 1
        harness_counts[incident.harness] = harness_counts.get(incident.harness, 0) + 1

    # Secondary keys keep reports deterministic when counts tie (stable git diffs).
    by_category = [
        CategoryCount(category=category, label=label, count=count)
        for category, (label, count) in sorted(category_counts.items(), key=lambda item: (-item[1][1], item[0]))
    ]
    by_model = [
        KeyCount(key=key, count=count)
        for key, count in sorted(model_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    by_harness = [
        KeyCount(key=key, count=count)
        for key, count in sorted(harness_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    top_signatures = [
        TopSignature(
            signature=incident.signature,
            title=incident.title,
            public_id=incident.public_id,
            occurrences=incident.occurrences,
        )
        for incident in sorted(incidents, key=lambda item: (-item.occurrences, item.public_id))[:5]
    ]
    recent = [
        IncidentBrief(
            public_id=incident.public_id,
            title=incident.title,
            category=incident.category,
            category_label=incident.category_label,
            status=incident.status,
            repro_status=incident.reproduction.status,
            model=incident.model,
            harness=incident.harness,
            occurrences=incident.occurrences,
        )
        for incident in sorted(incidents, key=lambda item: (_instant(item.updated_at), item.public_id), reverse=True)[
            :10
        ]
    ]
    return Report(
        tool=ToolInfo(version=__version__),
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        totals=totals,
        by_category=by_category,
        by_model=by_model,
        by_harness=by_harness,
        top_signatures=top_signatures,
        recent=recent,
    )


def dump_json(payload: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def write_json_report(report: Report, path: Path) -> None:
    dump_json(report.model_dump(mode="json"), path)


def build_charts(report: Report) -> dict[str, str]:
    return {
        "by_category": bar_chart([(item.label, float(item.count)) for item in report.by_category], title="分类分布"),
        "by_model": bar_chart([(item.key, float(item.count)) for item in report.by_model], title="模型分布"),
        "by_harness": bar_chart([(item.key, float(item.count)) for item in report.by_harness], title="Harness 分布"),
    }


def write_html_report(report: Report, path: Path) -> None:
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template("dashboard.html.j2")
    html = template.render(report=report, charts=build_charts(report))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8", newline="\n")
