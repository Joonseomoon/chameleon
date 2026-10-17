"""
THIS IS THE FILE YOU IMPLEMENT.

Everything else in this repo (input loading, output writing, rendering,
the Dockerfile) is plumbing so you can focus on exactly one function:
`resize()`. See RULES.md and the design schema for the full contract.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

from functools import lru_cache

# Section ownership (docs/engine.md §4). Each developer edits only their own sections.
#   §0 shared types, geometry, TUNABLES ........... Dev 3 (changes reviewed by all)
#   §1 source analysis ............................ Dev 1
#   §2 candidate generation ....................... Dev 1
#   §3 text and image fitting ..................... Dev 2
#   §4-§6 validation, fallback, resize() .......... Dev 3   (baseline `resize()` kept until replaced)

# =====================================================================================
# §0 Shared types, geometry, TUNABLES
# =====================================================================================

TUNABLES: dict[str, Any] = {}  # every number is PROVISIONAL (docs/engine.md); sections add their own keys


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def intersect(self, o: Box) -> float:
        """Overlap AREA with `o` (0 when disjoint)."""
        ox = min(self.right, o.right) - max(self.x, o.x)
        oy = min(self.bottom, o.bottom) - max(self.y, o.y)
        return ox * oy if ox > 0 and oy > 0 else 0.0

    def contains(self, o: Box, tol: float = 0.0) -> bool:
        return (o.x >= self.x - tol and o.y >= self.y - tol
                and o.right <= self.right + tol and o.bottom <= self.bottom + tol)

    def scaled(self, s: float, ox: float = 0.0, oy: float = 0.0) -> Box:
        return Box(ox + (self.x - ox) * s, oy + (self.y - oy) * s, self.w * s, self.h * s)

    def translated(self, dx: float, dy: float) -> Box:
        return Box(self.x + dx, self.y + dy, self.w, self.h)


def _num(v: Any, default: float = 0.0) -> float:
    """float(v), or `default` for None / strings / NaN / inf (resize() sanitizes first; this keeps helpers total)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _rotated_aabb(el: dict[str, Any]) -> Box:
    """Axis-aligned bounds of the element as drawn (rotation is clockwise degrees about the box centre)."""
    x, y = _num(el.get("x")), _num(el.get("y"))
    w, h = max(0.0, _num(el.get("width"))), max(0.0, _num(el.get("height")))
    rot = _num(el.get("rotation"))
    if not rot:
        return Box(x, y, w, h)
    a = math.radians(rot)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    bw, bh = w * c + h * s, w * s + h * c
    return Box(x + w / 2 - bw / 2, y + h / 2 - bh / 2, bw, bh)


def _overlap(a: Box, b: Box) -> float:
    return a.intersect(b)


def _union(boxes: list[Box]) -> Box:
    x0 = min(b.x for b in boxes)
    y0 = min(b.y for b in boxes)
    return Box(x0, y0, max(b.right for b in boxes) - x0, max(b.bottom for b in boxes) - y0)


@dataclass
class LayoutUnit:
    uid: str                       # "g<n>" group, "e<n>" singleton / implicit-containment unit; n = first member's SOURCE INDEX
    member_ids: list[str]          # element ids, source order
    kind: str                      # "background" | "decoration" | "content"
    src_box: Box                   # union of members' ROTATED AABBs, source px
    ink_box: Box                   # visible extent: measured text, asset alpha bbox, shapes as drawn
    rel: dict[str, Box]            # member id -> UNROTATED box relative to src_box origin (source scale)
    rotation: float                # 0 unless a member is rotated
    has_text: bool
    has_image: bool
    has_shape: bool
    aspect_locked: bool            # non-background image or any rotated member: uniform scale only
    prominence: float = 0.0        # 0..1
    h_align: str = "center"        # "left" | "center" | "right"
    edge_anchor: tuple[str | None, str | None] | None = None   # corner furniture: (top|bottom, left|right)
    block: int = -1                # index into SourceAnalysis.blocks
    sibling_set: int | None = None
    band: str | None = None        # "top" | "bottom": full-width header/footer strip (flow-exempt)
    fs_max: float = 0.0            # largest member font size (0 without text)


@dataclass
class SourceAnalysis:
    canvas_w: int
    canvas_h: int
    elements: dict[str, dict[str, Any]]      # original element dicts by id (read-only)
    order: list[str]                         # element ids in source order
    units: dict[str, LayoutUnit]
    backgrounds: list[str] = field(default_factory=list)   # unit uids, source order
    decorations: list[str] = field(default_factory=list)
    content: list[str] = field(default_factory=list)
    blocks: list[list[str]] = field(default_factory=list)  # reading-order blocks of CONTENT units
    flow_axis: str = "vertical"              # "vertical": blocks are rows; "horizontal": blocks are columns
    # unit pair -> overlapped fraction of the SMALLER ink box in the source (`pair in src_overlaps` works like a set)
    src_overlaps: dict[frozenset[str], float] = field(default_factory=dict)
    margin_frac: float = 0.0                 # source outer margin / short side


@dataclass
class Candidate:
    name: str
    slots: dict[str, Box]                    # content uid -> slot on the target canvas
    deco: dict[str, Box]                     # decoration uid -> final box
    align: dict[str, tuple[str, str]]        # content uid -> (h: left|center|right, v: top|middle|bottom) inside its slot
    k: float = 1.0                           # common scale of the candidate
    scales: dict[str, float] = field(default_factory=dict)   # content uid -> uniform scale that sized its slot (>= k when a floor lifted it)
    lanes: int = 0                           # lane count (0 = anchor)
    rung: float = 1.0                        # readability-floor ladder multiplier (1.0 = full floors, 0.0 = none)
    pinned: list[str] = field(default_factory=list)          # corner-pinned unit uids
    notes: list[str] = field(default_factory=list)


@dataclass
class TextFit:
    font_size: float
    lines: int
    text_w: float
    text_h: float
    fits: bool
    below_floor: bool


@dataclass
class FittedCandidate:
    candidate: Candidate
    elements: dict[str, dict[str, Any]]
    used: dict[str, Box]
    text: dict[str, TextFit]
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    hard: list[str]
    soft: dict[str, float]
    score: float


# =====================================================================================
# §3 Text and image fitting (Dev 2)
# Public handoff: fit_candidate(analysis, candidate, target) -> FittedCandidate.
# Failed fits are reported, never hidden by deleting or changing source content.
# =====================================================================================

TUNABLES.update({"fit_default_floor": 12.0})


@lru_cache(maxsize=1)
def _font_manager():
    try:
        from renderer.converter.brand_fonts import load_brand_font_manager
        return load_brand_font_manager(str(Path(__file__).resolve().parent.parent / "brand_kit/fonts"))
    except (ImportError, OSError):
        return None


@lru_cache(maxsize=2048)
def _font_metrics(family, weight, style, size, text, fallback):
    """Character advances and vertical extent. Cache font work across candidates."""
    if not fallback:
        import skia
        face = skia.Typeface.MakeFromData(_font_manager().lookup(family, weight, style))
        font = skia.Font(face, size)
        metrics = font.getMetrics()
        return tuple(font.getWidths(font.textToGlyphs(text))), -metrics.fAscent + metrics.fDescent
    from PIL import ImageFont
    root = Path(__file__).resolve().parent.parent / "brand_kit/fonts"
    suffix = {"regular": "Regular", "bold": "Bold", "italic": "Italic", "bolditalic": "BoldItalic"}[style]
    paths = [root / (family.replace(" ", "") + "-" + suffix + ".ttf")]
    paths += [root / (family.replace(" ", "") + "-Regular.ttf")]
    font = None
    for path in paths:
        try:
            font = ImageFont.truetype(str(path), max(1, round(size)))
            break
        except OSError:
            pass
    if font is None:
        font = ImageFont.load_default(size=max(1, round(size)))
    ascent, descent = font.getmetrics()
    return tuple(font.getlength(c) for c in text), ascent + descent


def _wrap_fallback(text, widths, max_width):
    """Same space-replacement algorithm as converter.wrap, without importing skia."""
    if not text or max_width <= 0:
        return text
    chars, total, last = list(text), 0.0, None
    for i, char in enumerate(chars):
        if char == "\n":
            total, last = 0.0, None
            continue
        if total + widths[i] > max_width and last is not None:
            chars[last] = "\n"
            total = sum(widths[j] for j in range(last + 1, i + 1) if chars[j] != "\n")
            last = None
        else:
            total += widths[i]
        if char == " ":
            last = i
    return "".join(chars)


@lru_cache(maxsize=8192)
def _measure_cached(payload, size, box_w, fallback):
    import json
    from itertools import groupby
    el = json.loads(payload)
    text = str(el.get("content", ""))
    if not text:
        return 0, 0.0, 0.0
    family = el.get("font_family", "Montserrat")
    bold_weights = {"bold", "semibold", "extrabold", "black"}
    weight = str(el.get("font_weight", "regular")).lower()
    bold = weight in bold_weights
    italic = el.get("font_style", el.get("italic")) in ("italic", True)
    style = "bolditalic" if bold and italic else "bold" if bold else "italic" if italic else "regular"
    advances, _ = _font_metrics(family, weight, style, size, text, fallback)
    if fallback:
        wrapped = _wrap_fallback(text, advances, box_w)
    else:
        from renderer.converter.wrap import wrap_text
        wrapped = wrap_text(text, list(advances), box_w)
    # Converter applies style runs after wrapping, with inclusive end offsets.
    styles = [(bold, italic, el.get("color", "rgba(0,0,0,1)")) for _ in text]
    for run in el.get("style_runs") or []:
        for i in range(max(0, int(run.get("start", 0))), min(len(text), int(run.get("end", len(text)-1)) + 1)):
            b, it, color = styles[i]
            styles[i] = (str(run["font_weight"]).lower() in bold_weights if "font_weight" in run else b,
                         run.get("font_style", run.get("italic")) in ("italic", True) if "font_style" in run or "italic" in run else it,
                         run.get("color", color))
    spacing = _num(el.get("letter_spacing"))
    widths, heights, offset = [], [], 0
    for line in wrapped.split("\n"):
        line_w, line_h = 0.0, 0.0
        for (b, it, _), indices in groupby(range(offset, offset + len(line)), key=lambda i: styles[i]):
            indices = list(indices)
            span = wrapped[indices[0]:indices[-1]+1]
            if el.get("capitalize"):
                span = span.upper()
            st = "bolditalic" if b and it else "bold" if b else "italic" if it else "regular"
            ws, h = _font_metrics(family, "bold" if b else "regular", st, max(1, int(size)), span, fallback)
            line_w += sum(ws) + spacing * max(0, len(ws)-1)
            line_h = max(line_h, h)
        widths.append(line_w)
        heights.append(line_h)
        offset += len(line) + 1
    step = size * (_num(el.get("line_height"), 1.2) or 1.0)
    return len(widths), max(widths), max(i * step + h for i, h in enumerate(heights))


