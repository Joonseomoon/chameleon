# Chameleon engine — Iteration 1 plan

**Status:** draft for team review · **Team:** three developers · **Code under change:** [`engine/resize.py`](../engine/resize.py) only (production)

This document plans the first real implementation of `resize(source, target_canvas)`.
It replaces the uniform-stretch baseline with a **deterministic, group-aware layout engine**.
It separates what the challenge actually requires (§2.1) from what we are choosing to do (§2.2).
It also lists the places where the official materials are unclear or contradict each other (§2.3).

Sources read for this plan: [`README.md`](../README.md), [`RULES.md`](../RULES.md), [`FAQ.md`](../FAQ.md),
[`chameleon_design_schema_v0.9.json`](../chameleon_design_schema_v0.9.json), [`engine/resize.py`](../engine/resize.py),
[`engine/main.py`](../engine/main.py), [`renderer/converter/`](../renderer/converter/), the installed `cr_renderer`
package (commit `a17e1fb`), the three files in [`input/`](../input/), the [`brand_kit/`](../brand_kit/README.md), and the
challenge brief PDF (*"Final Chameleon Challenge"*). The brief is not checked into this repo; references to it use the
form "Brief §N".

---

## Contents

1. [Objective, scope, and non-goals](#1-objective-scope-and-non-goals)
2. [Requirements: confirmed, proposed, unresolved](#2-requirements-confirmed-proposed-unresolved)
3. [Architecture and pipeline](#3-architecture-and-pipeline)
4. [Internal representation and helper contracts](#4-internal-representation-and-helper-contracts)
5. [Candidate generation and selection strategy](#5-candidate-generation-and-selection-strategy)
6. [Ownership: who owns what](#6-ownership-who-owns-what)
7. [Prioritized tasks](#7-prioritized-tasks)
8. [Milestones](#8-milestones)
9. [Evaluation plan](#9-evaluation-plan)
10. [Definition of done](#10-definition-of-done)
11. [Risks and organizer questions](#11-risks-and-organizer-questions)
12. [Iteration-two options](#12-iteration-two-options)
13. [Appendix: how this plan changed the original proposal](#13-appendix-how-this-plan-changed-the-original-proposal)

---

## 1. Objective, scope, and non-goals

### Objective

Replace the uniform-stretch baseline in [`engine/resize.py`](../engine/resize.py#L54) with an engine that **moves the
furniture instead of stretching the picture** (Brief §2). Concretely, iteration 1 should:

| Sample | Resize | Iteration-1 target |
|---|---|---|
| [Level 1](../input/chameleon_sample_level1_warmup.json) | 1080×1080 → 1080×1350 | **Preserve** the simple composition: same visual order, logo not distorted, text the same size or larger, vertical space used evenly. |
| [Level 2](../input/chameleon_sample_level2_core.json) | 1080×1080 → 1600×400 | **Visibly improve** on the baseline: product image keeps its proportions, labels stay inside their buttons, headline is readable, and content flows side by side rather than being squashed vertically. |
| [Level 3](../input/chameleon_sample_level3_advanced.json) | 1080×1350 → 728×90 | **Survive** honestly: no crash, all 17 elements present, every one on the canvas with a non-zero size, the rotated ribbon fully on the canvas, and the missing asset handled. Any text that falls below a readability floor is reported in the run log, not hidden. |

The same code must also behave sensibly on target shapes we have not seen. Targets will come from a catalogue of
real-world formats ([RULES — Target canvas sizes](../RULES.md#target-canvas-sizes)).

### In scope

- Source analysis: layout units (groups), background and decoration inference, reading order, alignment, prominence.
- Candidate layouts from two template families, described in §5: **anchor** (keep the composition) and **lanes**
  (reflow into stacked or side-by-side lanes).
- Fitting text with real font metrics, and fitting images, groups, and decorations.
- Hard validation, soft scoring, selection, and an explicit fallback.
- A dev-only evaluation script that compares our output with the baseline across many targets.
- Docker verification with `--network none`, and a short "Approach" section in the README. The README is a required
  stage-one deliverable (Brief §5.1).

### Non-goals (deferred)

- Model training, fine-tuning, LoRA, or a vision model at runtime. These may be revisited in iteration 2 (§12).
- Any UI, including the Express add-on (Brief §3.3 — finale only).
- Changing text content or font family. Adaptive copy is finale-only ([RULES](../RULES.md#adaptive-typography-and-copy--bonus-not-required)).
- Reading, cropping, recoloring, or generating asset pixels. Iteration 1 never opens asset files (see §2.2 P7).
- Changing `engine/main.py`, `renderer/`, the schema, or the Dockerfile's runtime behavior.
- Services, databases, CI pipelines, or new production modules.

---

## 2. Requirements: confirmed, proposed, unresolved

### 2.1 Confirmed constraints (from official sources)

| # | Constraint | Source |
|---|---|---|
| C1 | Public entry point is `resize(source, target_canvas) -> dict`. It returns `{"canvas", "elements"}` in the same structure as the source. | [`engine/resize.py`](../engine/resize.py#L12), Brief §2.3 |
| C2 | Every source `id` appears exactly once in the output. Elements may not be added, removed, or invented. | [RULES — What gets compared](../RULES.md#what-gets-compared-during-scoring), [schema `id`](../chameleon_design_schema_v0.9.json), [FAQ Q1](../FAQ.md#input--output) |
| C3 | Required fields stay present: common fields plus type-specific ones (text: `content`, `font_family`, `font_size`, `color`; image: `asset_id`; shape: `shape_type`, `fill_color`). | [schema `allOf`](../chameleon_design_schema_v0.9.json), [`main.py` self-check](../engine/main.py#L37) |
| C4 | Nothing ends up outside the target canvas. | [RULES](../RULES.md#what-gets-compared-during-scoring), [`resize.py` docstring](../engine/resize.py#L27) |
| C5 | Non-background images keep the aspect ratio of their source box within about 5%. Full-bleed / `fit: "cover"` images are exempt and are expected to crop. | [`resize.py` docstring](../engine/resize.py#L27), [RULES](../RULES.md#what-gets-compared-during-scoring) |
| C6 | `font_size` may change. For stage one, `content` and `font_family` must not change. | [RULES — Adaptive typography](../RULES.md#adaptive-typography-and-copy--bonus-not-required), [schema `font_size`](../chameleon_design_schema_v0.9.json) |
| C7 | **Disqualifying:** no regenerating, redrawing, or replacing the pixel content of any asset. | [RULES — Disqualifying](../RULES.md#disqualifying-regenerating-image-content) |
| C8 | Both artifacts are scored: the JSON (structural checks) and the PNG rendered by the official renderer (AI rubric evaluator). | [RULES — What gets compared](../RULES.md#what-gets-compared-during-scoring) |
| C9 | Quality rubric: no unwanted collisions (intentional overlap is fine; text must not sit on a busy image), readable text with enough contrast and nothing cut off, on-brand (colors, fonts, logo intact and in proportion), intentional (aligned, even spacing, hierarchy kept), robust across designs and shapes. | Brief §2.5 |
| C10 | Elements sharing a non-null `group_id` are a designer-intended unit that should travel together. The meaning of a group is not provided. | [schema `group_id`](../chameleon_design_schema_v0.9.json) |
| C11 | Input carries no semantic labels. The engine sees only `id`, `type`, geometry, and properties, and must not be hand-tailored to specific designs or sizes. | [FAQ Q1](../FAQ.md#input--output), Brief §4 |
| C12 | A missing asset must not crash the engine. The Level-3 `holiday_watermark` is deliberately missing; don't add it. | [Level-3 sample](../input/chameleon_sample_level3_advanced.json), [FAQ Q4](../FAQ.md#rendering--local-preview), [brand kit README](../brand_kit/README.md) |
| C13 | Stage-one scoring runs offline in Docker with `--network none`. All dependencies are installed at build time. | [README — Docker](../README.md#docker-how-youll-actually-be-run), [`Dockerfile`](../Dockerfile) |
| C14 | There is no hard time limit. Speed and reliability is a rubric row, and quality comes first. | [RULES — Performance](../RULES.md#performance) |
| C15 | Hidden targets come from a fixed catalogue of real platform formats. The specific targets are unseen. | [RULES — Target canvas sizes](../RULES.md#target-canvas-sizes) |
| C16 | Schema value ranges: `font_size ≥ 1`; `width`, `height ≥ 0`; `opacity` in [0, 1]; canvas dimensions are integers ≥ 1; the top-level object allows only `canvas` and `elements`. | [schema](../chameleon_design_schema_v0.9.json) |
| C17 | `rotation` is in degrees, clockwise, about the element's center. Higher `z_index` paints on top. | [schema](../chameleon_design_schema_v0.9.json) |
| C18 | The README says production work goes in `engine/resize.py` ("the only file you need to edit"), and `main.py` and `renderer/` are plumbing. | [README](../README.md), Brief §7.1 |

### 2.2 Observed renderer and harness behavior (verified in code; affects design)

These are facts about the vendored code. They are not rules, but ignoring them would produce bad PNGs.

| # | Observation | Where | Consequence for us |
|---|---|---|---|
| R1 | Text is **top-aligned** in its box. Line *i* starts at `y = i · font_size · line_height`. Box height is never used to center or clip. | `cr_renderer/text_utils.py` (`render_text`, line 33) | To center a label inside a shape, we compute the vertical offset ourselves from the measured text height. If the box is too short, text spills below it silently. |
| R2 | Line breaks come from a greedy word wrap using per-character skia advances from the brand font. Wrapping happens only at spaces, and **a word longer than the box overflows** instead of breaking. | [`wrap.py`](../renderer/converter/wrap.py#L23) | Our text measurement must call the same `char_widths` / `wrap_text`. The font size must be small enough that the longest word fits the box width. |
| R3 | `letter_spacing` is applied when drawing but **ignored when wrapping**. | [`wrap.py`](../renderer/converter/wrap.py#L8) vs `text_utils.make_text_blob` | Line *breaks* follow the unspaced widths, but the *drawn* width includes spacing. A line can be wider than its box even though it wrapped "correctly". We add spacing back when checking width. |
| R4 | `max_lines` is ignored by the renderer. | [`convert.py`](../renderer/converter/convert.py#L166) | When `max_lines` is set, we treat it as a fitting constraint ourselves. |
| R5 | `cr_renderer` requires `width > 0`, `height > 0`, and `fontSize > 0` (pydantic `gt=0`). A zero-size text element would fail validation during rendering. | `cr_renderer/schema.py` lines 18–19, 129 | Every output box is at least 1 px in each dimension, even though the schema allows 0. |
| R6 | The `fit` modes `contain`, `cover`, and `fill` are emulated before rendering. `contain` letterboxes, `cover` crops from the center, `fill` stretches. | [`assets.py`](../renderer/converter/assets.py#L49) | Changing a `contain` image's box ratio would not distort pixels, but it still fails C5 as written. We lock the box ratio regardless. |
| R7 | A missing asset renders as a **gray "missing asset" placeholder box**, and the converter logs a warning. | [`convert.py`](../renderer/converter/convert.py#L109), [`assets.py`](../renderer/converter/assets.py) | The watermark will be visible as a gray box wherever we place it. Iteration 1 treats it as an ordinary image whose aspect ratio comes from its source box. |
| R8 | `main.py` does **not** catch exceptions from `resize()`. One exception aborts every remaining task in the run. | [`main.py`](../engine/main.py#L116) | `resize()` must never raise. Any internal failure falls through to the fallback (§5.5). |
| R9 | The self-check's off-canvas test uses the **unrotated** box with a 0.5 px tolerance. | [`main.py`](../engine/main.py#L64) | A rotated element can pass the self-check while its corners are drawn off-canvas. We check the rotated bounding box instead. |
| R10 | A shape's `fill_color` alpha multiplies into the element's opacity. Strokes are drawn only for `line` shapes. Text-element `opacity` is not passed to the text renderer. | [`convert.py`](../renderer/converter/convert.py#L103), [`shapes.py`](../renderer/converter/shapes.py) | The effective visual weight of a shape is `opacity × fill alpha`. We use that number to infer decorations. |
| R11 | `style_runs` coloring treats `end` as **inclusive**, while the schema says end-exclusive. One extra character gets the run's style. | [`convert.py`](../renderer/converter/convert.py#L176) | Not ours to fix, and we never alter `style_runs`. Reported as an observation (Q9). |
| R12 | `requirements.txt` installs `cr-renderer` from an unpinned git URL. The local environment has commit `a17e1fb`. | [`requirements.txt`](../requirements.txt) | A future upstream change could alter rendering or measurement. Proposed fix: pin the commit (task T3.6). |
| R13 | Docker uses Python 3.11 and the local venv uses 3.13. | [`Dockerfile`](../Dockerfile) | Code must be valid on 3.11. For example, no `type` alias statements (3.12+). |
| R14 | An unknown `font_family` falls back to an available brand family and prints a message. It does not raise. | `cr_renderer/fonts.py` (verified locally) | Measurement stays consistent with rendering even for unexpected fonts. |

### 2.3 Unresolved or conflicting requirements

| # | Conflict / gap | Our iteration-1 decision |
|---|---|---|
| U1 | **Intentional bleed vs. "nothing off-canvas."** The README calls the Level-2 corner-bleeding circle "expected", but RULES says "nothing off-canvas" is checked structurally. The source design itself starts with `deco_circle_1` at (-100, -100). | Treat C4 as hard. Decorations are clamped fully inside the canvas, which loses the bleed effect. Ask organizers (Q1). |
| U2 | **Off-canvas for rotated elements.** It is unclear whether the scorer tests the unrotated box (as the self-check does) or the drawn extent. | Check the rotated bounding box. This is stricter, so it is safe either way. |
| U3 | **"Skip it" for missing assets.** The Level-3 note suggests the engine "skip it, placeholder it", but C2 forbids removing elements. | Interpret "skip" as "don't crash". The element is kept and placed normally, and the renderer draws its placeholder. Ask organizers (Q4). |
| U4 | **Readability vs. "keep everything" on tiny canvases.** At 728×90, 17 elements including about 190 characters of subhead and a legal line cannot all be readable. The brief's "let less-important elements drop away" is listed as a finale idea, not a stage-one allowance. | Keep every element at a non-zero, measurable size. Prioritize readability by prominence. Log every element below the readability floor. Never hide (§5.5). Ask organizers (Q2). |
| U5 | **Image proportion reference.** The ~5% rule compares against the "source aspect ratio". That could mean the box or the asset's intrinsic ratio, and the two differ (logo asset is 3:1, its Level-2 box is 320×110). | Preserve the **source box** ratio (internal tolerance 1%). This needs no asset I/O and is independent of `fit`. |
| U6 | **What counts as a background.** Only `fit: "cover"` and "full-bleed" are named. A full-canvas `contain` image or a full-canvas shape is not addressed. | Coverage-based inference (§5.1). Any inferred background is stretched to the full canvas. Ask organizers (Q3). |
| U7 | **Renderer guidance is stale in the FAQ.** FAQ Q3 and Q5 describe a separate `transformer-convertor` Flask app to vendor, but the README says it is already vendored in `renderer/`. | Follow the README. `python -m engine.main` renders real PNGs (verified locally). |
| U8 | **Viewer add-on in stage one.** FAQ Q5 mentions a "required (but not scored) viewer add-on" for stage one. The brief makes the add-on finale-only. | Out of scope for iteration 1. Ask organizers (Q5). |
| U9 | **Schema is provisional.** The schema title says "v0.9 — provisional", and the brief says the response schema "is being defined". | Copy every source field through untouched and change only geometry and `font_size`. That maximizes forward compatibility. |
| U10 | **Hardware, model list, and size limit are TBD.** RULES gives "Apple M5 Pro (TBD)". The image is `linux/amd64`, so on Apple silicon it may run under emulation. | Pure-Python heuristics with no new dependencies. Keep the per-task runtime modest (§10). |
| U11 | **No target catalogue in the repo.** RULES calls the catalogue a "fixed material", but it isn't provided. | Use our own list of common formats for evaluation (§9.2), clearly labeled as ours. Ask organizers (Q6). |
| U12 | **No contrast or "busy image" metric is defined.** | Iteration 1 uses geometric proxies: text should not overlap non-background images unless it did in the source. Pixel-aware contrast is an iteration-2 option. |

### 2.4 Proposed implementation choices (ours, not requirements)

| # | Choice | Why |
|---|---|---|
| P1 | All production code stays in `engine/resize.py`, split into owned sections (§6). Dev-only tooling goes in a new `tools/` folder that the Docker image does not copy. | Respects C18 and keeps the runtime image unchanged. |
| P2 | Text is measured by importing `renderer.converter.wrap` and `renderer.converter.brand_fonts`. If that import fails, measurement falls back to a Pillow FreeType approximation. | Gives parity with the renderer (R2) without editing it. The fallback keeps `resize()` alive if skia is unavailable. |
| P3 | The engine is fully deterministic. There is no randomness. Ties are broken by source element order, never by `id` text or asset names. | Makes runs reproducible and keeps us honest about C11. |
| P4 | Output elements are shallow copies of the source elements. Only `x`, `y`, `width`, `height`, and `font_size` change. Unknown fields such as `_note` pass through unchanged. | Reduces the risk of breaking JSON checks (C2, C3, C6, U9). |
| P5 | `rotation`, `opacity`, `z_index`, colors, `group_id`, `style_runs`, `fit`, and `asset_id` are never modified. | Keeps the stacking order and brand intent stable. |
| P6 | Coordinates are rounded to 2 decimals. The canvas uses the target's integers. | Gives stable JSON and avoids float noise in diffs. |
| P7 | Iteration 1 never opens asset files. | Avoids missing-asset paths entirely (C12) and keeps us far from C7. Pixel-aware ideas are iteration 2 (§12). |
| P8 | The internal aspect tolerance is 1%, stricter than the ~5% rule. | Leaves headroom for rounding and for a stricter scorer. |

---

## 3. Architecture and pipeline

```
resize(source, target_canvas)                         ── Dev 3 (orchestration, never raises)
 │
 ├─ 1. analyze_source(source)  → SourceAnalysis        ── Dev 1
 │      units (groups/singletons), backgrounds, decorations,
 │      reading-order blocks, alignment, prominence, source overlaps
 │
 ├─ 2. generate_candidates(analysis, target) → [Candidate]   ── Dev 1
 │      anchor (keep composition) + lanes(k, partition, orientation) [+ corner-pinned variants]
 │      each Candidate = a slot Box per content unit + decoration/background placements
 │
 ├─ 3. fit_candidate(analysis, candidate, target) → FittedCandidate   ── Dev 2 (per candidate)
 │      images: aspect-locked contain-in-slot
 │      text: real-font wrap + largest font that fits, hierarchy order enforced
 │      groups: uniform scale, labels re-fit inside their shape
 │
 ├─ 4. validate(analysis, fitted, target) → Report (hard failures + soft metrics)   ── Dev 3
 ├─ 5. select(reports) → best FittedCandidate | None          ── Dev 3
 │
 └─ 6. fallback_layout(...) if nothing passes or anything raised    ── Dev 3
         → {"canvas": target_canvas, "elements": [...]}  (same ids, same order as source)
```

Design principles:

- **Dev 1 decides *where*, Dev 2 decides *how big inside*.** A candidate assigns each content unit a slot (a box on the
  target canvas). Fitting places the unit's elements inside that slot and reports the box it actually used. This slot
  boundary is the main interface between parallel work.
- **Generate several candidates, then pick one.** Templates are cheap. Scoring is how we avoid tuning rules to one
  sample.
- **Hard checks gate, soft scores rank.** A candidate that fails any hard check is never selected, however good it
  looks (§5.3).
- **Fail honestly.** If no candidate passes, the fallback is structurally valid and its limitations are logged to
  stderr. Nothing is hidden (§5.5).

---

## 4. Internal representation and helper contracts

The public signature `resize(source: dict, target_canvas: dict) -> dict` **does not change**. Everything below is
private to `engine/resize.py`: leading underscore for helpers, plain `@dataclass` types, Python 3.11 compatible.

### 4.1 Shared types (Section 0 of `resize.py`, owned by Dev 3, agreed at the interface checkpoint)

```python
@dataclass(frozen=True)
class Box:
    x: float; y: float; w: float; h: float
    # helpers: right, bottom, cx, cy, area, intersect(other) -> float, contains(other, tol) -> bool,
    #          scaled(s, origin), translated(dx, dy)

@dataclass
class LayoutUnit:
    uid: str                    # internal: "g<n>" for groups, "e<n>" for singletons (n = first member's source index)
    member_ids: list[str]       # element ids, source order
    kind: str                   # "background" | "decoration" | "content"
    src_box: Box                # union of members' *rotated* AABBs in source coordinates
    ink_box: Box                # src_box tightened by measured text extent (text-only units); used for relationships
    rel: dict[str, Box]         # each member's unrotated box relative to src_box origin (source scale)
    rotation: float             # 0 unless members are rotated (members of a group share rotation in practice)
    has_text: bool; has_image: bool; has_shape: bool
    aspect_locked: bool         # True if the unit contains a non-background image or is a rotated group
    prominence: float           # 0..1, see §5.1
    h_align: str                # "left" | "center" | "right"  (relative to source canvas)
    edge_anchor: tuple[str, str] | None   # e.g. ("top", "left") if small and hugging two edges
    block: int                  # index of the reading-order block this unit belongs to
    sibling_set: int | None     # units with the same structure and similar size share an id → equal scale

@dataclass
class SourceAnalysis:
    canvas_w: int; canvas_h: int
    elements: dict[str, dict]   # original element dicts by id (read-only)
    order: list[str]            # source element order (for output ordering and tie-breaks)
    units: dict[str, LayoutUnit]
    backgrounds: list[str]; decorations: list[str]; content: list[str]   # unit uids
    blocks: list[list[str]]     # reading-order blocks of content units (rows, or columns for wide sources)
    flow_axis: str              # "vertical" if blocks are rows, "horizontal" if columns
    src_overlaps: set[frozenset[str]]   # unit pairs that overlap in the source (intentional by definition)
    margin_frac: float          # estimated source outer margin / short side

@dataclass
class Candidate:
    name: str                   # e.g. "anchor", "lanes:h:k=3:[0-1|2|3-4]", "+corners"
    slots: dict[str, Box]       # content unit uid -> slot on target canvas
    deco: dict[str, Box]        # decoration unit uid -> target box (already aspect-preserved, clamped)
    align: dict[str, tuple[str, str]]   # unit uid -> (h_align, v_align) inside its slot

@dataclass
class TextFit:
    font_size: float; lines: int; text_w: float; text_h: float
    fits: bool                  # measured box within allotted box (width incl. letter spacing, height, max_lines)
    below_floor: bool           # font_size < READABLE_MIN_PX

@dataclass
class FittedCandidate:
    candidate: Candidate
    elements: dict[str, dict]   # id -> output element (copy of source with new geometry/font_size)
    used: dict[str, Box]        # unit uid -> box actually occupied after fitting
    text: dict[str, TextFit]    # text element id -> fit report
    notes: list[str]

@dataclass
class Report:
    hard: list[str]             # human-readable hard failures; empty == valid
    soft: dict[str, float]      # metric name -> 0..1 (higher is better)
    score: float
```

### 4.2 Helper contracts

Each contract states the inputs, what the function guarantees, and what it must never do.
Function names are proposals to confirm at the interface checkpoint.

#### Dev 1 — source analysis and layout generation (Sections 1–2)

| Function | Contract |
|---|---|
| `_build_units(source) -> dict[str, LayoutUnit]` | Each non-null `group_id` becomes one unit; every other element is a singleton. Each element belongs to exactly one unit. Uses only geometry, type, z, opacity, and `group_id`. Never reads `id` text or `asset_id` names. |
| `_classify_units(units, canvas) -> None` | Sets `kind` on every unit using the §5.1 rules. At most one decision per unit; deterministic. |
| `_reading_blocks(units, analysis) -> (blocks, flow_axis)` | Clusters content units into rows (or columns for wide sources) using `ink_box` overlap along the cross axis, then orders blocks and units in reading order. |
| `_source_relationships(analysis) -> None` | Fills `src_overlaps`, `h_align`, `edge_anchor`, `sibling_set`, `prominence`, and `margin_frac`. |
| `analyze_source(source) -> SourceAnalysis` | Calls the above. Pure function that never mutates `source`. Needs Dev 2's `_measure_text` for `ink_box`; uses a character-count estimate until that lands. |
| `_candidate_anchor(analysis, target) -> Candidate` | Keeps the composition: each unit is scaled by `s = min(sx, sy)` and placed so its normalized center matches the source, then nudged to avoid new overlaps and kept within margins. |
| `_candidate_lanes(analysis, target, orientation, partition) -> Candidate` | Divides the reading-order blocks into lanes. Lanes sit side by side for wide targets and stack for tall ones, separated by gaps. Slot sizes are proportional to each block's demand at a common scale (binary search for the largest common scale that fits). |
| `_place_decorations(analysis, target, cand) -> None` | Fills `cand.deco`. Decorations keep their aspect ratio, are mapped by normalized position, scaled by `min(sx, sy)`, and clamped inside the canvas (U1). Overlapping content is allowed. |
| `generate_candidates(analysis, target) -> list[Candidate]` | Returns at least one candidate (anchor) for any input, and never more than `MAX_CANDIDATES`. Sorted deterministically. |

#### Dev 2 — text and image fitting (Section 3)

| Function | Contract |
|---|---|
| `_font_manager()` | Cached `FontManager` built from `brand_kit/fonts`, resolved relative to `resize.py` and not the working directory. Returns `None` if the renderer import fails, which makes callers use the Pillow fallback. |
| `_measure_text(el, font_size, box_w) -> (lines, text_w, text_h)` | Reproduces the renderer: `char_widths` + `wrap_text` (R2). `text_w` = widest line including `letter_spacing` (R3). `text_h = (lines-1)·fs·lh + ascent + descent` (R1). Pure and cached. |
| `_fit_text(el, box, size_cap) -> TextFit` | Finds the largest `font_size ≤ size_cap` (and `≥ 1`) such that wrapped text fits `box` in width (including the longest word), in height, and within `max_lines` if set. Binary search, then a final re-measure to confirm. Never changes `content` or `font_family`. |
| `_fit_image(el, slot, align) -> Box` | Returns the largest box with the **source box aspect ratio** that fits inside the slot, aligned as requested. Background images are not handled here. |
| `_fit_unit(analysis, unit, slot, align, text_scale) -> dict[id, dict]` | Singletons dispatch to text or image fitting. Groups and rotated units scale uniformly about the slot so their rotated AABB fits. Each label is then re-fit inside its associated shape (the shape in the same group that contained the label in the source), padded by the scaled source inset and vertically centered from its measured height (R1). If a label cannot fit at the floor size, the shape may widen within the slot, since shapes are not proportion-locked. |
| `_enforce_hierarchy(analysis, fits) -> None` | After individual fits: if source `fs_a > fs_b`, then output `fs_a ≥ fs_b`, achieved only by **lowering** sizes, never raising them. Units in the same `sibling_set` get the minimum of their sizes. |
| `fit_candidate(analysis, cand, target) -> FittedCandidate` | Places every element exactly once. Backgrounds become `Box(0, 0, W, H)`. Decorations are copied from `cand.deco`. Content is fitted into its slots. Fills `used` and `text`. Never raises on odd input; problems go into `notes` and the `fits` flags. |

#### Dev 3 — validation, selection, fallback, orchestration (Sections 0, 4–6)

| Function | Contract |
|---|---|
| `Box`, `_rotated_aabb(el) -> Box`, `_overlap(a, b) -> float` | Shared geometry used by all sections. Owned by Dev 3; changes go through a quick review. |
| `validate(analysis, fitted, target) -> Report` | Runs every hard check H1–H9 (§5.3) and every soft metric S1–S7 (§5.4). Pure. |
| `_select(reports) -> FittedCandidate \| None` | Picks the highest score among candidates with no hard failures. Ties go to the earlier candidate in generation order. |
| `_repair(analysis, fitted, target) -> FittedCandidate` | One bounded pass over the best-scoring failed candidate: shrink overflowing text in steps down to the hard floor, and shrink colliding units about their centers. Revalidate afterwards. |
| `fallback_layout(source, target) -> dict` | Uses a minimal set of dependencies so it can't plausibly fail. Works on the whole composition (§5.5). Always structurally valid. |
| `resize(source, target_canvas) -> dict` | Orchestrates the pipeline inside `try/except Exception`. Logs the chosen candidate, scores, floor hits, and fallback reasons to **stderr** (one line per task; verbose output when `CHAMELEON_DEBUG=1`). Output elements are in source order. Never raises (R8). |
| `tools/evaluate.py` (dev-only) | Batch runner, baseline comparison, contact sheets, schema validation, invariance tests (§9). |

---

## 5. Candidate generation and selection strategy

> **Every number in this section is PROVISIONAL.** They are starting points to tune during M3, not commitments.
> They live in one `TUNABLES` dict at the top of `resize.py`. Changing one requires evidence from the
> full evaluation set (§9), not from a single sample.

### 5.1 Source analysis rules

| Concept | Rule (provisional) |
|---|---|
| **Background** | An image or shape unit (no text) whose rotated AABB covers **≥ 90%** of the source canvas, with no content unit painted below it (lowest z band). Several stacked backgrounds are allowed. |
| **Decoration** | A shape-only unit (no text, no image) with effective alpha (`opacity × fill alpha`, R10) **≤ 0.5**, *or* one that extends **beyond the source canvas**, *or* one that overlaps content units while sitting below them in z. In Level 2, both circles qualify. The badge and CTA shapes do not, because they are grouped with text. |
| **Content** | Everything else. These units are laid out. |
| **Ink box** | For text, the measured wrapped extent at source size, aligned per `text_align`. Used for rows and overlaps so that generous text boxes don't merge neighboring rows. In Level 2 the subhead box overlaps the CTA box; their ink boxes do not. |
| **Reading blocks** | Content units whose ink boxes overlap by **≥ 50%** of the smaller extent along the cross axis share a block. Blocks are ordered along the flow axis, and units inside a block are ordered across it. |
| **Prominence** | `0.5 · area_share_rank + 0.5 · type_weight`, where `type_weight` = `font_size / max_font_size` for text-bearing units and `area / max_image_area` for image units. Ties go to higher z. |
| **Edge anchor** | Unit area **< 3%** of the canvas, and its ink box lies within **6%** of two perpendicular canvas edges. |
| **Sibling set** | Units with identical member-type signatures (for example shape+text), size within **±20%**, in the same block. Level-3 badges 1–3 qualify. |

### 5.2 Template families

All candidates use the target margin `m = max(4 px, 4% of target short side)` and gap `g = max(4 px, 2.5% of short side)`.

1. **Anchor (preserve composition).** Every content unit keeps its normalized center and is scaled by `min(sx, sy)`.
   Free space along the stretched axis is distributed in proportion to the source gaps. This is the expected winner
   for similar aspect ratios (Level 1).
2. **Lanes (reflow).** Take the ordered blocks `B1…Bn` and split them into `k` contiguous lanes.
   - For a **wide target** (target ratio > source ratio by a factor of more than 1.4), lanes are columns laid out
     left to right, and each lane stacks its blocks vertically. This covers side-by-side arrangements, for example
     logo + product | headline + subhead | CTA in Level 2.
   - For a **tall target**, lanes are rows stacked top to bottom, and each lane lays out its blocks horizontally.
     `k = n` with one block per lane is the pure vertical stack.
   - `k` ranges over 1…min(n, 4). Every contiguous partition is enumerated when `n ≤ 8` (at most 2^7 = 128). Above
     that, only balanced partitions are generated, so any number of elements is supported in bounded time.
   - Lane widths are proportional to each lane's content demand at a common scale. Inside a lane, blocks keep their
     source cross-axis alignment. A block with several units (badges) keeps them side by side and wraps onto extra
     rows if needed.
3. **Corner-pinned variant (P1).** For each of the above, a variant where edge-anchored units are removed from the
   flow and pinned to the same corner of the target, sized by the lane scale. This is kept only if they don't collide.

Every candidate places backgrounds at full bleed and decorations via `_place_decorations`.

### 5.3 Hard requirements (gate; failure = reject candidate)

| # | Check | Tolerance |
|---|---|---|
| H1 | Output id set equals source id set, each exactly once; same `type` per id | exact |
| H2 | Required fields present; every field except `x`, `y`, `width`, `height`, `font_size` identical to the source | exact |
| H3 | `canvas` equals the target, as integers; top-level keys are only `canvas` and `elements` | exact |
| H4 | Every element's **rotated** AABB lies inside the canvas; all numbers finite; `width`, `height ≥ 1`; `font_size ≥ 1` | 0.5 px |
| H5 | Non-background images: box aspect ratio matches the source box | 1% (rule says ~5%) |
| H6 | Text: measured lines fit the box width (with letter spacing) and height; `max_lines` respected | 0.5 px |
| H7 | A grouped label that was inside its shape in the source is inside that shape in the output | 0.5 px |
| H8 | No text element overlaps a text element from a different unit | > 0 px² fails |
| H9 | Output serializes to JSON and validates against the schema (dev tooling; at runtime the H1–H4 checks stand in) | — |

### 5.4 Quality preferences (soft score, provisional weights)

Each metric is normalized to 0…1, where higher is better.
`score = Σ wᵢ · Sᵢ` with **PROVISIONAL** weights:

| # | Metric | Weight |
|---|---|---|
| S1 | **Readability**: prominence-weighted share of text at or above `READABLE_MIN_PX = 12` (scaled up for large canvases: `max(12, 1.1% of short side)`) | 0.25 |
| S2 | **Unintended collisions**: 1 − (overlap area between content units that did *not* overlap in the source ÷ total content area). Text over a non-background image counts ×3. | 0.20 |
| S3 | **Hierarchy**: Kendall-τ between source and output prominence order (font sizes for text, areas for images), mapped to 0…1 | 0.15 |
| S4 | **Composition fidelity**: 1 − mean displacement of normalized unit centers, with reading order preserved | 0.15 |
| S5 | **Utilization**: share of the canvas inside margins covered by content bounding boxes, peaking at 55–75% | 0.10 |
| S6 | **Spacing consistency**: 1 − coefficient of variation of gaps between neighboring units in each lane | 0.08 |
| S7 | **Alignment**: share of units sharing a left, center, or right line with at least one other unit | 0.07 |

**Intentional vs. unwanted overlap.** An overlap is intentional if the same two units overlapped in the source, if
it is within a group, or if one side is a background or decoration with effective alpha ≤ 0.5. Every other overlap
between content units is unwanted, and text-on-text overlap is always a hard failure (H8).

### 5.5 Fallback and its limitations

Selection order:

1. The best-scoring candidate with zero hard failures.
2. Otherwise, `_repair` the best-scoring failed candidate once and revalidate.
3. Otherwise, or if any exception is raised anywhere: **`fallback_layout`**.
   - Backgrounds go to full bleed.
   - Every other element is transformed as one rigid composition: uniform scale `s = min(sx, sy)` about the source
     center, then centered on the target. This is "letterboxing", without stretching.
   - Text: `font_size = max(1, fs · s)` and box scaled by `s`, so wrapping stays approximately the same. The code
     verifies the result with `_measure_text` and grows the box height if needed.
   - Rotated AABBs are clamped inside the canvas, and every dimension is at least 1 px.
   - It writes a `FALLBACK: <reason>` line to stderr.

**Limitations, stated plainly:** the fallback is always structurally valid, but on extreme ratios it is visually weak.
At 728×90, `s ≈ 0.067`, so a 40 px headline becomes about 2.7 px and is unreadable, with large empty side bands. It is
a safety net, not a layout.

**Never allowed, in any path:** removing or duplicating ids, `opacity = 0`, zero or near-zero sizes used to hide
content, moving content off-canvas, stacking content behind a background, or changing `content`/`font_family`.

### 5.6 Tiny-canvas tradeoffs (recorded, not solved)

On extreme targets, fitting every element and keeping everything readable conflict. Iteration 1 resolves this in a
fixed, documented way:

- Readability is allocated by **prominence**. Low-prominence text (legal lines, long subheads) can fall below
  `READABLE_MIN_PX` and is reported per element in the log.
- The hard floor is the schema's `font_size ≥ 1`. We **log** any text below `HARD_MIN_PX = 6`. That is a quality
  failure we accept and record; it is not concealed.
- Expectation for Level 3: logo, product, headline, and CTA readable or recognizable; badges small; subhead and legal
  text very likely below the floor.

---

## 6. Ownership: who owns what

Production code stays in **one file**. Ownership is by **section** of `engine/resize.py`, marked with banner comments.
Each developer edits only their own sections. Changes to the shared types in Section 0 need agreement at a checkpoint
or a short review by the other two.

```
engine/resize.py
  # ===== §0 Shared types, geometry, TUNABLES ............ Dev 3 (changes reviewed by all)
  # ===== §1 Source analysis ............................. Dev 1
  # ===== §2 Candidate generation ......................... Dev 1
  # ===== §3 Text & image fitting ......................... Dev 2
  # ===== §4 Validation & scoring ......................... Dev 3
  # ===== §5 Fallback ..................................... Dev 3
  # ===== §6 resize() orchestration ....................... Dev 3
tools/evaluate.py   (dev-only, not copied into Docker) ...... Dev 3 (Dev 1/2 add their own diagnostics)
```

### Developer 1 — Source analysis and layout generation

Owns `_build_units`, `_classify_units`, `_reading_blocks`, `_source_relationships`, `analyze_source`,
`_candidate_anchor`, `_candidate_lanes`, `_place_decorations`, `generate_candidates`, and the §1 and §2 tunables.
**Delivers:** a `SourceAnalysis`, plus a list of `Candidate`s with slots for every content unit and boxes for every
decoration, through the §4 interface.

### Developer 2 — Text and image fitting

Owns `_font_manager`, `_measure_text`, `_fit_text`, `_fit_image`, `_fit_unit`, `_enforce_hierarchy`,
`fit_candidate`, and the §3 tunables. **Delivers:** `FittedCandidate`s with every element placed, plus `TextFit`
reports, through the §4 interface. `_measure_text` is delivered early (M1), because Dev 1's ink boxes and lane demand
estimates depend on it.

### Developer 3 — Validation, selection, evaluation, and integration

Owns the §0 shared code (`Box`, `_rotated_aabb`, `_overlap`, dataclasses, `TUNABLES`), `validate`, `_select`,
`_repair`, `fallback_layout`, `resize`, and `tools/evaluate.py`. Also owns the Docker offline run, the
`cr-renderer` pin proposal, the README run instructions, and the "Approach" section.
**Delivers:** the integrated engine, the evaluation reports, and merge or release decisions.

---

## 7. Prioritized tasks

Priority: **P0** = required for iteration 1 to ship; **P1** = should ship; **P2** = only if time remains.

| ID | Pri | Owner | Task / deliverable | Depends on | Acceptance criteria |
|---|---|---|---|---|---|
| T0.1 | P0 | All | **Interface checkpoint**: agree on §4 types, function signatures, `TUNABLES` names, section banners, and branch/merge rules | — | Signed-off stub file merged; every function exists and returns a trivially valid value |
| T3.1 | P0 | Dev 3 | §0 shared types and geometry (`Box`, rotated AABB, overlap) | T0.1 | Rotated AABB matches a hand calculation for the Level-3 ribbon (300×54 at −12° → about 304.7×115.2) |
| T3.2 | P0 | Dev 3 | Walking skeleton: `resize()` chains the stubs + `fallback_layout` + try/except; one stderr line per task | T0.1 | All 3 samples run; H1–H5 pass; no exceptions; output differs from the baseline (aspect-locked) |
| T3.3 | P0 | Dev 3 | `tools/evaluate.py`: runs samples × target set, writes outputs to `output/eval/`, baseline side by side, contact-sheet PNG, CSV of hard checks and scores | T3.2 | One command produces the contact sheet and CSV for all pairs; JSON schema validation included if `jsonschema` is installed locally |
| T2.1 | P0 | Dev 2 | `_font_manager` + `_measure_text` with Pillow fallback | T0.1 | For every sample text, line breaks equal the renderer's `wrap_text` output; measured height is within 2 px of the ink rows in the rendered PNG |
| T1.1 | P0 | Dev 1 | `_build_units`, `_classify_units` | T3.1 | Samples: `bg` → background; L2 circles → decoration; badge/cta/ribbon groups → one unit each; no reliance on ids or filenames |
| T1.2 | P0 | Dev 1 | `_reading_blocks`, `_source_relationships` (prominence, alignment, overlaps) | T1.1, T2.1 | L2 blocks: [badge, logo], [product], [headline], [subhead], [cta]; L3 badges form one sibling set |
| T1.3 | P0 | Dev 1 | `_candidate_anchor` + `_place_decorations` | T1.2 | L1 anchor candidate passes H1–H8 with zero unintended collisions |
| T2.2 | P0 | Dev 2 | `_fit_text`, `_fit_image`, `fit_candidate` (singletons, backgrounds) | T2.1, T3.1 | No text overflow (H6) on all samples; image aspect within 1% |
| T2.3 | P0 | Dev 2 | `_fit_unit` for groups and rotated units; labels centered in shapes | T2.2 | H7 holds for all groups in L2 and L3; ribbon rotated AABB inside canvas at 728×90 |
| T3.4 | P0 | Dev 3 | `validate` (H1–H8, S1–S7) and `_select` | T3.1 | Unit cases: a hand-made overlapping layout fails H8; a deliberately stretched image fails H5 |
| T1.4 | P0 | Dev 1 | `_candidate_lanes` (both orientations, partition enumeration with cap) | T1.2 | L2 produces ≥ 3 lane candidates; a synthetic 30-element design generates candidates in < 1 s |
| T2.4 | P0 | Dev 2 | `_enforce_hierarchy` + sibling consistency | T2.2 | Source font-size order preserved in every output; L3 badge labels share one size |
| T3.5 | P0 | Dev 3 | `_repair` + fallback reasons + floor-hit logging | T3.4 | Forced-failure test (all candidates invalid) yields valid JSON with a `FALLBACK:` line on stderr |
| T3.6 | P0 | Dev 3 | Docker build and `--network none` run on all samples; propose pinning `cr-renderer` to the tested commit | T3.2 | Docker output JSON identical to local output; pin proposed in a PR, merged only with team agreement |
| T3.7 | P0 | Dev 3 | README: "Approach" section + run/eval instructions | T3.5 | A teammate can reproduce the evaluation from the README alone |
| T1.5 | P1 | Dev 1 | Corner-pinned variants | T1.4 | Pinned variant chosen only when score improves; no new collisions |
| T1.6 | P1 | Dev 1 | Implicit containment units: an ungrouped shape that fully contains a higher-z text element travels with it | T1.1 | A synthetic button without `group_id` keeps its label inside |
| T2.5 | P1 | Dev 2 | Shape widening when a label can't fit at the floor size | T2.3 | Long-label synthetic button: label ≥ floor size, shape stays within its slot |
| T3.8 | P1 | Dev 3 | Invariance and determinism checks in `evaluate.py` (§9.3) | T3.3 | All invariance tests pass |
| T1.7 | P2 | Dev 1 | Post-fit re-spacing of lanes using `used` boxes | T2.2 | S6 improves on ≥ 70% of evaluation pairs without hurting S2 |
| T3.9 | P2 | Dev 3 | Weight tuning pass with a frozen held-out target subset | T3.4 | Tuned weights improve held-out pairs, not just tuned ones |

---

## 8. Milestones

Effort is measured in **work sessions**: one focused block of work by one developer. No calendar dates are assumed.
The order matters more than the counts, which are estimates.

| Milestone | Sessions (estimate) | Exit criteria |
|---|---|---|
| **M0 — Interface checkpoint** | 1 (all three together) | T0.1 done: stub `resize.py` merged with §0 types, every function signature, `TUNABLES`, section banners. Branch and merge rules agreed. **No parallel implementation starts before this.** |
| **M1 — Early end-to-end integration** | +1–2 | T3.1, T3.2, T2.1, T3.3: the pipeline runs end to end on real code paths (anchor stub, naive fit, real validation, fallback). `evaluate.py` produces a contact sheet. Docker builds. *From here on, the main branch always produces valid output.* |
| **M2 — Core features in parallel** | +3–4 per developer | All remaining P0 tasks. Merge to main at least once per session, behind the always-valid guarantee. |
| **M3 — Integration and tuning** | +2 | Full evaluation set run. Weights and thresholds adjusted only with cross-sample evidence. P1 tasks as time allows. |
| **M4 — Hardening and readiness** | +1 | Definition of done (§10) verified, including the Docker offline run and README. Iteration-2 backlog written from observed failures. |

Integration rhythm: short sync at the end of each session. The integration owner (Dev 3) runs `evaluate.py` after
each merge and posts the contact sheet so regressions are visible immediately.

---

## 9. Evaluation plan

### 9.1 What we run

For every **(design, target)** pair, `tools/evaluate.py`:

1. Runs our `resize()` and the baseline. The baseline is reproduced inside the tool so production code doesn't keep it.
2. Renders both through `renderer.converter.convert.render_design_to_png`, the same path as `main.py`.
3. Runs the hard checks H1–H8, plus schema validation (H9) when `jsonschema` is installed. `jsonschema` is a
   dev-only dependency and does not go into `requirements.txt`.
4. Records the soft metrics, the chosen candidate, whether the fallback was used, below-floor text, and runtime.
5. Writes `output/eval/summary.csv` and a contact sheet: source | baseline | ours, one row per pair.

### 9.2 Inputs

- **All three samples**, each at its own target **and** at every target below.
- **Our target set** (team-chosen stand-in for the unpublished catalogue, U11):
  1080×1080 (square post), 1080×1350 (portrait post), 1080×1920 (story), 1200×628 (link/ad landscape),
  1280×720 (video thumbnail), 1500×500 (header), 1584×396 (wide banner), 728×90 (leaderboard),
  300×250 (medium rectangle), 160×600 (skyscraper), 320×50 (mobile banner), 2480×3508 (A4 print, large).
- **Synthetic variants** built by transforming the samples. No new assets or fonts are added:
  - Rename every `id`, `group_id`, and `asset_id` to random strings. Output geometry must be identical.
  - Shuffle the element list order (allowed to change ties only; must stay valid).
  - Three to six badge groups instead of three.
  - Doubled text length (subhead), to stress fitting.
  - A design with no `group_id`, and one with a single element.

### 9.3 Pass criteria

- **JSON validity:** 100% of pairs pass H1–H9. Any fallback is listed explicitly in the summary.
- **Invariance:** the renamed-id/asset variant produces identical geometry. Running twice produces byte-identical JSON.
- **Baseline comparison (visual):** two developers who did not write the candidate in question review the contact
  sheet against the five rubric qualities (C9). For each pair they record *better / same / worse* than the baseline.
  Target: no "worse" on any pair, and "better" on all Level-2 and wide/tall pairs.
- **Sample-specific:** see §1. Level 3 must render with all 17 elements, nothing off-canvas, and every below-floor
  element listed in the log.

### 9.4 Guarding against overfitting

- No rule may branch on ids, `asset_id`s, content strings, or exact canvas sizes (enforced by the renaming test and
  code review).
- Tunable changes must improve the summary across the whole target set, not only one sample.
- Before M3, freeze a held-out subset (for example 320×50, 160×600, 2480×3508) and only look at it after tuning.

---

## 10. Definition of done

Iteration 1 is done when **all** of the following are true:

- [ ] `resize(source, target_canvas)` keeps its signature. All production changes are inside `engine/resize.py`.
- [ ] All three samples: zero hard failures; Level 1 preserves its composition; Level 2 is visibly better than the
      baseline; Level 3 renders without a crash, with all elements present, the rotated ribbon on canvas, and the
      missing asset placed but not hidden.
- [ ] Full evaluation set (§9.2): zero crashes, zero hard failures except documented fallback cases, and no
      "worse than baseline" review verdicts.
- [ ] Invariance and determinism tests pass.
- [ ] `resize()` never raises. A forced internal error produces valid fallback output and a `FALLBACK:` stderr line.
- [ ] `docker build --platform linux/amd64` and `docker run --network none` succeed and match local outputs.
- [ ] Runtime stays reasonable: target under a few seconds per task locally, not counting rendering (C14).
- [ ] Code runs on Python 3.11 (Docker) and 3.13 (local venv).
- [ ] README contains a short "Approach" section, run instructions, and the evaluation command.
- [ ] Every `TUNABLES` entry is labeled provisional, and §11 and §12 of this document are updated with what we
      actually observed.

---

## 11. Risks and organizer questions

### 11.1 Risks

| Risk | Likelihood / impact | Mitigation | Owner |
|---|---|---|---|
| **Ambiguous backgrounds**: a partial-canvas panel, a gradient shape, or several stacked full-bleed layers get misclassified | Med / High | Coverage + z rule with logging. Containment units (T1.6). Synthetic tests with a 70%-coverage panel. | Dev 1 |
| **Intentional overlaps** reported as collisions, or real collisions excused | Med / Med | "Intentional" is defined only from source evidence (§5.4). Text-on-text is always hard. Reviewers check S2 on contact sheets. | Dev 3 |
| **Rotated bounds**: clipping at canvas edges, or labels drifting out of rotated shapes | Med / Med | Rotated AABB everywhere. Groups scale uniformly, which preserves the rotated geometry exactly. H4 and H7 checks. | Dev 2 / Dev 3 |
| **Text-measurement mismatch** with the official renderer (letter spacing R3, font fallback R14, version drift R12, Pillow fallback differences) | Med / High | Reuse the renderer's own wrap code. Add letter spacing to widths. Pin `cr-renderer`. A slight internal safety pad on heights. Regression check against the rendered PNG in T2.1. | Dev 2 |
| **Arbitrary element counts**: partition explosion, slow runs, tiny content | Med / Med | Partition cap with balanced partitions above `n = 8`. Block-level lanes. A 30-element synthetic test. | Dev 1 |
| **Missing assets**: crash, or a jarring placeholder | Low (no I/O) / Med | No asset I/O in iteration 1. Placement from box geometry. The placeholder is visible by design (U3, Q4). | Dev 2 |
| **Tiny canvases**: legibility collapses | High / Med | Prominence-ordered allocation, floor logging, explicit tradeoff (§5.6). No hiding. | Dev 2 / Dev 3 |
| **Single-file merge conflicts** with three developers in `resize.py` | High / Low | Section banners, small frequent merges, §0 changes reviewed. Dev 3 merges. | Dev 3 |
| **Overfitting to three samples** | High / High | Renaming invariance, the broad target set, a held-out subset, and the review rule in §9.4. | All |
| **Schema or rules change** (v0.9 is provisional) | Med / Med | Copy-through output (P4). Re-run evaluation on any new materials. | Dev 3 |
| **Docker/amd64 surprises** on the reference Apple hardware (emulation, skia libs) | Low / High | Docker run at M1 and M4. No new dependencies. | Dev 3 |

### 11.2 Questions for organizers

1. **Q1 (U1):** Is intentional bleed beyond the canvas (like `deco_circle_1`) allowed in the output, or does
   "nothing off-canvas" apply to every element? Is the check done on the unrotated box or the drawn extent (U2)?
2. **Q2 (U4):** When a tiny target cannot fit every element readably, how is that scored? Is a below-readable but
   present element preferred to anything else? (We assume yes, and we never hide content.)
3. **Q3 (U6):** Which elements count as "full-bleed" and are exempt from the proportion rule? Only `fit: "cover"`, or
   any image covering the canvas? What about full-canvas shapes?
4. **Q4 (U3, R7):** For a missing asset, is the gray placeholder also rendered at scoring time? Is it judged as part of
   the design?
5. **Q5 (U8):** Is a viewer add-on required for stage one, as FAQ Q5 suggests, or only for the finale?
6. **Q6 (U11):** Can teams have the target-size catalogue that RULES calls a fixed material?
7. **Q7:** Is the ~5% proportion tolerance measured against the source element box or the asset's intrinsic pixels?
8. **Q8:** May production code be split into extra modules under `engine/`? The Dockerfile copies the whole folder.
   (We stay in `resize.py` until this is answered.)
9. **Q9 (R11, R3):** The renderer treats `style_runs.end` as inclusive and ignores `letter_spacing` when wrapping. Is
   the official scoring renderer identical to the vendored one?
10. **Q10 (U10):** What are the exact reference hardware limits, and is the image run natively or under amd64 emulation?

---

## 12. Iteration-two options

Pick from this list **based on observed iteration-1 failures**. Each option lists the failure that would justify it.

| If we observe… | Consider… |
|---|---|
| Text placed over busy regions of the background, or low contrast | Read asset pixels (read-only, never modified, C7) to estimate local luminance and variance, then prefer calm, high-contrast regions for text. |
| Wasted space around images with transparent padding (the bottle asset is ~43% × 69% visible) | Use the asset's alpha bounding box for collision and spacing while keeping the box ratio for C5. |
| Lane partitions look mechanical or unbalanced with many units | A small deterministic local search over slot boxes, or a constraint solver if one fits the dependency budget. |
| Ungrouped "buttons" or panels break apart | Promote T1.6 containment units to P0 and add a proximity-based implicit grouping. |
| Extreme-banner legibility remains poor | A prominence-driven secondary-zone layout (for example a compact strip for small units), pending organizer answers to Q2. |
| Hierarchy feels flat after compression | Keep minimum font-size *ratios* between hierarchy tiers, not only their order. |
| Corner badges or ribbons lose their character | Strengthen corner pinning and allow rotated units to reuse their source corner relationship. |
| Selection picks layouts humans dislike | A local vision-language model as a critic, re-ranking the top 3 candidates. Only within the approved model list and size limit, with weights public if adapted. |
| Finale preparation | Return the top-k candidates as options, user locks, and adaptive copy (finale-only per RULES). |

---

## 13. Appendix: how this plan changed the original proposal

The four-stage plan (analyze → generate → fit → validate) is kept. These refinements came from reading the code:

1. **Slot interface between Dev 1 and Dev 2.** Generation outputs slots, and fitting fills them. Without a fixed
   boundary like this, parallel work on positions and sizes would constantly collide.
2. **Stacking and side-by-side are unified as "lanes".** Both are produced from reading-order blocks, so tall, wide,
   and in-between targets share one code path. That path supports any number of elements through bounded partition
   enumeration.
3. **Renderer quirks drive the fitting rules** (R1–R5): text top-alignment, box height ignored, letter spacing missing
   from wrap, overflow of long words, and zero-size crashes. Ignoring any of these would make plausible-looking JSON
   render badly, and C8 scores both artifacts.
4. **`resize()` must never raise**, because `main.py` aborts the whole run on the first exception (R8).
5. **Hierarchy is preserved as font-size order, not fixed ratios**, plus equal sizing for sibling units such as
   badges. Fixed ratios would make one hard-to-fit element shrink all text.
6. **Ink boxes for relationships.** Generous text boxes in the source would otherwise merge separate rows (Level-2
   subhead and CTA).
7. **Decorations are clamped inside the canvas** despite the source bleed, because the structural check is stated as
   hard. This is flagged as Q1.
8. **No asset I/O in iteration 1.** This removes the missing-asset failure mode and keeps us far from the
   disqualifying rule. Pixel-aware ideas are deferred to iteration 2 with a stated trigger.
9. **Renaming-invariance test** to show, not just claim, that nothing depends on ids, filenames, or labels.
10. **Stricter internal tolerances** (1% aspect, rotated bounds) than the published ones, for safety headroom.
