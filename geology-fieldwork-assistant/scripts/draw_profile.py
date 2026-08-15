#!/usr/bin/env python3
"""Draw a schematic geological cross-section (信手剖面图) as SVG.

Usage:
    python draw_profile.py profile.json -o profile.svg

Input JSON format:
    {
      "title": "马山采石场信手地质剖面图",
      "direction": "NE45°",
      "scale": "1:1000",
      "topography": [[0, 120], [50, 135], [100, 128]],
      "strata": [
        {"name": "花岗岩", "color": "#e8a0a0", "pattern": "cross",
         "dip": "300°∠35°", "thickness": 40}
      ],
      "notes": "点1附近见断层破碎带"
    }

 topography: list of [distance_m, elevation_m], left to right.
 strata: layers from top (youngest exposed) to bottom; thickness is
 apparent vertical thickness in metres; pattern is one of
 cross / dots / lines / brick / blank.

Exit codes: 0 success, 1 file/usage error, 2 invalid input data.
Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from xml.sax.saxutils import escape

CANVAS_W = 1000
MARGIN_L = 90
MARGIN_R = 260
MARGIN_T = 90
MARGIN_B = 80
DEFAULT_COLORS = ["#f2d38a", "#a8d5a2", "#a0c4e8", "#e8a0a0", "#d3b8e6", "#f0b8a0"]
PATTERNS = {"cross", "dots", "lines", "brick", "blank"}


def load_spec(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(spec, dict):
        raise ValueError("JSON 根节点必须是对象")
    topo = spec.get("topography")
    if not isinstance(topo, list) or len(topo) < 2:
        raise ValueError("topography 至少需要两个 [距离, 高程] 点")
    for point in topo:
        if not (isinstance(point, list) and len(point) == 2 and all(isinstance(v, (int, float)) for v in point)):
            raise ValueError("topography 每项必须是 [数字, 数字]")
    strata = spec.get("strata")
    if not isinstance(strata, list) or not strata:
        raise ValueError("strata 至少需要一个地层")
    for index, layer in enumerate(strata):
        if not isinstance(layer, dict) or not layer.get("name"):
            raise ValueError(f"strata[{index}] 缺少 name")
        if not isinstance(layer.get("thickness"), (int, float)) or layer["thickness"] <= 0:
            raise ValueError(f"strata[{index}].thickness 必须是正数（视厚度，米）")
        pattern = layer.get("pattern", "blank")
        if pattern not in PATTERNS:
            raise ValueError(f"strata[{index}].pattern 只能是 {sorted(PATTERNS)}")
    return spec


def interpolate(topo: list[list[float]], x: float) -> float:
    """Linear interpolation of elevation at distance x."""
    for i in range(len(topo) - 1):
        x0, y0 = topo[i]
        x1, y1 = topo[i + 1]
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return topo[-1][1]


def pattern_defs(layers: list[dict]) -> str:
    defs = []
    for i, layer in enumerate(layers):
        color = layer.get("color") or DEFAULT_COLORS[i % len(DEFAULT_COLORS)]
        pattern = layer.get("pattern", "blank")
        pid = f"pat{i}"
        if pattern == "dots":
            defs.append(
                f'<pattern id="{pid}" width="14" height="14" patternUnits="userSpaceOnUse">'
                f'<rect width="14" height="14" fill="{color}"/>'
                f'<circle cx="4" cy="4" r="1.4" fill="#444"/><circle cx="11" cy="10" r="1.4" fill="#444"/></pattern>'
            )
        elif pattern == "lines":
            defs.append(
                f'<pattern id="{pid}" width="10" height="8" patternUnits="userSpaceOnUse">'
                f'<rect width="10" height="8" fill="{color}"/>'
                f'<line x1="0" y1="4" x2="10" y2="4" stroke="#555" stroke-width="0.8"/></pattern>'
            )
        elif pattern == "cross":
            defs.append(
                f'<pattern id="{pid}" width="16" height="16" patternUnits="userSpaceOnUse">'
                f'<rect width="16" height="16" fill="{color}"/>'
                f'<path d="M0 0 L16 16 M16 0 L0 16" stroke="#7a4a4a" stroke-width="0.7"/></pattern>'
            )
        elif pattern == "brick":
            defs.append(
                f'<pattern id="{pid}" width="24" height="12" patternUnits="userSpaceOnUse">'
                f'<rect width="24" height="12" fill="{color}"/>'
                f'<path d="M0 0 L24 0 M0 12 L24 12 M0 6 L24 6 M12 0 L12 6 M6 6 L6 12 M18 6 L18 12" '
                f'stroke="#666" stroke-width="0.7" fill="none"/></pattern>'
            )
        else:
            defs.append(
                f'<pattern id="{pid}" width="4" height="4" patternUnits="userSpaceOnUse">'
                f'<rect width="4" height="4" fill="{color}"/></pattern>'
            )
    return "".join(defs)


def render(spec: dict) -> str:
    topo: list[list[float]] = spec["topography"]
    strata: list[dict] = spec["strata"]
    title = str(spec.get("title", "信手地质剖面图"))
    direction = str(spec.get("direction", ""))
    scale = str(spec.get("scale", ""))
    notes = str(spec.get("notes", ""))

    dist_min = min(p[0] for p in topo)
    dist_max = max(p[0] for p in topo)
    elev_max = max(p[1] for p in topo)
    total_thickness = sum(layer["thickness"] for layer in strata)
    elev_min = elev_max - total_thickness
    span_x = max(dist_max - dist_min, 1e-6)
    span_y = max(elev_max - elev_min, 1e-6)

    plot_w = CANVAS_W - MARGIN_L - MARGIN_R
    kx = plot_w / span_x
    ky = kx  # 1:1 vertical to horizontal, standard for field sketches
    plot_h = span_y * ky
    canvas_h = MARGIN_T + plot_h + MARGIN_B + (30 if notes else 0)

    def px(x: float) -> float:
        return MARGIN_L + (x - dist_min) * kx

    def py(y: float) -> float:
        return MARGIN_T + (elev_max - y) * ky

    def boundary_path(offset: float) -> str:
        """Top boundary of a layer = topography shifted down by offset metres."""
        points = []
        steps = 60
        for s in range(steps + 1):
            x = dist_min + span_x * s / steps
            points.append(f"{px(x):.1f},{py(interpolate(topo, x) - offset):.1f}")
        return "M" + " L".join(points)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS_W}" height="{canvas_h:.0f}" '
        f'viewBox="0 0 {CANVAS_W} {canvas_h:.0f}" font-family="Microsoft YaHei, SimSun, sans-serif">',
        f"<defs>{pattern_defs(strata)}</defs>",
        f'<rect width="{CANVAS_W}" height="{canvas_h:.0f}" fill="#ffffff"/>',
    ]

    # Strata bands, top layer first
    cumulative = 0.0
    boundaries = [0.0]
    for layer in strata:
        cumulative += layer["thickness"]
        boundaries.append(cumulative)

    for i, layer in enumerate(strata):
        top = boundary_path(boundaries[i])
        bottom = boundary_path(boundaries[i + 1])
        # polygon: top boundary left→right, then bottom boundary right→left
        bottom_rev = " L".join(bottom[1:].split(" L")[::-1])
        parts.append(f'<path d="{top} L{bottom_rev} Z" fill="url(#pat{i})" stroke="#333" stroke-width="0.8"/>')

    # Topography line on top
    topo_line = boundary_path(0.0)
    parts.append(f'<path d="{topo_line}" fill="none" stroke="#000" stroke-width="1.8"/>')

    # Dip symbols + labels at each stratum's mid-outcrop
    for i, layer in enumerate(strata):
        mid_depth = (boundaries[i] + boundaries[i + 1]) / 2
        x_pos = dist_min + span_x * (0.25 + 0.5 * (i / max(len(strata), 1)))
        y_elev = interpolate(topo, x_pos) - mid_depth
        cx, cy = px(x_pos), py(y_elev)
        dip = str(layer.get("dip", ""))
        parts.append(f'<line x1="{cx - 16:.1f}" y1="{cy:.1f}" x2="{cx + 16:.1f}" y2="{cy:.1f}" stroke="#000" stroke-width="1.2"/>')
        parts.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{cx:.1f}" y2="{cy + 10:.1f}" stroke="#000" stroke-width="1.2"/>')
        if dip:
            parts.append(
                f'<text x="{cx + 20:.1f}" y="{cy + 12:.1f}" font-size="12" fill="#000">{escape(dip)}</text>'
            )

    # Elevation axis
    axis_x = MARGIN_L - 20
    parts.append(f'<line x1="{axis_x}" y1="{py(elev_max):.1f}" x2="{axis_x}" y2="{py(elev_min):.1f}" stroke="#000"/>')
    tick_step = max(10, math.ceil(span_y / 5 / 10) * 10)
    y_tick = math.ceil(elev_min / tick_step) * tick_step
    while y_tick <= elev_max:
        parts.append(f'<line x1="{axis_x - 5}" y1="{py(y_tick):.1f}" x2="{axis_x}" y2="{py(y_tick):.1f}" stroke="#000"/>')
        parts.append(f'<text x="{axis_x - 8}" y="{py(y_tick) + 4:.1f}" font-size="11" text-anchor="end">{y_tick:g}m</text>')
        y_tick += tick_step

    # Title / direction / scale
    parts.append(f'<text x="{CANVAS_W / 2:.0f}" y="40" font-size="22" font-weight="bold" text-anchor="middle">{escape(title)}</text>')
    info = "　".join(item for item in [f"方向 {escape(direction)}" if direction else "", f"比例尺 {escape(scale)}" if scale else ""] if item)
    if info:
        parts.append(f'<text x="{CANVAS_W / 2:.0f}" y="66" font-size="14" text-anchor="middle" fill="#333">{info}</text>')

    # Legend
    legend_x = CANVAS_W - MARGIN_R + 40
    legend_y = MARGIN_T + 10
    parts.append(f'<rect x="{legend_x - 12}" y="{legend_y - 24}" width="{MARGIN_R - 70}" height="{len(strata) * 30 + 40}" fill="#fafafa" stroke="#999"/>')
    parts.append(f'<text x="{legend_x}" y="{legend_y - 6}" font-size="13" font-weight="bold">图例</text>')
    for i, layer in enumerate(strata):
        y = legend_y + 12 + i * 30
        parts.append(f'<rect x="{legend_x}" y="{y}" width="26" height="16" fill="url(#pat{i})" stroke="#333" stroke-width="0.6"/>')
        parts.append(f'<text x="{legend_x + 34}" y="{y + 13}" font-size="13">{escape(str(layer["name"]))}</text>')

    if notes:
        parts.append(f'<text x="{MARGIN_L}" y="{canvas_h - 24:.0f}" font-size="13" fill="#333">注：{escape(notes)}</text>')

    parts.append("</svg>")
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="绘制示意地质剖面图（SVG，仅标准库）")
    parser.add_argument("spec", type=Path, help="剖面数据 JSON 路径")
    parser.add_argument("-o", "--output", type=Path, required=True, help="输出 SVG 路径")
    args = parser.parse_args()

    if not args.spec.is_file():
        print(f"错误：文件不存在：{args.spec}", file=sys.stderr)
        return 1
    try:
        spec = load_spec(args.spec)
    except (OSError, ValueError) as exc:
        print(f"输入数据无效：{exc}", file=sys.stderr)
        return 2

    svg = render(spec)
    try:
        args.output.write_text(svg, encoding="utf-8")
    except OSError as exc:
        print(f"错误：无法写入 {args.output}：{exc}", file=sys.stderr)
        return 1
    print(f"已生成剖面图 → {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