def _measure_text(el: dict[str, Any], font_size: float, box_w: float) -> tuple[int, float, float]:
    """Renderer wrap parity; drawn span advances include integer sizing and letter spacing.

    Height is the font-metric envelope, not the tight visible-pixel bounding box.
    Pillow is an approximate offline fallback when renderer imports are unavailable.
    """
    import json
    keys = ("content", "font_family", "font_weight", "font_style", "italic", "color", "style_runs", "letter_spacing", "line_height", "capitalize")
    payload = json.dumps({k: el[k] for k in keys if k in el}, sort_keys=True)
    return _measure_cached(payload, max(1.0, _num(font_size, 1)), max(0.0, _num(box_w)), _font_manager() is None)


def _text_report(el, box, size, floor=None):
    lines, width, height = _measure_text(el, size, box.w)
    limit = el.get("max_lines")
    return TextFit(size, lines, width, height,
                   width <= box.w + 1e-7 and height <= box.h + 1e-7 and (limit is None or lines <= limit),
                   size < (TUNABLES["fit_default_floor"] if floor is None else floor))


def _fit_text(el: dict[str, Any], box: Box, size_cap: float) -> TextFit:
    """Largest WHOLE font size <= size_cap that fits. The renderer draws glyphs at int(font_size), so only whole sizes
    keep the measured wrap and height equal to what is drawn."""
    cap = max(1, int(_num(size_cap, 1)))
    top = _text_report(el, box, cap)
    if top.fits:
        return top
    best = _text_report(el, box, 1)
    if not best.fits:
        return best
    low, high = 1, cap          # low always fits, high never does
    while high - low > 1:
        middle = (low + high) // 2
        if _text_report(el, box, middle).fits:
            low = middle
        else:
            high = middle
    return _text_report(el, box, low)


def _aligned_box(slot, width, height, align):
    horizontal, vertical = align
    x = slot.x if horizontal == "left" else slot.right-width if horizontal == "right" else slot.cx-width/2
    y = slot.y if vertical == "top" else slot.bottom-height if vertical == "bottom" else slot.cy-height/2
    return Box(x, y, width, height)


def _fit_image(el: dict[str, Any], slot: Box, align: tuple[str, str]) -> Box:
    w, h = _num(el.get("width")), _num(el.get("height"))
    if min(w, h, slot.w, slot.h) <= 0:
        raise ValueError("Image and slot dimensions must be positive")
    scale = min(slot.w/w, slot.h/h)
    return _aligned_box(slot, w*scale, h*scale, align)


def _set_box(el, box):
    el.update(x=box.x, y=box.y, width=box.w, height=box.h)


def _element_box(el):
    return Box(*(_num(el.get(k)) for k in ("x", "y", "width", "height")))


def _scale_unit(analysis, unit, slot, align):
    group = _fit_image({"width": unit.src_box.w, "height": unit.src_box.h}, slot, align)
    scale = group.w / unit.src_box.w
    out = {}
    for eid in unit.member_ids:
        el = dict(analysis.elements[eid])
        rel = unit.rel[eid]
        _set_box(el, Box(group.x+rel.x*scale, group.y+rel.y*scale, rel.w*scale, rel.h*scale))
        if el["type"] == "text":
            el["font_size"] = max(1.0, _num(el.get("font_size"), 1)*scale)
        out[eid] = el
    return out


def _place_fitted_text(el, box, fit, align=("center", "middle")):
    el["font_size"] = fit.font_size
    if not fit.fits:
        _set_box(el, box)  # Keep honest allocation; the report records the overflow.
        return
    height = max(1.0, fit.text_h)
    # Shrink about the existing center for rotated text: preserve the rotation pivot.
    placement = _aligned_box(box, box.w, height, ("center", "middle") if el.get("rotation") else align)
    _set_box(el, placement)


def _fit_unit(analysis, unit, slot, align, text_scale):
    if len(unit.member_ids) > 1 or unit.rotation:
        out = _scale_unit(analysis, unit, slot, align)
        for eid, el in out.items():
            if el["type"] != "text":
                continue
            allocation = _element_box(el)
            fit = _fit_text(el, allocation, el["font_size"])
            # For unrotated labels, keep source insets but optionally widen their
            # containing shape within the slot when the readability floor is missed.
            source_label = _element_box(analysis.elements[eid])
            containers = [sid for sid in unit.member_ids if analysis.elements[sid]["type"] == "shape"
                          and _element_box(analysis.elements[sid]).contains(source_label, 0.5)]
            if containers and not unit.rotation and fit.below_floor and not unit.has_image:
                sid = min(containers, key=lambda i: _element_box(analysis.elements[i]).area)
                shape = _element_box(out[sid])
                # Only widen a simple shape+label pair, avoiding sibling collisions.
                if len(unit.member_ids) == 2:
                    left, right = max(0, allocation.x-shape.x), max(0, shape.right-allocation.right)
                    expanded = Box(slot.x+left, allocation.y, max(0, slot.w-left-right), allocation.h)
                    wider = _fit_text(el, expanded, el["font_size"])
                    if wider.fits and wider.font_size > fit.font_size:
                        _set_box(out[sid], Box(slot.x, shape.y, slot.w, shape.h))
                        allocation, fit = expanded, wider
            _place_fitted_text(el, allocation, fit)
        return out
    eid = unit.member_ids[0]
    el = dict(analysis.elements[eid])
    if el["type"] == "text":
        fit = _fit_text(el, slot, _num(el.get("font_size"), 1)*text_scale)
        _place_fitted_text(el, slot, fit, align)
    elif el["type"] == "image":
        _set_box(el, _fit_image(el, slot, align))
    else:
        _set_box(el, slot)
    return {eid: el}


def _enforce_hierarchy(analysis, fits: FittedCandidate) -> None:
    """Lower sizes only; iterate sibling equality and source order to a fixed point."""
    ids = [i for i in analysis.order if fits.elements[i]["type"] == "text"]
    siblings = {}
    for unit in analysis.units.values():
        if unit.sibling_set is not None:
            siblings.setdefault(unit.sibling_set, []).extend(i for i in unit.member_ids if i in ids)
    sizes = {i: fits.elements[i]["font_size"] for i in ids}
    for _ in range(len(ids)+1):
        before = dict(sizes)
        for members in siblings.values():
            smallest = min(sizes[i] for i in members)
            for i in members:
                sizes[i] = smallest
        for i in ids:
            larger = [sizes[j] for j in ids if _num(analysis.elements[j].get("font_size")) > _num(analysis.elements[i].get("font_size"))]
            if larger:
                sizes[i] = min(sizes[i], min(larger))
        if sizes == before:
            break
    for i, size in sizes.items():
        el = fits.elements[i]
        if size != el["font_size"]:
            box = _element_box(el)
            report = _fit_text(el, box, size)
            _place_fitted_text(el, box, report)


def fit_candidate(analysis: SourceAnalysis, cand: Candidate, target: dict[str, Any]) -> FittedCandidate:
    """Fit all units; expose failures in notes/TextFit instead of aborting a batch.

    This is fitting, not validation or selection: Dev 3 must reject collisions,
    impossible tiny allocations, and any fit with errors before selecting it.
    """
    out = FittedCandidate(cand, {}, {}, {}, list(cand.notes))
    canvas = Box(0, 0, max(1, _num(target.get("width"), 1)), max(1, _num(target.get("height"), 1)))
    if _font_manager() is None:
        out.notes.append("Text measurement uses approximate Pillow fallback")
    for uid, unit in analysis.units.items():
        try:
            if uid in analysis.backgrounds:
                members = {i: dict(analysis.elements[i]) for i in unit.member_ids}
                for el in members.values():
                    _set_box(el, canvas)
            elif uid in analysis.decorations:
                members = _scale_unit(analysis, unit, cand.deco[uid], ("center", "middle"))
            else:
                members = _fit_unit(analysis, unit, cand.slots[uid], cand.align.get(uid, ("center", "middle")), cand.scales.get(uid, cand.k))
            out.elements.update(members)
        except Exception as exc:
            out.notes.append(f"FIT ERROR {uid}: {type(exc).__name__}: {exc}")
            # Retain every element and its content even when a candidate is malformed.
            try:
                out.elements.update(_scale_unit(analysis, unit, canvas, ("center", "middle")))
            except Exception:
                for i in unit.member_ids:
                    el = dict(analysis.elements[i])
                    out.elements[i] = el  # Invalid source geometry stays visible to validation.
    try:
        _enforce_hierarchy(analysis, out)
    except Exception as exc:
        out.notes.append(f"FIT ERROR hierarchy: {exc}")
    try:
        floors = readability_floors(analysis, target)  # Dev 1's full floors, not the candidate's relaxed rung.
    except Exception:
        floors = {}
    for uid, unit in analysis.units.items():
        out.used[uid] = _union([_rotated_aabb(out.elements[i]) for i in unit.member_ids])
        for i in unit.member_ids:
            el = out.elements[i]
            if el["type"] == "text":
                try:
                    report = _text_report(el, _element_box(el), el["font_size"], floors.get(uid, TUNABLES["fit_default_floor"]))
                except Exception as exc:
                    out.notes.append(f"FIT ERROR text {i}: {exc}")
                    report = TextFit(_num(el.get("font_size"), 1), 0, 0, 0, False, True)
                out.text[i] = report
                if not report.fits:
                    out.notes.append(f"Text overflow: {i}")
                if report.below_floor:
                    out.notes.append(f"Below readability floor: {i} ({report.font_size:.2f}px)")
    out.elements = {i: out.elements[i] for i in analysis.order}
    return out


# =====================================================================================
# §1 Source analysis (Dev 1)
# Units, background / decoration / content classification, furniture flags, reading blocks and source
# relationships. Geometry only: nothing branches on ids, asset names, content strings or canvas sizes.
# =====================================================================================

TUNABLES.update({
    "bg_cover_min": 0.90,            # source-canvas coverage that makes a lowest-z image/shape a background
    "bg_cover_min_cover_fit": 0.60,  # ... for `fit: cover` images
    "deco_alpha_max": 0.50,          # effective alpha (opacity x fill alpha) at or below which a shape-only unit is decoration
    "deco_over_content_min": 0.20,   # opaque shape-only unit under content, covering this share of it => decoration
    "contain_max_area": 0.30,        # implicit container: opaque shape below this canvas share ...
    "contain_min_inside": 0.90,      # ... containing this share of a higher-z text's ink box
    "block_overlap_min": 0.50,       # cross-axis ink overlap (share of the smaller extent) that joins two units in a block
    "edge_near": 0.06,               # "hugs an edge": ink within this share of the canvas dimension
    "corner_area_max": 0.03,         # corner furniture: unit area share of the canvas
    "band_min_width": 0.30,          # thin header/footer strip: ink width share of the canvas ...
    "band_max_height": 0.04,         # ... and ink height share
    "sibling_size_tol": 0.20,
    "overlap_min": 0.02,             # smaller-box share below which a source overlap is ignored
    "use_asset_ink": True,           # read asset alpha bounding boxes (read-only) for ink boxes
    "ink_alpha_min": 24,             # alpha (0-255) above which a pixel counts as ink
})

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "brand_kit" / "assets"
_INK_CACHE: dict[str, tuple[float, float, float, float, int, int] | None] = {}


def _z(el: dict[str, Any]) -> int:
    return int(_num(el.get("z_index")))


