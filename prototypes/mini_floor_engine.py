"""Deliberately minimal resize engine (pragmatist experiment, NOT production).
Rigid units (group_id or singleton) + full-bleed background + decorations kept
proportional + content reflowed into n balanced contiguous lanes at a common
scale. No font measurement at all: boxes and font_size scale together, which
preserves the renderer's wrap exactly (skia advances are linear in size).
"""
import math
from typing import Any

def _aabb(e):
    x, y, w, h = e["x"], e["y"], e["width"], e["height"]
    rot = float(e.get("rotation") or 0)
    if not rot:
        return x, y, w, h
    a = math.radians(rot); c, s = abs(math.cos(a)), abs(math.sin(a))
    bw, bh = w * c + h * s, w * s + h * c
    cx, cy = x + w / 2, y + h / 2
    return cx - bw / 2, cy - bh / 2, bw, bh

def _union(boxes):
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
    return x0, y0, x1 - x0, y1 - y0

def _alpha(e):
    fc = e.get("fill_color") or ""
    try:
        a = float(fc.strip()[:-1].split(",")[3]) if fc.startswith("rgba") else 1.0
    except Exception:
        a = 1.0
    return a * float(e.get("opacity", 1.0))

def resize(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    SW, SH = source["canvas"]["width"], source["canvas"]["height"]
    TW, TH = target["width"], target["height"]
    sx, sy = TW / SW, TH / SH
    s = min(sx, sy)
    els = source["elements"]
    out = {e["id"]: dict(e) for e in els}

    # 1. classify
    bg, deco, content = [], [], {}
    for e in els:
        bx, by, bw, bh = _aabb(e)
        cover = (max(0, min(bx + bw, SW) - max(bx, 0)) * max(0, min(by + bh, SH) - max(by, 0))) / (SW * SH)
        if e["type"] != "text" and cover >= 0.9 and not e.get("group_id"):
            bg.append(e); continue
        if e["type"] == "shape" and not e.get("group_id") and (
                bx < 0 or by < 0 or bx + bw > SW or by + bh > SH or _alpha(e) <= 0.5):
            deco.append(e); continue
        content.setdefault(e.get("group_id") or ("__" + e["id"]), []).append(e)

    # 2. background full bleed; decorations proportional, normalized center, clamped
    for e in bg:
        out[e["id"]].update(x=0, y=0, width=TW, height=TH)
    for e in deco:
        w, h = e["width"] * s, e["height"] * s
        cx, cy = (e["x"] + e["width"] / 2) / SW * TW, (e["y"] + e["height"] / 2) / SH * TH
        x = min(max(cx - w / 2, 0), TW - w); y = min(max(cy - h / 2, 0), TH - h)
        out[e["id"]].update(x=x, y=y, width=w, height=h)

    # 3. content units in reading order
    units = []
    for key, members in content.items():
        ub = _union([_aabb(m) for m in members])
        units.append((ub, members))
    units.sort(key=lambda u: (round((u[0][1] + u[0][3] / 2) / 40), u[0][0]))  # row-ish then x

    r = (TW / TH) / (SW / SH)
    margin = max(4.0, 0.04 * min(TW, TH)); gap = max(4.0, 0.025 * min(TW, TH))
    AW, AH = TW - 2 * margin, TH - 2 * margin

    def place_unit(ub, members, k, x, y):
        # uniform scale k about the unit's top-left; keeps rotation and wrap
        for m in members:
            mb = _aabb(m)
            o = out[m["id"]]
            o["x"] = x + (m["x"] - ub[0]) * k; o["y"] = y + (m["y"] - ub[1]) * k
            o["width"] = m["width"] * k; o["height"] = m["height"] * k
            if m["type"] == "text":
                o["font_size"] = max(1.0, m["font_size"] * k)

    if 1 / 1.4 <= r <= 1.4 or len(units) <= 1:
        # ANCHOR: proportional sizes, normalized centers, clamp inside
        for ub, members in units:
            w, h = ub[2] * s, ub[3] * s
            cx, cy = (ub[0] + ub[2] / 2) / SW * TW, (ub[1] + ub[3] / 2) / SH * TH
            x = min(max(cx - w / 2, 0), TW - w); y = min(max(cy - h / 2, 0), TH - h)
            place_unit(ub, members, s, x, y)
    else:
        wide = r > 1
        n = max(1, min(4, round(r), len(units))) if wide else len(units)  # tall: pure vertical stack
        # balanced contiguous partition along the stacking axis (DP on max-lane-demand)
        dem = [(ub[3] if wide else ub[2]) for ub, _ in units]
        best = None
        import itertools
        for cuts in itertools.combinations(range(1, len(units)), n - 1):
            idx = [0, *cuts, len(units)]
            lanes = [list(range(idx[i], idx[i + 1])) for i in range(n)]
            cost = max(sum(dem[j] for j in L) for L in lanes)
            if best is None or cost < best[0]:
                best = (cost, lanes)
        lanes = best[1]
        # common scale: each lane's stack must fit the stacking axis; lanes side by side must fit the other
        lane_w = [max((units[j][0][2] if wide else units[j][0][3]) for j in L) for L in lanes]
        lane_h = [sum((units[j][0][3] if wide else units[j][0][2]) for j in L) for L in lanes]
        lane_g = [gap * (len(L) - 1) for L in lanes]
        along, across = (AH, AW) if wide else (AW, AH)
        k = min(min((along - g) / h for h, g in zip(lane_h, lane_g)), (across - gap * (n - 1)) / sum(lane_w))
        # distribute leftover across-space evenly between lanes
        used = sum(lw * k for lw in lane_w)
        spread = (across - used) / (n + 1)
        pos = margin + spread
        for L, lw, lh, lg in zip(lanes, lane_w, lane_h, lane_g):
            start = margin + (along - (lh * k + lg)) / 2
            for j in L:
                ub, members = units[j]
                if wide:
                    place_unit(ub, members, k, pos + (lw - ub[2]) * k / 2, start)
                    start += ub[3] * k + gap
                else:
                    place_unit(ub, members, k, start, pos + (lw - ub[3]) * k / 2)
                    start += ub[2] * k + gap
            pos += lw * k + spread

    res = []
    for e in els:
        o = out[e["id"]]
        for f in ("x", "y", "width", "height"):
            o[f] = round(float(o[f]), 2)
        res.append(o)
    return {"canvas": {"width": TW, "height": TH}, "elements": res}
