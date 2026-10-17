# Iteration 1 — conclusion

**Closed 2026-10-17.** The current design and open work are in [`engine.md`](engine.md). The full original plan
is kept in git history:
[`iteration-1-plan.md` at `1239111`](https://github.com/Joonseomoon/chameleon/blob/12391113f8fc3ed13b0d05e7d2880786ddb8c0c6/docs/iteration-1-plan.md).

## Goal

Replace the starter's uniform stretch in `resize()` with a deterministic, group-aware engine that "moves the
furniture instead of stretching the picture": analyze the design, generate several layouts, fit text and images,
then validate and pick one. Production code stays in `engine/resize.py`; three developers own separate sections.

## Outcome against the sample goals

| Sample | Goal | Result |
|---|---|---|
| L1 · 1080² → 1080×1350 | Preserve the composition | ✅ Keeps it: same order and positions, logo undistorted, text the same size. |
| L2 · 1080² → 1600×400 | Visibly better than the stretch | ✅ Reflows into three columns: logo · product · headline + subhead + CTA. |
| L3 · 1080×1350 → 728×90 | No crash, nothing hidden, ribbon on canvas, missing asset placed | ✅ All met — but 7 of 8 texts are below the readability floor (3–9 px). Carried forward as B1. |

## What was built

- **Dev 1 — analysis and layout generation** (PR #4): units, implicit pill/label grouping, backgrounds and
  decorations, reading blocks, corner and band furniture; "anchor" and "lanes" layouts with a readability-floor ladder.
- **Dev 2 — fitting** (PR #5): text measured with the renderer's own wrap code, largest-fitting font search, image
  and group fitting, hierarchy and sibling sizing.
- **Dev 3 — integration** (`dev3-integration`): hard checks, soft scoring, selection, repair, fallback, a `resize()`
  that never raises; Docker with pinned dependencies and `.dockerignore`; CI; `tools/evaluate.py`; tests.
- **Owen's analysis** (PR #3, [`analysis-2026-10-13.md`](analysis-2026-10-13.md)): renderer probes and prototypes
  that shaped much of Dev 1's design, plus hand-built reference layouts used as quality targets.

## Results

- **Evaluation** (37 sample × size pairs): ours passes every hard check in 37/37, the stretch in 1/37 (it distorts
  images). One fallback (L3 at 320×50). Deterministic, rename-invariant, and shuffle-valid in every pair.
- **Tests:** 26 (layout 10, fitting 9, resize 7), passing locally on Python 3.13 and in Docker on 3.11.
- **Docker:** builds and runs offline; two runs give byte-identical JSON. CI runs all of it on every PR.
- **Speed:** under 1 s per sample in emulated Docker; a 129-element design about 4 s.

## Definition of done

- [x] `resize()` signature unchanged; production code only in `engine/resize.py`
- [x] Sample goals met (table above)
- [x] Zero crashes and zero hard failures on the evaluation grid, except the documented fallback
- [x] Deterministic and rename-invariant
- [x] `resize()` never raises; a forced crash produces valid fallback output and a `FALLBACK:` log line
- [x] Docker build and offline run succeed; runs are reproducible
- [x] Runs on Python 3.11 and 3.13
- [ ] README "Approach" section — deferred (B3)
- [ ] Two-person visual review of the contact sheets (B8)

## How the build differed from the plan

- **Assets are read** (alpha bounds only) instead of never opened — transparent padding was distorting spacing.
- **Readability floors** became a size-tiered table with a relaxation ladder instead of one 12 px threshold, and
  `readability_floors()` became the shared public source for all stages.
- **Lanes** start at a 15% aspect change (not 40%); tall targets get only a single stack. Headline + subhead stay
  together; header/footer bands and per-lane growth were added.
- **Scoring:** spacing and alignment are judged relative to the source; a line-break metric (S8) was added; weights
  rebalanced. The originals made L1 abandon its composition and L2 break its headline.
- **Whole-number font sizes**, chosen during fitting — rounding afterwards left a two-line box around a one-line
  headline in Docker.
- **Repair** tries the three best failures, not one. The fallback grows pills to hold their labels and is only held
  to the structural checks.
- **Rename test** renames ids and group ids but not asset ids (renamed assets are, correctly, missing assets).

## Lessons

- **Test where you're scored.** Linux text measurement differs from macOS; two real bugs only showed up in Docker.
- **Score against references, not just rules.** The hand-built L3 layout outscores our engine's pick, which showed
  that layout generation — not scoring — is the bottleneck on small banners.
- **Soft scores are only meaningful after hard checks.** The stretch "outscored" us in 16 pairs while failing 36/37.
- **Wall-clock limits don't belong in CI tests** beyond catching blow-ups; shared runners are several times slower.
- **Weights tuned on three samples are fragile.** Pinning the current choices in tests makes changes deliberate.

## Carried into iteration 2

Everything open is tracked in [`engine.md` §10](engine.md#10-open-work): L3 legibility (B1), L2 landscape choice
(B2), README (B3), decorations behind text (B4), the 320×50 fallback (B5), tall layouts (B6), weight tuning (B7), and
the visual review (B8). The LLM direction and the organizer questions are listed there as pending decisions.