def _rgba_alpha(color: Any) -> float:
    m = re.match(r"\s*rgba\(([^)]*)\)", str(color or ""))
    parts = m.group(1).split(",") if m else []
    return min(1.0, max(0.0, _num(parts[3], 1.0))) if len(parts) == 4 else 1.0


def _eff_alpha(el: dict[str, Any]) -> float:
    """Visual weight. Shapes multiply opacity into fill alpha (R10); text uses colour alpha only (the renderer ignores
    text opacity); images use opacity."""
    if el.get("type") == "text":
        return _rgba_alpha(el.get("color"))
    op = min(1.0, max(0.0, _num(el.get("opacity"), 1.0)))
    return op * _rgba_alpha(el.get("fill_color")) if el.get("type") == "shape" else op


def _asset_ink(asset_id: str) -> tuple[float, float, float, float, int, int] | None:
    """Alpha bounding box of an asset as fractions of its pixels (x0, y0, x1, y1) + pixel size. Read-only; None when the
    file is missing, unreadable, opaque or empty (callers then use the whole box)."""
    if asset_id not in _INK_CACHE:
        res = None
        try:
            from PIL import Image
            with Image.open(_ASSETS_DIR / Path(asset_id).name) as im:
                iw, ih = im.size
                if "A" in im.getbands():
                    thr = TUNABLES["ink_alpha_min"]
                    bb = im.getchannel("A").point(lambda v: 255 if v > thr else 0).getbbox()
                    if bb:
                        res = (bb[0] / iw, bb[1] / ih, bb[2] / iw, bb[3] / ih, iw, ih)
        except Exception:
            res = None
        _INK_CACHE[asset_id] = res
    return _INK_CACHE[asset_id]


def _image_ink(el: dict[str, Any]) -> Box:
    """Visible extent of an image element: the asset's alpha bbox mapped through `fit` into the element box."""
    box = _rotated_aabb(el)
    info = _asset_ink(str(el.get("asset_id", ""))) if TUNABLES["use_asset_ink"] and not _num(el.get("rotation")) else None
    if info is None or box.w <= 0 or box.h <= 0:
        return box
    x0, y0, x1, y1, iw, ih = info
    fit = el.get("fit", "contain")
    if fit == "fill":
        rect = box
    else:
        sc = (max if fit == "cover" else min)(box.w / iw, box.h / ih)
        rect = Box(box.x + (box.w - iw * sc) / 2, box.y + (box.h - ih * sc) / 2, iw * sc, ih * sc)
    ink = Box(rect.x + x0 * rect.w, rect.y + y0 * rect.h, (x1 - x0) * rect.w, (y1 - y0) * rect.h)
    cx0, cy0 = max(ink.x, box.x), max(ink.y, box.y)
    cx1, cy1 = min(ink.right, box.right), min(ink.bottom, box.bottom)
    return Box(cx0, cy0, cx1 - cx0, cy1 - cy0) if cx1 > cx0 and cy1 > cy0 else box


def _text_ink(el: dict[str, Any]) -> Box:
    """Measured wrapped text extent at source size, positioned per `text_align` (the renderer ignores the box height)."""
    if _num(el.get("rotation")):
        return _rotated_aabb(el)
    x, y, w = _num(el.get("x")), _num(el.get("y")), max(1.0, _num(el.get("width"), 1.0))
    _, tw, th = _measure_text(el, max(1, int(_num(el.get("font_size"), 16.0))), w)
    align = el.get("text_align", "left")
    return Box(x + ((w - tw) / 2 if align == "center" else w - tw if align == "right" else 0.0), y, tw, th)


def _member_ink(el: dict[str, Any], canvas_area: float) -> Box:
    t = el.get("type")
    if t == "text":
        return _text_ink(el)
    if t == "image" and _rotated_aabb(el).area < 0.5 * canvas_area:   # backgrounds are never ink-tightened
        return _image_ink(el)
    return _rotated_aabb(el)


def _containers(els: list[dict[str, Any]], grouped: set[int], cw: int, ch: int) -> dict[int, int]:
    """Implicit containment (docs/engine.md §5): ungrouped text index -> index of the SMALLEST opaque, non-bleeding shape below
    it that contains it. A pill and its label with no group_id therefore still travel together."""
    cands = []
    for i, e in enumerate(els):
        if i in grouped or e.get("type") != "shape" or e.get("shape_type") == "line" or _num(e.get("rotation")):
            continue
        b = _rotated_aabb(e)
        if (_eff_alpha(e) <= TUNABLES["deco_alpha_max"] or b.area >= TUNABLES["contain_max_area"] * cw * ch
                or b.x < -0.5 or b.y < -0.5 or b.right > cw + 0.5 or b.bottom > ch + 0.5):
            continue
        cands.append((i, b))
    owner: dict[int, int] = {}
    for j, e in enumerate(els):
        if j in grouped or e.get("type") != "text" or _num(e.get("rotation")):
            continue
        ink = _text_ink(e)
        best = None
        for i, b in cands:
            if (_z(els[i]), i) < (_z(e), j) and b.intersect(ink) >= TUNABLES["contain_min_inside"] * ink.area > 0 \
                    and (best is None or b.area < best[1]):
                best = (i, b.area)
        if best:
            owner[j] = best[0]
    return owner


def _make_unit(uid: str, members: list[dict[str, Any]], canvas_area: float) -> LayoutUnit:
    src = _union([_rotated_aabb(m) for m in members])
    texts = [m for m in members if m.get("type") == "text"]
    return LayoutUnit(
        uid=uid,
        member_ids=[m["id"] for m in members],
        kind="content",
        src_box=src,
        ink_box=_union([_member_ink(m, canvas_area) for m in members]),
        rel={m["id"]: Box(_num(m.get("x")) - src.x, _num(m.get("y")) - src.y,
                          _num(m.get("width")), _num(m.get("height"))) for m in members},
        rotation=next((r for r in (_num(m.get("rotation")) for m in members) if r), 0.0),
        has_text=bool(texts),
        has_image=any(m.get("type") == "image" for m in members),
        has_shape=any(m.get("type") == "shape" for m in members),
        aspect_locked=False,
        fs_max=max((_num(m.get("font_size")) for m in texts), default=0.0),
    )


def _build_units(els: list[dict[str, Any]], cw: int, ch: int) -> dict[str, LayoutUnit]:
    """One unit per non-null `group_id`, one per implicit container with its labels, one per other element."""
    gid = [e.get("group_id") if e.get("group_id") not in (None, "") else None for e in els]
    owner = _containers(els, {i for i, g in enumerate(gid) if g is not None}, cw, ch)
    owners = set(owner.values())
    groups: dict[Any, list[int]] = {}
    for i in range(len(els)):
        key = (("g", gid[i]) if gid[i] is not None else ("c", owner[i]) if i in owner
               else ("c", i) if i in owners else ("e", i))
        groups.setdefault(key, []).append(i)
    units = {}
    for key, idxs in groups.items():
        uid = ("g" if key[0] == "g" else "e") + str(idxs[0])
        units[uid] = _make_unit(uid, [els[i] for i in idxs], cw * ch)
    return units


def _classify_units(an: SourceAnalysis) -> None:
    """background | decoration | content, plus aspect_locked. Coverage and alpha rules of docs/engine.md §5."""
    T = TUNABLES
    cv = Box(0, 0, an.canvas_w, an.canvas_h)
    first = lambda u: int(u.uid[1:])
    paint = lambda u: (min(_z(an.elements[i]) for i in u.member_ids), first(u))
    units = sorted(an.units.values(), key=paint)

    def is_bg(u: LayoutUnit) -> bool:
        if len(u.member_ids) != 1 or u.has_text:
            return False
        e = an.elements[u.member_ids[0]]
        cover = cv.intersect(u.src_box) / (an.canvas_w * an.canvas_h)
        return e.get("shape_type") != "line" and (cover >= T["bg_cover_min"] or (
            e.get("type") == "image" and e.get("fit") == "cover" and cover >= T["bg_cover_min_cover_fit"]))

    for u in units:            # the unbroken run of full-bleed units at the bottom of the stack (stacked backgrounds)
        if not is_bg(u):
            break
        u.kind = "background"
    shape_only = lambda u: u.has_shape and not u.has_text and not u.has_image
    alpha = lambda u: max(_eff_alpha(an.elements[i]) for i in u.member_ids)
    live = [u for u in units if u.kind != "background"]
    for u in live:
        b = u.src_box
        bleeds = b.x < -0.5 or b.y < -0.5 or b.right > an.canvas_w + 0.5 or b.bottom > an.canvas_h + 0.5
        if shape_only(u) and (alpha(u) <= T["deco_alpha_max"] or bleeds):
            u.kind = "decoration"
    content = [u for u in live if u.kind == "content"]
    under = [u for u in content if shape_only(u) and any(      # opaque shape sitting under content it overlaps
        v is not u and paint(v) > paint(u) and u.src_box.intersect(v.ink_box) >= T["deco_over_content_min"] * v.ink_box.area > 0
        for v in content)]
    for u in under:
        u.kind = "decoration"
    for u in an.units.values():
        u.aspect_locked = (u.has_image and u.kind != "background") or any(
            _num(an.elements[i].get("rotation")) for i in u.member_ids)
    for kind, bucket in (("background", an.backgrounds), ("decoration", an.decorations), ("content", an.content)):
        bucket[:] = [u.uid for u in sorted(an.units.values(), key=first) if u.kind == kind]


