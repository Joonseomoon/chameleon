# Developer 2 fitting handoff

The fitting implementation is in section 3 of `engine/resize.py`.
Developer 1 analysis and candidate generation are unchanged. The public
`resize()` still uses the starter baseline; Developer 3 owns connecting
fitting to validation, candidate selection, and fallback.

```python
analysis = analyze_source(source)
for candidate in generate_candidates(analysis, target_canvas):
    fitted = fit_candidate(analysis, candidate, target_canvas)
    design = {
        "canvas": target_canvas,
        "elements": [fitted.elements[i] for i in analysis.order],
    }
    # Validate and score before selecting or rendering this design.
```

`FittedCandidate.elements` contains every original element keyed by ID.
`used` maps unit IDs to the union of their actual rotated element bounds.
`text` maps text IDs to final `TextFit` reports, refreshed after hierarchy
adjustment. `notes` includes errors, overflow, and readability warnings.
A `FIT ERROR` note or a false `TextFit.fits` must be treated as a failed
candidate, not silently accepted. Lower-priority readability warnings do
not mean content was removed. Structural and collision validation remain
Developer 3's responsibility.

Text measurement reuses converter wrapping and matches the installed
renderer’s integer glyph sizing, inclusive style-run endpoints, and
per-span letter spacing. Height is a conservative font-metric envelope,
not a tight visible-pixel bound. Measurements and font work are cached.
If the renderer/font manager is unavailable, Pillow provides approximate
measurements and the candidate includes a diagnostic note.

Images preserve their source box proportions. Groups scale together;
rotated labels retain their center when their measured box height changes.
Unrotated labels keep their source allocation and are centered vertically.
A simple shape-and-label group may widen inside its slot when that improves
an undersized label. This widening heuristic uses the default 12 px floor;
final readability reports use Developer 1's target-dependent full floors,
not the candidate's relaxed rung. No asset pixels are modified.

Hierarchy and sibling consistency lower font sizes only. Very small
canvases can still produce unreadable or overflowing candidates. The fitting
stage reports these limitations; it cannot guarantee a good composition.

Run checks from the repository root:

```bash
.venv/bin/python tests/test_layout.py
.venv/bin/python -m unittest discover -s tests -p test_fitting.py -v
```

The fitting tests cover wrapping, mixed styles, integer glyph sizes,
letter spacing, Pillow fallback, long-word overflow, line limits, alignment,
rotated groups, grouped labels, hierarchy, sibling consistency, malformed
candidate reporting, and sample-wide source preservation and bounds.

Diagnostic previews of each sample's first (anchor) candidate are in
`output/dev2/`. These demonstrate fitting and rendering, not final selection.
The Level 3 missing-asset placeholder is intentional in the supplied input.
