# Chameleon engine — design and status

**Living document.** Part 1 describes the engine as it is built; Part 2 tracks what's open. Update it in the same PR
as any change it describes. Last updated: 2026-10-17. How we got here: [`iteration-1.md`](iteration-1.md).

All production code is in [`engine/resize.py`](../engine/resize.py). Every number quoted below lives in its `TUNABLES`
dict and is **provisional**.

## Run it

```bash
.venv/bin/python -m engine.main                        # quick local run → output/*_output.{json,png}
docker build --platform linux/amd64 -t chameleon-engine .
docker run --rm --network none -v "$PWD/input:/app/input" -v "$PWD/output:/app/output" chameleon-engine   # as scored
.venv/bin/python tools/evaluate.py                     # full grid → output/eval/ (contact sheets, summary.csv)
.venv/bin/python tests/test_layout.py                  # Dev 1 tests (plain script)
.venv/bin/python -m unittest discover -s tests -p "test_*.py"   # Dev 2 + Dev 3 tests
```

`CHAMELEON_DEBUG=1` prints every candidate's scores. Trust Docker over local runs when judging layouts (R15).
CI ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)) builds the image and runs the engine and all tests on
every push to `main` and every PR.

---

# Part 1 — Design

## 1. Rules we must follow

From [RULES](../RULES.md), [README](../README.md), the [schema](../chameleon_design_schema_v0.9.json),
[FAQ](../FAQ.md#input--output), and the brief (not in the repo):

- `resize(source, target_canvas)` returns `{"canvas", "elements"}`: every source `id` exactly once, nothing added.
- Only `x`, `y`, `width`, `height`, `font_size` may change. `content` and `font_family` are fixed for stage one.
- Nothing off the target canvas. Non-background images keep their proportions (~5%); `fit: "cover"` may crop.
- **Disqualifying:** regenerating or altering asset pixels ([RULES](../RULES.md#disqualifying-regenerating-image-content)).
- Both the JSON and the rendered PNG are scored. Rubric: no unwanted collisions, readable, on-brand, intentional
  (aligned, spaced, hierarchy kept), robust ([RULES](../RULES.md#what-gets-compared-during-scoring)).
- No semantic labels in the input; nothing may be tailored to specific designs or sizes. Hidden targets come from a
  catalogue of real formats ([RULES](../RULES.md#target-canvas-sizes)).
- Scoring runs in Docker with `--network none`: everything installs at build time, no API calls at run time
  ([README](../README.md#docker-how-youll-actually-be-run)).
- No time limit; quality first ([RULES](../RULES.md#performance)).

## 2. Renderer facts that shape the design

Verified in the vendored code. IDs are referenced from code comments.

| # | Fact | So the engine… |
|---|---|---|
| R1 | Text is top-aligned; box height is never used to center or clip. | Computes vertical placement itself; overflow would spill silently. |
| R2 | Wrap is greedy, at spaces only; a long word overflows ([`wrap.py`](../renderer/converter/wrap.py#L23)). | Measures with the renderer's own wrap code. |
| R3 | `letter_spacing` is drawn but ignored when wrapping ([`wrap.py`](../renderer/converter/wrap.py#L8)). | Adds spacing back when checking width. |
| R4 | `max_lines` is ignored ([`convert.py`](../renderer/converter/convert.py#L166)). | Enforces it as a fitting constraint. |
| R5 | `width`, `height`, `font_size` must be > 0 or rendering fails. | Keeps every box ≥ 1 px. |
| R6 | `fit` modes are emulated ([`assets.py`](../renderer/converter/assets.py#L49)). | Locks image box ratios regardless. |
| R7 | A missing asset draws a grey placeholder ([`convert.py`](../renderer/converter/convert.py#L109)). | Places it like any image; never drops it. |
| R8 | [`main.py`](../engine/main.py#L116) catches no exceptions; one crash aborts every task. | `resize()` never raises. |
| R9 | The self-check tests the unrotated box ([`main.py`](../engine/main.py#L64)). | Checks rotated bounds instead. |
| R10 | Shape opacity × fill alpha = visual weight; text ignores `opacity` ([`convert.py`](../renderer/converter/convert.py#L103)). | Uses effective alpha to find decorations. |
| R11 | `style_runs.end` is treated as inclusive ([`convert.py`](../renderer/converter/convert.py#L176)). | Measures the same way; never edits runs. |
| R12 | Glyphs are drawn at `int(font_size)`; line spacing uses the float. | Emits whole-number font sizes only. |
| R13 | Docker runs Python 3.11 (local venv: 3.13). | Code and tests run on both. |
| R14 | An unknown `font_family` falls back silently. | Measurement matches rendering anyway. |
| R15 | macOS (CoreText) and Linux (FreeType) measure text differently, by up to ~8% at small sizes. | Docker runs must match each other; local runs only approximately. Observed: L2 headline 64 px in Docker vs 62 px locally. |

## 3. Design principles

- **Generate several layouts, then pick one.** Hard checks gate; soft scores rank.
- **Dev 1 decides *where*, Dev 2 decides *how big inside*.** Layout generation gives each unit a slot; fitting fills it.
- **Never hide content** (no removal, zero opacity, or near-zero sizes) and **never raise**.
- **Geometry only.** Nothing branches on ids, asset names, content, or exact canvas sizes; a renamed-id test proves it.
- **Deterministic.** No randomness; ties go to source order.
- **Asset files are only read** (alpha bounds), never modified. A missing file falls back to the element's box.
- Output copies every source field and changes only geometry and `font_size`; coordinates rounded to 2 decimals.

## 4. Pipeline and ownership

```
resize(source, target)                                  Dev 3  — sanitize input, never raise, log one line per task
 ├─ analyze_source(source)          → SourceAnalysis    Dev 1  — units, kinds, reading blocks, relationships
 ├─ generate_candidates(analysis)   → [Candidate]       Dev 1  — "anchor" + "lanes" layouts (slot per unit)
 │    readability_floors(analysis, target)               Dev 1  — min text size per unit, shared by all stages
 ├─ fit_candidate(analysis, cand)   → FittedCandidate   Dev 2  — fonts sized, images placed, hierarchy enforced
 ├─ validate(analysis, fitted)      → Report            Dev 3  — hard checks H1–H8 + soft score S1–S8
 ├─ _select → best valid; else _repair the 3 best failures   Dev 3
 └─ fallback_layout                 if nothing passes or anything raises   Dev 3
```

`resize.py` sections: §0 shared types and `TUNABLES` (Dev 3, reviewed by all) · §3 fitting (Dev 2) · §1 analysis and
§2 generation (Dev 1) · §4 validation, §5 selection/fallback, §6 `resize()` (Dev 3). Tests: `tests/test_layout.py`
(Dev 1), `test_fitting.py` (Dev 2), `test_resize.py` (Dev 3). Dev tools: `tools/` (not in the image).

## 5. Analysis (Dev 1)

| Concept | Rule |
|---|---|
| **Unit** | One per `group_id`. An ungrouped text ≥ 90% inside a small (< 30% of canvas), opaque shape painted below it joins that shape. Everything else is a singleton. |
| **Background** | Single text-free unit covering ≥ 90% of the canvas (≥ 60% for `fit: "cover"`), at the bottom of the paint order. Stretched to full bleed. |
| **Decoration** | Shape-only unit with effective alpha ≤ 0.5, or bleeding off the canvas, or opaque and under ≥ 20% of a content unit. |
| **Content** | Everything else; this is what gets laid out. |
| **Ink box** | Visible extent: measured text, image alpha bounds (via `fit`), shapes as drawn. Used for all relationships. |
| **Reading blocks** | Units whose ink overlaps ≥ 50% across the flow axis share a row; rows or columns, whichever gives more blocks. |
| **Furniture** | *Corner*: ≤ 3% of the canvas, within 6% of two edges → pinned. *Band*: ≥ 30% wide, ≤ 4% tall, at top/bottom edge (legal line) → kept at that edge. |
| **Siblings** | Same structure, size within ±20%, same block (L3 badges) → same size. |
| **Prominence** | Half area rank, half font size (or image area) relative to the largest. |

## 6. Layout generation (Dev 1)

Margins follow the source's own margin, clamped to 3–6% of the target's short side; gaps are 2.5%.

**Readability floors** (`readability_floors()`), in judge pixels (PNGs are downscaled to ≤ 2048 px on the long side):

| Target short side | Headline / free text | Label in a shape | Footer band |
|---|---|---|---|
| ≤ 90 px | 8 | 7 | 5 |
| 360 px | 10 | 8 | 7 |
| ≥ 720 px | 12 | 10 | 8 |

Interpolated between rows; +1 px for regular weight, +2 px for faint text. When a layout can't keep every floor, the
floors are scaled down a step at a time (1.0 → 0.85 → … → 0.25 → none). The step used is recorded on the candidate.

- **Anchor** keeps the composition: uniform scale `min(sx, sy)`, units lifted to their floor, centres mapped
  proportionally (edge-hugging units keep their edge distance), new overlaps pushed apart along the flow axis.
- **Lanes** reflow into 1–4 side-by-side columns when the canvas gets ≥ 15% wider, or into one vertical stack when it
  gets ≥ 13% taller. Headline + subhead stay together. Every contiguous split is tried (balanced splits above 8
  groups; ≤ 64 candidates). The largest common scale that fits is found by bisection; each lane may then grow 2×.
  Corner furniture is pinned into the outer lanes; bands span the full width.
- Within ±3% aspect change, only anchor is generated.
- Decorations keep their aspect, scale by `min(sx, sy)`, keep their corner, and are clamped inside the canvas
  (`deco_bleed` flag, pending Q1). They do not avoid content (B4).

## 7. Fitting (Dev 2)

- **Text:** largest whole-number size ≤ the slot's size cap whose wrapped text fits width (with letter spacing),
  height, and `max_lines`; measured with the renderer's wrap code (Pillow approximation if the renderer is missing).
- **Images:** largest box with the source box's aspect ratio inside the slot.
- **Groups** (pill + label, rotated ribbon): scaled uniformly, then each label re-fitted and centred. A simple
  pill + label may widen its pill when the label would be too small (uses a fixed 12 px test, B7).
- **Hierarchy:** text larger than another in the source never ends up smaller; siblings share the smallest size.
  Sizes are only ever lowered.
- Never raises: malformed candidates get a `FIT ERROR` note, overflowing text a `fits = false` report.

## 8. Validation, scoring, selection (Dev 3)

**Hard checks** (any failure rejects the layout):

| # | Check |
|---|---|
| H1 | Same ids, in source order, each once; same `type` |
| H2 | Every field except geometry and `font_size` unchanged |
| H3 | Canvas = target as integers (guaranteed by construction) |
| H4 | Rotated bounds inside the canvas (0.5 px); finite numbers; sizes and `font_size` ≥ 1 |
| H5 | Non-background, non-`cover` images keep the source box ratio within 1% |
| H6 | Measured text fits its box (0.5 px) and `max_lines` |
| H7 | A label that was inside its shape stays inside it |
| H8 | No text overlaps text from another unit (unless they overlapped in the source) |
| H9 | JSON schema validation — `tools/evaluate.py` only, when `jsonschema` is installed |

**Soft score** = weighted sum of 0–1 metrics:

| # | Metric | Weight |
|---|---|---|
| S1 | Readability: size relative to an area-preserving size, plus "at or above its floor", prominence-weighted | 0.25 |
| S2 | No new collisions between content units (text over image ×3) | 0.20 |
| S4 | Composition fidelity: unit centres stay where they were, proportionally | 0.17 |
| S3 | Hierarchy order kept (text by size, images by area) | 0.10 |
| S8 | Line breaks: texts wrap onto no more lines than in the source | 0.08 |
| S5 | Utilization: content covers 55–75% of the canvas | 0.08 |
| S6 | Spacing no less even than in the source | 0.06 |
| S7 | Alignment no worse than in the source | 0.06 |

**Selection:** highest-scoring layout with no hard failure (ties → earlier). If none, `_repair` the three
best-scoring failures (lift sub-pixel boxes, refit text, shrink colliding units, slide inside) and take the first
that passes. Otherwise **fallback**: backgrounds full-bleed, everything else scaled uniformly and centred
(letterbox), pills grown to hold their labels. The fallback guarantees H1, H2, H4, H5 only; on extreme sizes its text
can be ~1 px. If even the fallback raises, the source geometry is returned so the run never aborts.

---

# Part 2 — Status and tracking

## 9. Current status

- **Code:** all stages built. `dev3-integration` (CI green) carries Dev 3's work and is pending merge to `main`.
- **Evaluation** (`tools/evaluate.py`, 2026-10-17, local): 37 pairs = 3 samples × 12 catalogue sizes + their own
  targets. Ours passes every hard check in **37/37**; the starter stretch in 1/37. One fallback (L3 at 320×50).
  Deterministic, rename-invariant, shuffle-valid in every pair. Contact sheets: `output/eval/level{1,2,3}.png`.
- **Samples:** L1 keeps its composition; L2 at 1600×400 reflows into three columns; L3 at 728×90 is valid but its
  text is 3–9 px.
- **Docker:** builds with fully pinned dependencies, runs offline, two runs byte-identical, tests pass on Python 3.11.

## 10. Open work

Priority: **High** = visible quality or a required deliverable. Evidence is from `tools/evaluate.py` unless noted.

| # | Item | Owner | Priority |
|---|---|---|---|
| B1 | **L3 728×90 text is 3–9 px.** Owen's [reference layout](analysis-2026-10-13/reference_layouts/l3_728x90.png) fits 6–12 px with everything kept (our scorer rates it 0.85 vs 0.74 for our pick), so layout generation is the bottleneck. Ideas: size pill labels from the pill (≈ 0.42 × height); give surplus lane width to text lanes (the logo lane wastes ~85 px); stack logo + product in one narrow column. | Dev 1 / Dev 2 | High |
| B2 | **L2 at 1200×628 and 1280×720 picks anchor** (small content, empty sides) over lanes. Likely S4 fidelity outweighing S5/S1. | Dev 3 | High |
| B3 | **README "Approach" section** — a required stage-one deliverable; deferred so far. | TBD | High |
| B4 | **Decorations land behind text** (L2 three-lane pick, L3 1200×628). Steer them off text ink, or add a soft penalty. | Dev 1 / Dev 3 | Med |
| B5 | **L3 at 320×50 falls back** (1 px text). | Dev 1 / Dev 3 | Med |
| B6 | **Tall targets only get one vertical stack** (no rows of side-by-side blocks). | Dev 1 | Med |
| B7 | **Weights tuned on three samples only.** Tune on the full grid, check on held-out sizes (320×50, 160×600, 2480×3508), with the reference layouts and possibly an offline AI judge. | Dev 3 | Med |
| B8 | **Two-person visual review** of the contact sheets (better / same / worse than the stretch, per pair). | All | Med |
| B9 | Pill widening uses a fixed 12 px "too small" test instead of `readability_floors()`. | Dev 2 | Low |
| B10 | `FIT ERROR` is detected by string prefix; add a structured flag. | Dev 2 / Dev 3 | Low |
| B11 | Anchor pushes units apart only on overlap, not when gaps are below the minimum (L2 anchor: subhead nearly touches CTA). | Dev 1 | Low |
| B12 | Install `jsonschema` in the dev venv so the evaluation runs H9. | Dev 3 | Low |
| B13 | Corner-pinned layout variants; post-fit re-spacing of lanes. | Dev 1 | Low |

**Decisions pending:** whether and how to use an LLM (hosted APIs can't run at scoring, `--network none`: a local
model in the image, or offline use for judging/tuning/proposing templates); whether the repo stays public; who sends
the organizer questions; viewer add-on (Q5).

## 11. Risks

| Risk | Mitigation |
|---|---|
| Official renderer or OS differs from ours, so text overflows at scoring (R15, Q9) | Fit with 4% width slack; pinned renderer; CI renders in Linux; ask Q9. |
| Overfitting to three sample designs | Rename/shuffle invariance, 12-size grid, held-out sizes, synthetic variants (not built yet). |
| Ambiguous backgrounds or panels misclassified | Coverage + paint-order rules, containment units; add synthetic test designs. |
| Brand kit swapped for different fonts/assets (same filenames) | All metrics and alpha bounds derived at run time; nothing hard-coded per asset. |
| Schema v0.9 changes | Output copies every field through untouched; rerun evaluation on new materials. |
| Merge conflicts in one shared file | Section ownership, small PRs, CI on every PR. |
| Slower under amd64 emulation | Samples take < 1 s in emulated Docker; a 129-element design ~4 s. |

## 12. Questions for the organizers (not yet sent)

| # | Question | Our assumption meanwhile |
|---|---|---|
| Q1 | May a decoration that bled off the source canvas bleed off the target? Is "off-canvas" judged on the unrotated box or the drawn extent? | No bleed; drawn extent. |
| Q2 | When a tiny target can't fit everything readably, is a present-but-tiny element preferred to anything else? | Yes; never hide. |
| Q3 | Which elements are "full-bleed" and exempt from the proportion rule (only `fit: "cover"`? full-canvas shapes?) | Anything covering ≥ 90% (≥ 60% if `cover`). |
| Q4 | Is the missing-asset grey placeholder rendered and judged at scoring time? | Yes; placed like any image. |
| Q5 | **Is a viewer add-on required for stage one** (FAQ Q5) or only the finale? | Finale only. |
| Q6 | Can we have the target-size catalogue? | Our 12-size stand-in. |
| Q7 | Is the ~5% proportion rule measured on the element box or the asset's pixels? | Element box, 1% internally. |
| Q8 | May production code be split into modules under `engine/`? | Stay in `resize.py`. |
| Q9 | Is the official renderer identical to the vendored one? Is our PNG scored, or our JSON re-rendered — on which OS? | Identical; Linux (Docker). |
| Q10 | Exact reference hardware; native or amd64 emulation? | Emulation. |
| Q11 | **Does any stage allow network access (hosted LLM APIs)?** If not, which local models are allowed, and what image size limit applies? | No network. |

## 13. Ideas for later

Contrast-aware text placement (read background pixels, never modify them) · local search over slot positions ·
minimum size *ratios* between hierarchy tiers · a local vision model re-ranking the top candidates · an LLM proposing
layouts that still go through fit → validate → select · top-k candidates and adaptive copy for the finale.