def _reading_blocks(an: SourceAnalysis) -> tuple[list[list[str]], str]:
    """Cluster content units into rows (vertical flow) or columns (horizontal flow) by cross-axis ink overlap; the axis
    yielding more blocks wins (ties: vertical). Blocks are ordered along the flow axis, units across it."""
    cont = [an.units[uid] for uid in an.content]

    def cluster(axis: str) -> list[list[LayoutUnit]]:
        ext = [(u.ink_box.y, u.ink_box.bottom) if axis == "y" else (u.ink_box.x, u.ink_box.right) for u in cont]
        parent = list(range(len(cont)))

        def find(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for i in range(len(cont)):
            for j in range(i + 1, len(cont)):
                small = min(ext[i][1] - ext[i][0], ext[j][1] - ext[j][0])
                if small > 0 and min(ext[i][1], ext[j][1]) - max(ext[i][0], ext[j][0]) >= TUNABLES["block_overlap_min"] * small:
                    parent[find(j)] = find(i)
        groups: dict[int, list[int]] = {}
        for i in range(len(cont)):
            groups.setdefault(find(i), []).append(i)
        start = (lambda i: (ext[i][0], ext[i][1])), (lambda i: (cont[i].ink_box.x, cont[i].ink_box.y) if axis == "y"
                                                       else (cont[i].ink_box.y, cont[i].ink_box.x))
        blocks = sorted(groups.values(), key=lambda g: (min(ext[i][0] for i in g), min(g)))
        return [[cont[i] for i in sorted(g, key=start[1])] for g in blocks]

    rows, cols = cluster("y"), cluster("x")
    horizontal = len(cols) > len(rows)
    blocks = cols if horizontal else rows
    for bi, blk in enumerate(blocks):
        for u in blk:
            u.block = bi
    return [[u.uid for u in blk] for blk in blocks], "horizontal" if horizontal else "vertical"


def _source_relationships(an: SourceAnalysis) -> None:
    """src_overlaps, h_align, edge_anchor, band, sibling_set, prominence, margin_frac (all from ink boxes)."""
    T = TUNABLES
    cont = [an.units[uid] for uid in an.content]
    W, H = an.canvas_w, an.canvas_h
    if not cont:
        return
    for i, a in enumerate(cont):
        for b in cont[i + 1:]:
            small = min(a.ink_box.area, b.ink_box.area)
            frac = a.ink_box.intersect(b.ink_box) / small if small > 0 else 0.0
            if frac >= T["overlap_min"]:
                an.src_overlaps[frozenset((a.uid, b.uid))] = round(frac, 6)
    near = T["edge_near"]
    for u in cont:                       # furniture flags depend only on the unit's own ink box
        b, sb = u.ink_box, u.src_box
        dt, db, dl, dr = min(b.y, sb.y), H - max(b.bottom, sb.bottom), min(b.x, sb.x), W - max(b.right, sb.right)
        v = "top" if dt <= near * H and dt <= db else "bottom" if db <= near * H else None
        h = "left" if dl <= near * W and dl <= dr else "right" if dr <= near * W else None
        if v and b.w >= T["band_min_width"] * W and b.h <= T["band_max_height"] * H:
            u.band = v
        elif v and h and b.area <= T["corner_area_max"] * W * H:
            u.edge_anchor = (v, h)
    body = [u for u in cont if u.edge_anchor is None and u.band is None] or cont   # furniture must not define the alignment lines
    bbox = _union([u.ink_box for u in body])
    for u in cont:
        b = u.ink_box
        d = {"left": abs(b.x - bbox.x), "right": abs(b.right - bbox.right),
             "center": min(abs(b.cx - bbox.cx), abs(b.cx - W / 2))}
        u.h_align = min(("center", "left", "right"), key=lambda a: d[a])
    sig = lambda u: (u.has_text, u.has_image, u.has_shape, len(u.member_ids))
    tol = T["sibling_size_tol"]
    similar = lambda a, b: (abs(a.src_box.w - b.src_box.w) <= tol * max(a.src_box.w, b.src_box.w)
                            and abs(a.src_box.h - b.src_box.h) <= tol * max(a.src_box.h, b.src_box.h))
    next_set, taken = 0, set()
    for blk in an.blocks:
        us = [an.units[uid] for uid in blk]
        for i, a in enumerate(us):
            fam = [a] + [b for b in us[i + 1:] if b.uid not in taken and sig(b) == sig(a) and similar(a, b)]
            if a.uid not in taken and len(fam) > 1:
                for m in fam:
                    m.sibling_set = next_set
                    taken.add(m.uid)
                next_set += 1
    areas = sorted((u.src_box.area for u in cont), reverse=True)
    max_fs = max((u.fs_max for u in cont), default=0.0)
    max_img = max((u.src_box.area for u in cont if u.has_image and not u.has_text), default=0.0)
    for u in cont:
        share = 1.0 - areas.index(u.src_box.area) / (len(cont) - 1) if len(cont) > 1 else 1.0
        weight = u.fs_max / max_fs if u.has_text and max_fs > 0 else (
            u.src_box.area / max_img if u.has_image and max_img > 0 else 0.0)
        u.prominence = round(0.5 * share + 0.5 * weight, 6)
    m = min(bbox.x, bbox.y, W - bbox.right, H - bbox.bottom)
    an.margin_frac = min(0.25, max(0.0, m) / min(W, H))


def analyze_source(source: dict[str, Any]) -> SourceAnalysis:
    """Pure: never mutates `source`. Expects sanitized numerics (resize() sanitizes first)."""
    canvas = source.get("canvas") or {}
    cw, ch = max(1, int(_num(canvas.get("width"), 1.0))), max(1, int(_num(canvas.get("height"), 1.0)))
    els = list(source.get("elements") or [])
    an = SourceAnalysis(cw, ch, {e["id"]: e for e in els}, [e["id"] for e in els], _build_units(els, cw, ch))
    _classify_units(an)
    an.blocks, an.flow_axis = _reading_blocks(an)
    _source_relationships(an)
    return an


# =====================================================================================
# §2 Candidate generation (Dev 1)
# `anchor` keeps the composition; `lanes` reflows reading-order blocks into 1..4 vertical lanes at the largest common
# scale. Corner furniture is pinned into the outermost lanes, header/footer strips are reserved bands.
# Slots are what Dev 2 fits into: rigid units get their exact scaled size, text units the rectangle a block needs.
# =====================================================================================

TUNABLES.update({
    "margin_min": 0.03, "margin_max": 0.06,       # target margin = source margin_frac clamped to this range (>= 4 px)
    "gap_frac": 0.025, "lane_gap_mult": 2.0,      # gaps (share of the short side, >= 4 px); lane gutter = mult * gap
    "near_identity": 0.03,                        # |ln(AR_target/AR_source)| below this: anchor only
    "lanes_min_ratio": 1.15, "stack_max_ratio": 0.87,   # AR ratio above / below which lanes / a single stack are generated
    "max_lanes": 4, "max_candidates": 64, "exhaustive_atoms": 8,
    "rungs": (1.0, 0.85, 0.7, 0.55, 0.4, 0.25),   # readability-floor ladder (the engine's only discontinuity)
    "rung_keep": 0.6,                             # a rung is kept while its common scale stays >= this share of the floor-free one
    "floor_px": ((90, (8, 7, 5)), (360, (10, 8, 7)), (720, (12, 10, 8))),   # judge-px floors (top, label, footer) by short side
    "floor_regular_bonus": 1, "floor_lowalpha_bonus": 2,
    "text_slack_w": 0.96, "text_slack_h": 1.0,    # fit slack handed to Dev 2: text <= 96% of the box width, +1 px of height
    "text_cap_w": 1.25,                           # a text block never grows beyond this multiple of its scaled source width
    "bind_ratio": 1.2,                            # consecutive text-only blocks with font sizes this far apart stay in one lane
    "deco_bleed": False,                          # keep decoration bleed (docs/engine.md Q1); False clamps decorations inside
    "k_iters": 14, "k_min": 0.004, "lane_boost": 2.0,   # per-lane growth beyond the common scale
})


def readability_floors(an: SourceAnalysis, target: dict[str, Any]) -> dict[str, float]:
    """Readability floor per text content unit in TARGET px at ladder rung 1 (judge-px floors / render downscale).
    Public: candidate generation relaxes these per rung; fitting reports and validation always use them in full."""
    T = TUNABLES
    TW, TH = max(1, int(_num(target.get("width"), 1.0))), max(1, int(_num(target.get("height"), 1.0)))
    units = [an.units[u] for u in an.content]
    ss, jp = min(TW, TH), min(1.0, 2048.0 / max(TW, TH))
    pts = T["floor_px"]
    if ss <= pts[0][0]:
        tiers = pts[0][1]
    elif ss >= pts[-1][0]:
        tiers = pts[-1][1]
    else:
        i = next(i for i in range(1, len(pts)) if ss <= pts[i][0])
        t = (ss - pts[i - 1][0]) / (pts[i][0] - pts[i - 1][0])
        tiers = tuple(a + (b - a) * t for a, b in zip(pts[i - 1][1], pts[i][1]))
    top, label, foot = tiers
    labels = [u.fs_max for u in units if u.has_text and u.has_shape and not u.band]
    out: dict[str, float] = {}
    for u in units:
        if not u.has_text:
            continue
        f = foot if u.band else (top if not u.has_shape or u.fs_max >= 0.85 * max(labels) else label)
        big = max((an.elements[i] for i in u.member_ids if an.elements[i].get("type") == "text"),
                  key=lambda e: _num(e.get("font_size")))
        f += T["floor_regular_bonus"] * (big.get("font_weight", "regular") == "regular")
        f += T["floor_lowalpha_bonus"] * (_eff_alpha(big) <= 0.6)
        out[u.uid] = f
    free = sorted((u for u in units if u.uid in out and not u.has_shape and not u.band), key=lambda u: -u.fs_max)
    for i, u in enumerate(free):        # a bigger free text never gets a lower floor than a smaller one (hierarchy)
        out[u.uid] = max([out[u.uid]] + [out[v.uid] for v in free[i + 1:] if v.fs_max < u.fs_max])
    return {k: v / jp for k, v in out.items()}


class _Ctx:
    """Per-(analysis, target) constants and caches shared by every candidate."""

    def __init__(self, an: SourceAnalysis, target: dict[str, Any]):
        self.an = an
        self.TW, self.TH = max(1, int(_num(target.get("width"), 1.0))), max(1, int(_num(target.get("height"), 1.0)))
        ss = min(self.TW, self.TH)
        T = TUNABLES
        self.m = max(4.0, min(T["margin_max"], max(T["margin_min"], an.margin_frac)) * ss)
        self.g = max(4.0, T["gap_frac"] * ss)
        self.sx, self.sy = self.TW / an.canvas_w, self.TH / an.canvas_h
        self.s = min(self.sx, self.sy)
        self.ratio = (self.TW / self.TH) / (an.canvas_w / an.canvas_h)
        self.R = Box(self.m, self.m, max(1.0, self.TW - 2 * self.m), max(1.0, self.TH - 2 * self.m))
        self.units = [an.units[u] for u in an.content]
        self.pins = [u for u in self.units if u.edge_anchor]
        self.bands = [u for u in self.units if u.band]
        furniture = {u.uid for u in self.pins + self.bands}
        self.blocks = [b for b in ([an.units[u] for u in blk if u not in furniture] for blk in an.blocks) if b]
        self.floors = readability_floors(an, target)
        self.prof: dict[tuple[str, int], list[tuple[int, float, float]]] = {}

    def flex(self, u: LayoutUnit) -> bool:
        return len(u.member_ids) == 1 and self.an.units[u.uid].has_text and not u.aspect_locked and not u.rotation

    def el(self, u: LayoutUnit) -> dict[str, Any]:
        return self.an.elements[u.member_ids[0]]

    def scale(self, u: LayoutUnit, k: float, rung: float) -> float:
        f = self.floors.get(u.uid, 0.0) * rung
        return max(k, f / u.fs_max) if f and u.fs_max else k


def _profile(ctx: _Ctx, el: dict[str, Any], fs: int) -> list[tuple[int, float, float]]:
    """Wrap table of a text at integer size `fs`: [(lines, box width needed incl. slack, block height incl. slack)],
    ascending lines (descending width). Built from `_measure_text`, so it follows the renderer once Dev 2 lands."""
    key = (el["id"], fs)
    if key not in ctx.prof:
        T = TUNABLES
        n1, w1, _ = _measure_text(el, fs, 1e6)
        nm, wm, _ = _measure_text(el, fs, 1.0)
        rows: dict[int, tuple[float, float]] = {}
        for L in range(n1, nm + 1):
            lo, hi = wm, max(w1, wm)
            while hi - lo > 0.5 and _measure_text(el, fs, lo)[0] > L:
                mid = (lo + hi) / 2
                if _measure_text(el, fs, mid)[0] <= L:
                    hi = mid
                else:
                    lo = mid
            n, tw, th = _measure_text(el, fs, hi)
            rows[n] = (max(hi, tw / T["text_slack_w"]), th + T["text_slack_h"])
        ctx.prof[key] = [(n, w, h) for n, (w, h) in sorted(rows.items())]
    return ctx.prof[key]


def _need_h(prof: list[tuple[int, float, float]], w: float) -> float | None:
    return next((h for _, rw, h in prof if rw <= w + 1e-6), None)


def _map_axis(c: float, S: float, T: float, ks: float) -> float:
    """Target centre coordinate for a source centre `c` on an axis (source length S, target length T). Near the
    start/end edges the offset from that edge is kept (scaled by ks); toward the middle it blends into the proportional
    map, so the result varies continuously with the target size."""
    u = c / S
    prop = u * T
    edge = c * ks if u < 0.5 else T - (S - c) * ks
    return prop + (edge - prop) * min(1.0, max(0.0, (abs(u - 0.5) - 0.10) / 0.25))


def _place_decorations(an: SourceAnalysis, target: dict[str, Any], cand: Candidate, ctx: _Ctx | None = None) -> None:
    """Decorations keep their aspect, scale by min(sx, sy), hug the same corners/edges as in the source and stay inside
    the canvas (unless TUNABLES["deco_bleed"])."""
    ctx = ctx or _Ctx(an, target)
    for uid in an.decorations:
        b = an.units[uid].src_box
        sc = min(ctx.s, ctx.TW / b.w, ctx.TH / b.h) if b.w > 0 and b.h > 0 else ctx.s
        w, h = b.w * sc, b.h * sc
        x = _map_axis(b.cx, an.canvas_w, ctx.TW, sc) - w / 2
        y = _map_axis(b.cy, an.canvas_h, ctx.TH, sc) - h / 2
        if not TUNABLES["deco_bleed"]:
            x, y = min(max(x, 0.0), ctx.TW - w), min(max(y, 0.0), ctx.TH - h)
        cand.deco[uid] = Box(x, y, w, h)


# ----- anchor ---------------------------------------------------------------------------------------------------

def _candidate_anchor(an: SourceAnalysis, target: dict[str, Any], ctx: _Ctx | None = None) -> Candidate:
    """Keep the composition: normalised centres, uniform scale s = min(sx, sy) (lifted per unit to its readability
    floor), corner furniture hugging its corner, then a push-apart along the flow axis. The highest floor rung that
    gives a clean layout wins; rung 0 (plain uniform scale) is always returned as the last resort."""
    ctx = ctx or _Ctx(an, target)
    for rung in TUNABLES["rungs"] + (0.0,):
        cand = _anchor_try(ctx, rung, strict=rung > 0)
        if cand:
            _place_decorations(an, target, cand, ctx)
            return cand
    raise AssertionError("unreachable: rung 0 never fails")


def _anchor_try(ctx: _Ctx, rung: float, strict: bool) -> Candidate | None:
    an, R, m, g = ctx.an, ctx.R, ctx.m, ctx.g
    cap = max(ctx.sx, ctx.sy)
    slots: dict[str, Box] = {}
    scales: dict[str, float] = {}
    for u in ctx.units:
        ks = min(ctx.scale(u, ctx.s, rung), cap)
        b, ink = u.src_box, u.ink_box
        if ctx.flex(u):
            prof = _profile(ctx, ctx.el(u), max(1, int(u.fs_max * ks + 1e-6)))
            w = min(R.w, max(b.w * ctx.sx, prof[-1][1]))
            h = _need_h(prof, w)
            if h is None:
                w, h = prof[-1][1], prof[-1][2]
        else:
            w, h = b.w * ks, b.h * ks
        if u.edge_anchor:
            v, hh = u.edge_anchor
            x = max(m, b.x * ks) if hh == "left" else ctx.TW - max(m, (an.canvas_w - b.right) * ks) - w
            y = max(m, b.y * ks) if v == "top" else ctx.TH - max(m, (an.canvas_h - b.bottom) * ks) - h
        elif u.band:
            x = _map_axis(b.cx, an.canvas_w, ctx.TW, ks) - w / 2
            y = max(m, b.y * ks) if u.band == "top" else ctx.TH - max(m, (an.canvas_h - b.bottom) * ks) - h
        else:
            x = _map_axis(b.cx, an.canvas_w, ctx.TW, ks) - w / 2
            y = _map_axis(ink.cy if ctx.flex(u) else b.cy, an.canvas_h, ctx.TH, ks) - h / 2
        slots[u.uid] = Box(min(max(x, m), max(m, ctx.TW - m - w)), min(max(y, m), max(m, ctx.TH - m - h)), w, h)
        scales[u.uid] = ks
    horiz = an.flow_axis == "horizontal"
    ax = (lambda bx: bx.x) if horiz else (lambda bx: bx.y)
    order = sorted(slots, key=lambda uid: (ax(slots[uid]), uid))
    for i, uid in enumerate(order):         # push later units past earlier ones they newly overlap
        for prev in order[:i]:
            a, b = slots[prev], slots[uid]
            if a.intersect(b) > 0 and frozenset((prev, uid)) not in an.src_overlaps:
                slots[uid] = b.translated(a.right + g - b.x, 0) if horiz else b.translated(0, a.bottom + g - b.y)
    for uid, b in slots.items():
        if strict:
            if b.x < m - 0.5 or b.y < m - 0.5 or b.right > ctx.TW - m + 0.5 or b.bottom > ctx.TH - m + 0.5:
                return None
        else:                                   # last resort: shrink to the canvas and slide inside it (overlaps allowed)
            f = min(1.0, ctx.TW / b.w, ctx.TH / b.h)
            b = Box(b.x, b.y, b.w * f, b.h * f)
            slots[uid] = Box(min(max(b.x, 0.0), ctx.TW - b.w), min(max(b.y, 0.0), ctx.TH - b.h), b.w, b.h)
            scales[uid] *= f
    align = {u.uid: ((u.edge_anchor[1] if u.edge_anchor else u.h_align), (u.edge_anchor[0] if u.edge_anchor else "middle"))
             for u in ctx.units}
    return Candidate("anchor", slots, {}, align, k=ctx.s, scales=scales, lanes=0, rung=rung,
                     pinned=[u.uid for u in ctx.pins])


# ----- lanes ----------------------------------------------------------------------------------------------------

def _unit_dem(ctx: _Ctx, u: LayoutUnit, ks: float, w: float, multi: bool) -> tuple[float, float] | None:
    """Slot (width, height) of a unit at scale ks in a lane `w` wide, or None when it cannot fit."""
    if ctx.flex(u):
        prof = _profile(ctx, ctx.el(u), max(1, int(u.fs_max * ks + 1e-6)))
        cap = max(u.src_box.w * ks * TUNABLES["text_cap_w"], prof[-1][1])
        if multi:
            cap = min(cap, max(prof[0][1], prof[-1][1]))
        aw = min(w, cap)
        h = _need_h(prof, aw)
        return None if h is None else (aw, h)
    sw, sh = u.src_box.w * ks, u.src_box.h * ks
    return (sw, sh) if sw <= w + 1e-6 else None


def _item_layout(ctx: _Ctx, item: list[LayoutUnit], scale: Any, w: float, align: tuple[str, str] | None = None
                 ) -> tuple[float, list[tuple[str, float, float, float, float]]] | None:
    """Lay one reading block's units side by side (wrapping onto rows) in width `w`: (height, [(uid, x, y, sw, sh)])."""
    g = ctx.g
    dems = []
    for u in item:
        d = _unit_dem(ctx, u, scale(u), w, len(item) > 1)
        if d is None:
            return None
        dems.append((u, d))
    rows: list[list[tuple[LayoutUnit, tuple[float, float]]]] = [[]]
    used = 0.0
    for u, d in dems:
        if rows[-1] and used + g + d[0] > w + 1e-6:
            rows.append([])
            used = 0.0
        used += d[0] + (g if rows[-1] else 0.0)
        rows[-1].append((u, d))
    y, out = 0.0, []
    for row in rows:
        rw, rh = sum(d[0] for _, d in row), max(d[1] for _, d in row)
        gap = min(3 * g, (w - rw) / (len(row) - 1)) if len(row) > 1 else 0.0
        x = (w - rw - gap * (len(row) - 1)) / 2 if len(row) > 1 else 0.0
        for u, d in row:
            h_al = (align or (u.h_align, "middle"))[0]
            xx = x if len(row) > 1 else (0.0 if h_al == "left" else w - d[0] if h_al == "right" else (w - d[0]) / 2)
            out.append((u.uid, xx, y + (rh - d[1]) / 2, d[0], d[1]))
            x += d[0] + gap
        y += rh + g
    return y - g, out


def _item_widths(ctx: _Ctx, item: list[LayoutUnit], scale: Any, wmax: float) -> tuple[float, float]:
    """(minimum, natural) width of a block at the given scales."""
    g, mins, nats = ctx.g, [], []
    for u in item:
        if ctx.flex(u):
            prof = _profile(ctx, ctx.el(u), max(1, int(u.fs_max * scale(u) + 1e-6)))
            mins.append(prof[-1][1])
            nats.append(min(prof[0][1], max(u.src_box.w * scale(u) * TUNABLES["text_cap_w"], prof[-1][1])))
        else:
            mins.append(u.src_box.w * scale(u))
            nats.append(mins[-1])
    return max(mins), min(wmax, max(max(nats), sum(nats) + g * (len(nats) - 1)))


class _Lane:
    def __init__(self, top: list, mid: list, bot: list):
        self.top, self.mid, self.bot = top, mid, bot   # lists of (item, align|None)

    def items(self) -> list:
        return self.top + self.mid + self.bot


def _lane_need(ctx: _Ctx, lane: _Lane, scale: Any, w: float) -> float | None:
    hs = []
    for item, al in lane.items():
        lay = _item_layout(ctx, item, scale, w, al)
        if lay is None:
            return None
        hs.append(lay[0])
    return sum(hs) + ctx.g * (len(hs) - 1) if hs else 0.0


def _arrange(ctx: _Ctx, lanes: list[_Lane], bands: list[LayoutUnit], scale: Any
             ) -> tuple[list[tuple[str, Box, tuple[str, str]]], dict[str, float]] | None:
    """Place lanes at one set of unit scales, or None when they do not fit. Returns ([(uid, slot, align)], scales).
    Lane widths are fixed at the common scale; each lane then grows its own scale while it still fits, so a text lane
    is not held to the size of a height-limited hero lane (up to TUNABLES["lane_boost"])."""
    R, g = ctx.R, ctx.g
    out: list[tuple[str, Box, tuple[str, str]]] = []
    band_scales: dict[str, float] = {}
    top_y, bot_y = R.y, R.bottom
    for u in bands:                                     # header / footer strips span the full width
        lay = _item_layout(ctx, [u], scale, R.w)
        if lay is None:
            return None
        h, (uid, x, _, sw, sh), = lay[0], lay[1][0]
        y = top_y if u.band == "top" else bot_y - h
        out.append((uid, Box(R.x + x, y, sw, sh), (u.h_align, u.band == "top" and "top" or "bottom")))
        band_scales[uid] = scale(u)
        if u.band == "top":
            top_y += h + g
        else:
            bot_y -= h + g
    Hr = bot_y - top_y
    if Hr < 1:
        return None
    n = len(lanes)
    wid, pref = [], []
    for lane in lanes:
        its = lane.items()
        mn = max((_item_widths(ctx, it, scale, R.w)[0] for it, _ in its), default=1.0)
        nat = max((_item_widths(ctx, it, scale, R.w)[1] for it, _ in its), default=1.0)
        need = _lane_need(ctx, lane, scale, R.w)
        if mn > R.w + 1e-6 or need is None or need > Hr + 1e-6:
            return None
        lo, hi = mn, R.w
        if (_lane_need(ctx, lane, scale, lo) or Hr + 1) > Hr:
            for _ in range(10):
                mid = (lo + hi) / 2
                nd = _lane_need(ctx, lane, scale, mid)
                if nd is not None and nd <= Hr:
                    hi = mid
                else:
                    lo = mid
            lo = hi
        wid.append(lo)
        pref.append(max(lo, nat))
    gl = g * TUNABLES["lane_gap_mult"]
    if n == 1:
        wid, extra = [R.w], 0.0
    else:
        surplus = R.w - sum(wid) - gl * (n - 1)
        if surplus < -1e-6:
            return None
        want = [p - w for p, w in zip(pref, wid)]
        take = min(1.0, surplus / sum(want)) if sum(want) > 0 else 0.0
        wid = [w + d * take for w, d in zip(wid, want)]
        extra = (surplus - sum(want) * take) / (2 * n)      # outer margin 1 share, each gutter 2 shares
    x = R.x + extra
    scales: dict[str, float] = {}
    cap = max(ctx.sx, ctx.sy)
    for lane, w in zip(lanes, wid):
        sc = scale
        its = lane.items()
        if n > 1 and its:
            top = max(scale(u) for it, _ in its for u in it)
            lo, hi = 1.0, min(TUNABLES["lane_boost"], cap / top)
            for _ in range(8):
                mid = (lo + hi) / 2
                nd = _lane_need(ctx, lane, lambda u, f=mid: scale(u) * f, w)
                if nd is not None and nd <= Hr:
                    lo = mid
                else:
                    hi = mid
            sc = lambda u, f=lo: scale(u) * f
        laid = []
        for item, al in lane.items():
            laid.append(_item_layout(ctx, item, sc, w, al))
            for u in item:
                scales[u.uid] = sc(u)
        hs = [lay[0] for lay in laid]
        nt, nb = len(lane.top), len(lane.bot)
        y = top_y
        ys: list[float] = []
        for h in hs[:nt]:
            ys.append(y)
            y += h + g
        yb = bot_y
        tail: list[float] = []
        for h in reversed(hs[len(hs) - nb:] if nb else []):
            yb -= h
            tail.append(yb)
            yb -= g
        mids = hs[nt:len(hs) - nb]
        if mids:
            free = yb - y + g - (sum(mids) + g * (len(mids) - 1)) - g   # space left between the pinned ends
            gap = g + (min(2 * g, free / (len(mids) - 1) / 2) if len(mids) > 1 and free > 0 else 0.0)
            used = sum(mids) + gap * (len(mids) - 1)
            cy = y + max(0.0, (yb + g - y - g - used) / 2)
            for h in mids:
                ys.append(cy)
                cy += h + gap
        ys.extend(reversed(tail))
        for (item, al), lay, y0 in zip(lane.items(), laid, ys):
            for uid, ux, uy, sw, sh in lay[1]:
                bx = x + ux
                if al and al[0] == "left":          # corner furniture hugs the canvas corner, not its lane
                    bx = R.x
                elif al and al[0] == "right":
                    bx = R.right - sw
                out.append((uid, Box(bx, y0 + uy, sw, sh), al or (ctx.an.units[uid].h_align, "middle")))
        x += w + gl + 2 * extra
    return out, {**band_scales, **scales}


def _lanes_spec(ctx: _Ctx, groups: list[list[list[LayoutUnit]]]) -> list[_Lane]:
    """groups[i] = blocks of lane i. Corner furniture joins the outermost lanes (left -> first, right -> last)."""
    lanes = [_Lane([], [(blk, None) for blk in g], []) for g in groups]
    for u in ctx.pins:
        v, h = u.edge_anchor
        lane = lanes[0 if h == "left" else -1]
        (lane.top if v == "top" else lane.bot).append(([u], (h, v)))
    return lanes


def _search(ctx: _Ctx, lanes: list[_Lane], rung: float) -> tuple[float, list] | None:
    """Largest common scale k at which every lane fits (bisection), with its arrangement."""
    units = [u for lane in lanes for it, _ in lane.items() for u in it] + ctx.bands
    floors = {u.uid: ctx.scale(u, 0.0, rung) for u in units}

    def at(k: float):
        return _arrange(ctx, lanes, ctx.bands, lambda u: max(k, floors[u.uid]))

    hi = max(ctx.sx, ctx.sy)
    best = at(hi)
    if best is not None:
        return hi, best
    lo = TUNABLES["k_min"]
    best = at(lo)
    if best is None:
        return None
    for _ in range(TUNABLES["k_iters"]):
        mid = (lo + hi) / 2
        lay = at(mid)
        if lay is not None:
            lo, best = mid, lay
        else:
            hi = mid
    return lo, best


def _candidate_lanes(an: SourceAnalysis, target: dict[str, Any], partition: list[list[int]],
                     ctx: _Ctx | None = None, atoms: list[list[list[LayoutUnit]]] | None = None) -> Candidate | None:
    """One lane arrangement: `partition` lists, per lane, the atom indices it holds (contiguous, in reading order)."""
    ctx = ctx or _Ctx(an, target)
    atoms = atoms if atoms is not None else _atoms(ctx)
    lanes = _lanes_spec(ctx, [[blk for a in lane for blk in atoms[a]] for lane in partition])
    free = _search(ctx, lanes, 0.0)
    if free is None:
        return None
    k_free, (arr, scs), rung = free[0], free[1], 0.0
    for r in TUNABLES["rungs"]:
        got = _search(ctx, lanes, r)
        if got is not None and got[0] >= TUNABLES["rung_keep"] * k_free:
            (k_free, (arr, scs)), rung = got, r
            break
    k = k_free
    slots = {uid: b for uid, b, _ in arr}
    cand = Candidate("lanes:%d:%s" % (len(partition), "|".join(",".join(map(str, p)) for p in partition)),
                     slots, {}, {uid: al for uid, _, al in arr}, k=k, lanes=len(partition), rung=rung,
                     pinned=[u.uid for u in ctx.pins])
    cand.scales = scs
    _place_decorations(an, target, cand, ctx)
    return cand


def _atoms(ctx: _Ctx) -> list[list[list[LayoutUnit]]]:
    """Blocks bound into atoms that may not be split across lanes: consecutive text-only single-unit blocks whose font
    sizes differ (a typographic pair such as headline + subhead), up to three blocks."""
    atoms: list[list[list[LayoutUnit]]] = []
    r = TUNABLES["bind_ratio"]

    def plain(blk: list[LayoutUnit]) -> bool:
        return len(blk) == 1 and ctx.flex(blk[0])

    for blk in ctx.blocks:
        last = atoms[-1] if atoms else None
        if last and len(last) < 3 and plain(blk) and plain(last[-1]) and max(
                blk[0].fs_max, last[-1][0].fs_max) >= r * min(blk[0].fs_max, last[-1][0].fs_max):
            last.append(blk)
        else:
            atoms.append([blk])
    return atoms


def _partitions(n: int, max_lanes: int, exhaustive: int) -> list[list[list[int]]]:
    """Contiguous partitions of n atoms into 1..max_lanes lanes (all of them when n is small, else balanced ones)."""
    out = []
    for k in range(1, min(n, max_lanes) + 1):
        if n <= exhaustive:
            cut_sets = list(combinations(range(1, n), k - 1))
        else:
            base = [round(n * i / k) for i in range(1, k)]
            cut_sets = {tuple(base)}
            for i in range(len(base)):
                for d in (-1, 1):
                    c = list(base)
                    c[i] += d
                    if all(0 < a < b < n for a, b in zip([0] + c, c + [n])) and len(set(c)) == len(c):
                        cut_sets.add(tuple(sorted(c)))
            cut_sets = sorted(cut_sets)
        for cuts in cut_sets:
            idx = [0, *cuts, n]
            out.append([list(range(idx[i], idx[i + 1])) for i in range(k)])
    return out


def generate_candidates(an: SourceAnalysis, target: dict[str, Any]) -> list[Candidate]:
    """The anchor candidate (always) plus lane arrangements when the aspect ratio changes enough. Deterministic order:
    anchor, then by lane count and partition. Never raises: a failing arrangement is skipped."""
    ctx = _Ctx(an, target)
    out = [_candidate_anchor(an, target, ctx)]
    T = TUNABLES
    wide, tall = ctx.ratio >= T["lanes_min_ratio"], ctx.ratio <= T["stack_max_ratio"]
    if abs(math.log(ctx.ratio)) < T["near_identity"] or not (wide or tall) or not ctx.blocks:
        return out
    atoms = _atoms(ctx)
    for part in _partitions(len(atoms), T["max_lanes"] if wide else 1, T["exhaustive_atoms"]):
        if len(out) >= T["max_candidates"]:
            break
        try:
            cand = _candidate_lanes(an, target, part, ctx, atoms)
        except Exception:
            cand = None
        if cand is not None:
            out.append(cand)
    return out


# =====================================================================================
# §4 Validation and scoring (Dev 3)
# validate() runs the hard checks H1-H8 on the FINAL output elements and the soft metrics S1-S8 (docs/engine.md §8).
# A candidate with any hard failure is never selected as-is.
# =====================================================================================

TUNABLES.update({
    "weights": {"readability": 0.25, "collisions": 0.20, "fidelity": 0.17, "wrapping": 0.08, "hierarchy": 0.10,
                "utilization": 0.08, "spacing": 0.06, "alignment": 0.06},   # wrapping added after review; sums to 1
    "tol_px": 0.5,                    # H4 / H6 / H7 geometric tolerance
    "aspect_tol": 0.01,               # H5: non-background image aspect drift
    "util_band": (0.55, 0.75),        # S5: content coverage of the inner canvas that scores 1.0
    "collide_image_mult": 3.0,        # S2: text over a non-background image counts this many times
    "repair_tries": 3,                # failed candidates repaired (best score first) before the fallback
    "repair_shrink": 0.9, "repair_steps": 12,
})

_GEOMETRY = ("x", "y", "width", "height", "font_size")


def _text_ink_out(el: dict[str, Any]) -> Box:
    """Drawn extent of an OUTPUT text element (top-aligned, horizontally per text_align)."""
    if _num(el.get("rotation")):
        return _rotated_aabb(el)
    box = _element_box(el)
    _, tw, th = _measure_text(el, _num(el.get("font_size"), 1.0), box.w)
    align = el.get("text_align", "left")
    dx = (box.w - tw) / 2 if align == "center" else box.w - tw if align == "right" else 0.0
    return Box(box.x + dx, box.y, tw, th)


def _hard_checks(an: SourceAnalysis, fitted: FittedCandidate, tw: int, th: int) -> list[str]:
    T, tol = TUNABLES, TUNABLES["tol_px"]
    out, hard = fitted.elements, []
    if list(out) != an.order:
        hard.append("H1 element ids differ from the source")
        return hard
    hard += [n for n in fitted.notes if n.startswith("FIT ERROR")]
    canvas = Box(0, 0, tw, th)
    bg_ids = {i for uid in an.backgrounds for i in an.units[uid].member_ids}
    for i, el in out.items():
        src = an.elements[i]
        if el.get("type") != src.get("type"):
            hard.append(f"H1 type changed: {i}")
        changed = [k for k in set(src) | set(el) if k not in _GEOMETRY and src.get(k) != el.get(k)]
        if changed:
            hard.append(f"H2 fields changed on {i}: {sorted(changed)}")
        nums = [el.get(k) for k in ("x", "y", "width", "height")] + ([el.get("font_size")] if el.get("type") == "text" else [])
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in nums):
            hard.append(f"H4 non-finite geometry: {i}")
            continue
        if el["width"] < 1 or el["height"] < 1 or (el.get("type") == "text" and el["font_size"] < 1):
            hard.append(f"H4 below 1 px: {i}")
        if not canvas.contains(_rotated_aabb(el), tol):
            hard.append(f"H4 off canvas: {i}")
        if el.get("type") == "image" and i not in bg_ids and src.get("fit") != "cover":
            sa, oa = _num(src.get("width")) / max(1e-9, _num(src.get("height"))), el["width"] / max(1e-9, el["height"])
            if abs(oa / sa - 1) > T["aspect_tol"]:
                hard.append(f"H5 image aspect changed: {i}")
        if el.get("type") == "text":
            lines, w, h = _measure_text(el, el["font_size"], el["width"])
            limit = el.get("max_lines")
            if w > el["width"] + tol or h > el["height"] + tol or (limit is not None and lines > limit):
                hard.append(f"H6 text overflows its box: {i}")
    for u in an.units.values():                                         # H7 labels stay inside their shapes
        shapes = [i for i in u.member_ids if an.elements[i].get("type") == "shape"]
        for t in (i for i in u.member_ids if an.elements[i].get("type") == "text"):
            for sh in shapes:
                if _element_box(an.elements[sh]).contains(_element_box(an.elements[t]), tol) \
                        and not _element_box(out[sh]).contains(_element_box(out[t]), tol):
                    hard.append(f"H7 label left its shape: {t}")
    unit_of = {i: uid for uid, u in an.units.items() for i in u.member_ids}
    texts = [(i, _text_ink_out(el)) for i, el in out.items() if el.get("type") == "text"]
    for (a, ba), (b, bb) in combinations(texts, 2):                    # H8 text on text from different units
        if unit_of[a] != unit_of[b] and ba.intersect(bb) > 0 and frozenset((unit_of[a], unit_of[b])) not in an.src_overlaps:
            hard.append(f"H8 text overlaps text: {a} / {b}")
    return hard


