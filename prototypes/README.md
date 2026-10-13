# Prototype resize engines

Evidence-only prototypes from the 2026-10-13 analysis (see
`docs/analysis-2026-10-13.md` on the `oz-hypothesis` branch). None of them is
production code; `engine/resize.py` on `main` is the shipped engine.

| File | What it is | Verdict |
|---|---|---|
| `proto_blocks_lanes.py` | Blocks-and-Lanes: analyze → anchor/lane candidates → integer-size text fit → gates → lexicographic selection → staged fallback | **Recommended skeleton to port.** Known bugs: `fallback()` must consume sanitized dicts; `NaN` passes through. |
| `mini_floor_engine.py` | ~130-line floor engine: rigid units, full-bleed background, balanced lanes at a common scale, no text measurement | Day-1 floor engine candidate. Pure Python, no renderer imports. |
| `grammar_engine.py` | Role inference + aspect-class layout grammars | Best-looking single renders; broken fallback; discontinuous across neighbouring sizes. |
| `opt_proto.py` | Objective-driven local search per unit | Chaotic under tiny input perturbations; keep only its metrics-cache idea. |

## Running

All three measuring prototypes import the repo's vendored renderer
(`renderer/converter`) and locate the repo root relative to this directory, so
run them from the repo root inside the project venv:

```sh
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

# Blocks-and-Lanes: resizes every input/*.json to six targets, writes JSON+PNG to prototypes/out/
python prototypes/proto_blocks_lanes.py

# Optimizer: one sample, one target
python prototypes/opt_proto.py input/chameleon_sample_level2_core.json 1200 628 output/proto_l2_1200x628 500

# Grammar engine and floor engine expose resize(source, target_canvas); import them:
python -c "import sys; sys.path.insert(0,'prototypes'); import grammar_engine, mini_floor_engine; print('ok')"
```

Fit results differ slightly between macOS (CoreText, fractional glyph advances)
and the Linux container (FreeType, integer advances); that is expected and is
why the recommended engine fits text with slack.
