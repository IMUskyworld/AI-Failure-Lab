"""Hand-rolled SVG bar charts — self-contained reports with zero external assets (§12)."""

from __future__ import annotations

from html import escape

WIDTH = 680
ROW_HEIGHT = 26
LABEL_WIDTH = 180
VALUE_WIDTH = 56


def bar_chart(items: list[tuple[str, float]], *, title: str) -> str:
    """Horizontal bars: [(label, value)]; deterministic markup, escaped labels."""
    if not items:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="80" viewBox="0 0 {WIDTH} 80" role="img">'
            f'<text x="12" y="20" class="chart-title">{escape(title)}</text>'
            f'<text x="12" y="48" class="chart-label">（无数据）</text></svg>'
        )
    height = 34 + ROW_HEIGHT * len(items) + 8
    max_value = max(value for _label, value in items) or 1.0
    bar_area = WIDTH - LABEL_WIDTH - VALUE_WIDTH - 16
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img">',
        f'<text x="12" y="20" class="chart-title">{escape(title)}</text>',
    ]
    for index, (label, value) in enumerate(items):
        y = 34 + index * ROW_HEIGHT
        bar_width = max(bar_area * (value / max_value), 2.0)
        parts.append(
            f'<text x="{LABEL_WIDTH - 8}" y="{y + 15}" class="chart-label" text-anchor="end">{escape(label)}</text>'
        )
        parts.append(
            f'<rect x="{LABEL_WIDTH}" y="{y + 4}" width="{bar_width:.1f}" '
            f'height="{ROW_HEIGHT - 10}" rx="3" class="chart-bar"/>'
        )
        parts.append(f'<text x="{LABEL_WIDTH + bar_width + 6:.1f}" y="{y + 15}" class="chart-label">{value:g}</text>')
    parts.append("</svg>")
    return "".join(parts)