def _kendall(pairs: list[tuple[float, float]]) -> float:
    """Kendall tau of (source, output) value pairs mapped to 0..1 (1 = same order). Source ties are skipped."""
    agree = total = 0
    for (s1, o1), (s2, o2) in combinations(pairs, 2):
        if s1 == s2:
            continue
        total += 1
        agree += (s1 - s2) * (o1 - o2) > 0
    return agree / total if total else 1.0


def _gap_cv(cont: list[LayoutUnit], boxes: dict[str, Box]) -> float:
    """S6 helper: coefficient of variation (capped at 1) of the gap from each unit to the nearest unit below it that
    shares some horizontal extent."""
    gaps = []
    for a in cont:
        ba = boxes[a.uid]
        below = [boxes[b.uid].y - ba.bottom for b in cont if b is not a
                 and min(ba.right, boxes[b.uid].right) > max(ba.x, boxes[b.uid].x) and boxes[b.uid].y >= ba.bottom - 1e-6]
        if below:
            gaps.append(min(below))
    if len(gaps) < 2 or sum(gaps) <= 0:
        return 0.0
    mean = sum(gaps) / len(gaps)
    return min(1.0, math.sqrt(sum((g - mean) ** 2 for g in gaps) / len(gaps)) / mean)


def _aligned_share(cont: list[LayoutUnit], boxes: dict[str, Box], tol: float) -> float:
    """S7 helper: share of units sharing a left, centre or right line with another unit."""
    if len(cont) < 2:
        return 1.0
    lines = {u.uid: (boxes[u.uid].x, boxes[u.uid].cx, boxes[u.uid].right) for u in cont}
    return sum(any(abs(p - q) <= tol for v in cont if v is not u for p, q in zip(lines[u.uid], lines[v.uid]))
               for u in cont) / len(cont)


def _soft_metrics(an: SourceAnalysis, fitted: FittedCandidate, tw: int, th: int) -> dict[str, float]:
    T, out = TUNABLES, fitted.elements
    cont = [an.units[uid] for uid in an.content]
    used = {u.uid: fitted.used[u.uid] for u in cont}
    if not cont:
        return {k: 1.0 for k in T["weights"]}
    # S1 readability: per text, 0.5 * (size / area-preserving size, capped at 1) + 0.5 * (at or above its floor),
    # weighted by the unit's prominence.
    try:
        floors = readability_floors(an, {"width": tw, "height": th})
    except Exception:
        floors = {}
    area_scale = math.sqrt((tw * th) / (an.canvas_w * an.canvas_h))
    num = den = 0.0
    for u in cont:
        for i in u.member_ids:
            if out[i].get("type") != "text":
                continue
            fs, src_fs = _num(out[i].get("font_size"), 1.0), _num(an.elements[i].get("font_size"), 1.0)
            r = 0.5 * min(1.0, fs / max(1e-9, src_fs * area_scale)) + 0.5 * (fs >= floors.get(u.uid, 0.0) - 1e-6)
            wgt = 0.2 + u.prominence
            num, den = num + wgt * r, den + wgt
    readability = num / den if den else 1.0
    # S2 unintended collisions between content units (source overlaps are intentional).
    total = sum(b.area for b in used.values()) or 1.0
    bad = 0.0
    for a, b in combinations(cont, 2):
        if frozenset((a.uid, b.uid)) in an.src_overlaps:
            continue
        ov = used[a.uid].intersect(used[b.uid])
        if ov and ((a.has_text and b.has_image) or (b.has_text and a.has_image)):
            ov *= T["collide_image_mult"]
        bad += ov
    collisions = 1.0 - min(1.0, bad / total)
    # S3 hierarchy: text order by font size, image order by area.
    tx = [(_num(an.elements[i].get("font_size")), _num(out[i].get("font_size"))) for i in an.order if out[i].get("type") == "text"]
    im = [(u.src_box.area, used[u.uid].area) for u in cont if u.has_image and not u.has_text]
    parts = [_kendall(p) for p in (tx, im) if len(p) > 1]
    hierarchy = sum(parts) / len(parts) if parts else 1.0
    # S4 composition fidelity: mean displacement of normalised unit centres.
    disp = [math.hypot(u.src_box.cx / an.canvas_w - used[u.uid].cx / tw, u.src_box.cy / an.canvas_h - used[u.uid].cy / th)
            for u in cont]
    fidelity = 1.0 - min(1.0, sum(disp) / len(disp) / math.sqrt(0.5))
    # S5 utilization: content area over the canvas, best inside the band.
    cover = min(1.0, total / (tw * th))
    lo, hi = T["util_band"]
    utilization = cover / lo if cover < lo else 1.0 if cover <= hi else max(0.0, 1.0 - (cover - hi) / (1.0 - hi))
    # S8 wrapping: prominence-weighted share of texts that wrap onto no more lines than in the source
    # (a one-line headline broken into "Chase the / Light" reads worse than a slightly smaller one-liner).
    num = den = 0.0
    for u in cont:
        for i in u.member_ids:
            src = an.elements[i]
            if src.get("type") != "text":
                continue
            n_src = _measure_text(src, _num(src.get("font_size"), 1.0), _num(src.get("width"), 1.0))[0]
            n_out = _measure_text(out[i], _num(out[i].get("font_size"), 1.0), _num(out[i].get("width"), 1.0))[0]
            wgt = 0.2 + u.prominence
            num, den = num + wgt * (n_out <= n_src), den + wgt
    wrapping = num / den if den else 1.0
    # S6 / S7 are judged against the source: a layout only loses for being less even or less aligned than the original.
    src_boxes = {u.uid: u.src_box for u in cont}
    spacing = 1.0 - max(0.0, _gap_cv(cont, used) - _gap_cv(cont, src_boxes))
    alignment = 1.0 - max(0.0, _aligned_share(cont, src_boxes, max(2.0, 0.005 * an.canvas_w))
                          - _aligned_share(cont, used, max(2.0, 0.005 * tw)))
    return {"readability": readability, "collisions": collisions, "hierarchy": hierarchy, "fidelity": fidelity,
            "utilization": utilization, "spacing": spacing, "alignment": alignment, "wrapping": wrapping}


def validate(analysis: SourceAnalysis, fitted: FittedCandidate, target: dict[str, Any]) -> Report:
    tw, th = _target_size(target)
    hard = _hard_checks(analysis, fitted, tw, th)
    try:
        soft = _soft_metrics(analysis, fitted, tw, th)
    except Exception as exc:                       # a scoring bug must not hide a valid candidate
        soft = {k: 0.0 for k in TUNABLES["weights"]}
        fitted.notes.append(f"SCORE ERROR: {type(exc).__name__}: {exc}")
    score = sum(TUNABLES["weights"][k] * v for k, v in soft.items())
    return Report(hard, soft, round(score, 6))


# =====================================================================================
# §5 Selection, repair, fallback (Dev 3)
# =====================================================================================

def _select(scored: list[tuple[FittedCandidate, Report]]) -> tuple[FittedCandidate, Report] | None:
    """Highest score among candidates with no hard failure; ties go to the earlier candidate."""
    best = None
    for fc, rep in scored:
        if not rep.hard and (best is None or rep.score > best[1].score + 1e-9):
            best = (fc, rep)
    return best


def _scale_members(els: dict[str, dict[str, Any]], ids: list[str], f: float) -> None:
    """Shrink a unit uniformly about its centre (geometry and font sizes)."""
    box = _union([_element_box(els[i]) for i in ids])
    for i in ids:
        el = els[i]
        _set_box(el, _element_box(el).scaled(f, box.cx, box.cy))
        if el.get("type") == "text":
            el["font_size"] = max(1.0, _num(el.get("font_size"), 1.0) * f)


def _repair(analysis: SourceAnalysis, fitted: FittedCandidate, target: dict[str, Any]) -> FittedCandidate:
    """One bounded pass: lift sub-pixel boxes to 1 px, refit overflowing text, shrink units whose text collides, then
    slide every unit back inside the canvas."""
    import copy
    tw, th = _target_size(target)
    els = copy.deepcopy(fitted.elements)
    unit_of = {i: uid for uid, u in analysis.units.items() for i in u.member_ids}
    for el in els.values():
        b = _element_box(el)
        if b.w < 1 or b.h < 1:
            _set_box(el, Box(b.cx - max(1.0, b.w) / 2, b.cy - max(1.0, b.h) / 2, max(1.0, b.w), max(1.0, b.h)))
        if el.get("type") == "text":
            el["font_size"] = max(1.0, _num(el.get("font_size"), 1.0))
            box = _element_box(el)
            fit = _fit_text(el, box, el["font_size"])
            if not fit.fits:   # grow the box downward when the text needs more height than allocated
                _, _, need = _measure_text(el, fit.font_size, box.w)
                box = Box(box.x, box.y, box.w, max(box.h, need + 1))
            el["font_size"] = fit.font_size
            _set_box(el, box)
    for _ in range(TUNABLES["repair_steps"]):
        texts = [(i, _text_ink_out(el)) for i, el in els.items() if el.get("type") == "text"]
        hit = None
        for (a, ba), (b, bb) in combinations(texts, 2):
            ua, ub = unit_of[a], unit_of[b]
            if ua != ub and ba.intersect(bb) > 0 and frozenset((ua, ub)) not in analysis.src_overlaps:
                hit = min((ua, ub), key=lambda u: analysis.units[u].prominence)
                break
        if hit is None:
            break
        _scale_members(els, analysis.units[hit].member_ids, TUNABLES["repair_shrink"])
    for u in analysis.units.values():               # keep every unit inside the canvas (translate, then shrink)
        if u.uid in analysis.backgrounds:
            continue
        box = _union([_rotated_aabb(els[i]) for i in u.member_ids])
        if box.w > tw or box.h > th:
            _scale_members(els, u.member_ids, min(tw / box.w, th / box.h) * 0.999)
            box = _union([_rotated_aabb(els[i]) for i in u.member_ids])
        dx = -box.x if box.x < 0 else tw - box.right if box.right > tw else 0.0
        dy = -box.y if box.y < 0 else th - box.bottom if box.bottom > th else 0.0
        for i in u.member_ids:
            _set_box(els[i], _element_box(els[i]).translated(dx, dy))
    rep = FittedCandidate(fitted.candidate, els, {}, {}, [n for n in fitted.notes if not n.startswith("FIT ERROR")]
                          + ["repaired"])
    for uid, u in analysis.units.items():
        rep.used[uid] = _union([_rotated_aabb(els[i]) for i in u.member_ids])
    _round_elements(rep.elements)
    return rep


def fallback_layout(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Letterbox: backgrounds full-bleed, everything else one rigid composition at s = min(sx, sy), centred. Always
    structurally valid; visually weak on extreme ratios (docs/engine.md §8). Uses as little of the engine as possible."""
    tw, th = _target_size(target)
    canvas = source.get("canvas") or {}
    sw, sh = max(1.0, _num(canvas.get("width"), 1.0)), max(1.0, _num(canvas.get("height"), 1.0))
    s = min(tw / sw, th / sh)
    ox, oy = (tw - sw * s) / 2, (th - sh * s) / 2
    out = []
    for el in source.get("elements") or []:
        el = dict(el)
        b = Box(_num(el.get("x")), _num(el.get("y")), _num(el.get("width"), 1.0), _num(el.get("height"), 1.0))
        full = el.get("type") in ("image", "shape") and Box(0, 0, sw, sh).intersect(b) >= 0.9 * sw * sh
        if full:
            nb = Box(0, 0, tw, th)
        else:
            nb = Box(ox + b.x * s, oy + b.y * s, max(1.0, b.w * s), max(1.0, b.h * s))
        if el.get("type") == "text":
            el["font_size"] = max(1.0, _num(el.get("font_size"), 16.0) * s)
            try:
                _, _, need = _measure_text(el, el["font_size"], nb.w)
                nb = Box(nb.x, nb.y, nb.w, max(nb.h, need))
            except Exception:
                pass
        _set_box(el, nb)
        aabb = _rotated_aabb(el)                     # shrink, then slide the drawn bounds inside the canvas
        if aabb.w > tw or aabb.h > th:
            f = min(tw / aabb.w, th / aabb.h) * 0.999
            nb = Box(nb.cx - nb.w * f / 2, nb.cy - nb.h * f / 2, max(1.0, nb.w * f), max(1.0, nb.h * f))
            if el.get("type") == "text":
                el["font_size"] = max(1.0, el["font_size"] * f)
            _set_box(el, nb)
            aabb = _rotated_aabb(el)
        dx = -aabb.x if aabb.x < 0 else tw - aabb.right if aabb.right > tw else 0.0
        dy = -aabb.y if aabb.y < 0 else th - aabb.bottom if aabb.bottom > th else 0.0
        _set_box(el, nb.translated(dx, dy))
        out.append(el)
    # A label that grew to its measured height can poke out of its pill at tiny scales; grow the pill instead
    # (shapes are not proportion-locked), so labels stay inside their shapes (H7).
    src_els = source.get("elements") or []
    src_box = [Box(_num(e.get("x")), _num(e.get("y")), _num(e.get("width")), _num(e.get("height"))) for e in src_els]
    for ti, t in enumerate(out):
        if t.get("type") != "text" or _num(t.get("rotation")):
            continue
        holders = [si for si, s in enumerate(out) if s.get("type") == "shape" and not _num(s.get("rotation"))
                   and src_box[si].contains(src_box[ti], 0.5)]
        if not holders:
            continue
        si = min(holders, key=lambda i: src_box[i].area)
        tb, sb = _element_box(t), _element_box(out[si])
        if not sb.contains(tb, 0.0):
            u = _union([tb, sb])
            _set_box(out[si], Box(max(0.0, u.x), max(0.0, u.y), min(u.w, tw), min(u.h, th)))
    els = {str(i): e for i, e in enumerate(out)}
    _round_elements(els)
    return {"canvas": {"width": tw, "height": th}, "elements": out}


# =====================================================================================
# §6 resize() (Dev 3)
# =====================================================================================

def _target_size(target: dict[str, Any]) -> tuple[int, int]:
    return max(1, int(round(_num(target.get("width"), 1.0)))), max(1, int(round(_num(target.get("height"), 1.0))))


def _sanitize(source: dict[str, Any]) -> dict[str, Any]:
    """Deep copy with finite numerics, so every helper downstream can assume clean geometry."""
    import copy
    src = copy.deepcopy(source)
    for el in src.get("elements") or []:
        for k, default in (("x", 0.0), ("y", 0.0), ("width", 1.0), ("height", 1.0)):
            if not isinstance(el.get(k), (int, float)) or not math.isfinite(el[k]):
                el[k] = _num(el.get(k), default)
        if el.get("type") == "text" and (not isinstance(el.get("font_size"), (int, float)) or not el["font_size"] > 0):
            el["font_size"] = max(1.0, _num(el.get("font_size"), 16.0))
    return src


def _round_elements(els: dict[str, dict[str, Any]]) -> None:
    """Two decimals for geometry. Font sizes become whole numbers, rounded DOWN: cr-renderer draws glyphs at
    int(font_size) but spaces lines with the float, so a fractional size only makes measurement and drawing disagree.
    Rounding down never makes fitted text wider, taller, or wrap onto more lines."""
    for el in els.values():
        for k in ("x", "y", "width", "height"):
            if isinstance(el.get(k), (int, float)):
                el[k] = round(el[k], 2)
        if el.get("type") == "text" and isinstance(el.get("font_size"), (int, float)) and math.isfinite(el["font_size"]):
            el["font_size"] = max(1, int(el["font_size"]))


def _log(msg: str) -> None:
    import sys
    print(msg, file=sys.stderr)


def resize(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    """
    Args:
        source: {"canvas": {"width": int, "height": int}, "elements": [...]}
                 — the design at its original size. See
                 chameleon_design_schema_v0.9.json for the full element spec
                 (text / image / shape, position, size, rotation, opacity,
                 z_index, group_id, and type-specific fields).
        target_canvas: {"width": int, "height": int} — the shape you're
                 resizing into.

    Returns:
        A design dict with the SAME shape as `source`:
        {"canvas": target_canvas, "elements": [...]}

        Hard requirements (see RULES.md for the full list):
        - Every element `id` in `source` must appear exactly once in your
          output. Do not add, remove, or invent elements.
        - Do not regenerate or redraw any asset's pixel content — you are
          repositioning and resizing the elements you were given, nothing
          more. This is a disqualifying rule, not a style preference.
        - Non-background image elements should be kept in proportion
          (matching the source aspect ratio within ~5%) unless they are a
          full-bleed / `fit: "cover"` element, which is expected to crop.
        - `font_size` may change to help text fit. `font_family` and
          `content` should not, for this stage of scoring (see RULES.md,
          "Adaptive typography and copy").
        - Nothing should end up outside `target_canvas`'s bounds.

    This is where your actual approach goes: constraint solving, a learned
    layout model, heuristics, whatever you choose. Latency is not a scoring
    concern (see RULES.md) — spend your effort on quality, not speed.
    """
    import os
    import time
    t0 = time.perf_counter()
    debug = os.environ.get("CHAMELEON_DEBUG") == "1"
    tw, th = _target_size(target_canvas)
    tag = f"[resize {tw}x{th}]"
    try:
        src = _sanitize(source)
        target = {"width": tw, "height": th}
        an = analyze_source(src)
        scored = []
        for cand in generate_candidates(an, target):
            fitted = fit_candidate(an, cand, target)
            _round_elements(fitted.elements)
            scored.append((fitted, validate(an, fitted, target)))
            if debug:
                rep = scored[-1][1]
                _log(f"{tag}   {cand.name:24s} score={rep.score:.3f} hard={len(rep.hard)} "
                     + " ".join(f"{k[:4]}={v:.2f}" for k, v in rep.soft.items()) + (f"  {rep.hard[:3]}" if rep.hard else ""))
        best, how = _select(scored), "selected"
        if best is None:
            for fitted, _ in sorted(scored, key=lambda p: -p[1].score)[:TUNABLES["repair_tries"]]:
                fixed = _repair(an, fitted, target)
                rep = validate(an, fixed, target)
                if debug:
                    _log(f"{tag}   repair {fitted.candidate.name}: hard={rep.hard[:3]}")
                if not rep.hard:
                    best, how = (fixed, rep), "repaired"
                    break
        if best is None:
            reasons = sorted({h.split(":")[0] for _, rep in scored for h in rep.hard})
            _log(f"{tag} FALLBACK: no valid candidate among {len(scored)} ({', '.join(reasons)})")
            return fallback_layout(src, target)
        fitted, rep = best
        low = sum(n.startswith("Below readability floor") for n in fitted.notes)
        valid = sum(not r.hard for _, r in scored)
        _log(f"{tag} {how} {fitted.candidate.name} score={rep.score:.3f} ({valid}/{len(scored)} candidates valid, "
             f"{low} text(s) below readability floor) in {time.perf_counter() - t0:.2f}s")
        return {"canvas": {"width": tw, "height": th}, "elements": [fitted.elements[i] for i in an.order]}
    except Exception as exc:
        _log(f"{tag} FALLBACK: {type(exc).__name__}: {exc}")
        try:
            return fallback_layout(source, {"width": tw, "height": th})
        except Exception as exc2:   # last resort: never raise out of resize() (docs/engine.md R8)
            _log(f"{tag} FALLBACK FAILED: {type(exc2).__name__}: {exc2}; returning the source geometry")
            return {"canvas": {"width": tw, "height": th}, "elements": [dict(e) for e in source.get("elements") or []]}
