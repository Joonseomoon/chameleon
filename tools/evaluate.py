"""Evaluation harness (plan §9, tasks T3.3 and T3.8). Dev-only: not copied into the Docker image.

For every sample in input/ x every target size it runs our resize() and the starter's uniform stretch, renders both
next to the source, and records hard checks, soft scores, the chosen layout, fallbacks, text size and runtime. It also
scores the hand-built reference layouts in docs/analysis-2026-10-13/reference_layouts/ when one exists for a pair.

Run from the repository root:
    .venv/bin/python tools/evaluate.py                      # full grid (about a minute)
    .venv/bin/python tools/evaluate.py --quick              # each sample's own target only
    .venv/bin/python tools/evaluate.py --sizes 728x90 1200x628
    .venv/bin/python tools/evaluate.py --no-render          # numbers only, no contact sheets

Inside the image (Linux text measurement, as in scoring; plan R15):
    docker run --rm --network none --entrypoint python -v "$PWD/input:/app/input:ro" -v "$PWD/tools:/app/tools:ro" \
      -v "$PWD/docs:/app/docs:ro" -v "$PWD/output:/app/output" chameleon-engine tools/evaluate.py

Writes output/eval/summary.csv, output/eval/checks.txt and one contact sheet per sample (output/eval/<sample>.png).
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import random
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine.resize as r  # noqa: E402
from engine.main import self_check  # noqa: E402

# Plan §9.2 target set, plus each sample's own target. HELD_OUT is the §9.4 subset: look at it, never tune on it.
TARGETS = [(1080, 1080), (1080, 1350), (1080, 1920), (1200, 628), (1280, 720), (1500, 500), (1584, 396), (728, 90),
           (300, 250), (160, 600), (320, 50), (2480, 3508)]
HELD_OUT = {(320, 50), (160, 600), (2480, 3508)}
REFERENCES = ROOT / "docs" / "analysis-2026-10-13" / "reference_layouts"
OUT = ROOT / "output" / "eval"
TILE_W, TILE_H = 420, 300


def baseline(source: dict, target: dict) -> dict:
    """The starter's uniform stretch, reproduced here so production code doesn't keep it (plan §9.1)."""
    sx, sy = target["width"] / source["canvas"]["width"], target["height"] / source["canvas"]["height"]
    els = []
    for el in source["elements"]:
        el = dict(el)
        el.update(x=el["x"] * sx, y=el["y"] * sy, width=el["width"] * sx, height=el["height"] * sy)
        if el["type"] == "text":
            el["font_size"] = max(1.0, el["font_size"] * min(sx, sy))
        els.append(el)
    return {"canvas": {"width": target["width"], "height": target["height"]}, "elements": els}


def assess(source: dict, out: dict) -> dict:
    """Hard checks H1-H8 and soft metrics S1-S8 for any output (ours, the baseline or a reference)."""
    an = r.analyze_source(r._sanitize(source))
    w, h = out["canvas"]["width"], out["canvas"]["height"]
    by_id = {e["id"]: e for e in out["elements"]}
    fitted = r.FittedCandidate(None, by_id, {}, {})
    fitted.used = {uid: r._union([r._rotated_aabb(by_id[i]) for i in u.member_ids])
                   for uid, u in an.units.items() if all(i in by_id for i in u.member_ids)}
    hard = r._hard_checks(an, fitted, w, h)
    try:
        soft = r._soft_metrics(an, fitted, w, h)
    except Exception as exc:   # e.g. a reference missing an element
        soft = {k: 0.0 for k in r.TUNABLES["weights"]}
        hard.append(f"SCORE ERROR: {exc}")
    floors = r.readability_floors(an, {"width": w, "height": h})
    texts = [(i, e["font_size"]) for i, e in by_id.items() if e.get("type") == "text"]
    unit_of = {i: uid for uid, u in an.units.items() for i in u.member_ids}
    return {"hard": hard, "soft": soft, "score": sum(r.TUNABLES["weights"][k] * v for k, v in soft.items()),
            "min_font": min((fs for _, fs in texts), default=0),
            "below_floor": sum(fs < floors.get(unit_of.get(i), 0) - 1e-6 for i, fs in texts)}


def run_ours(source: dict, target: dict) -> tuple[dict, str, float]:
    logs: list[str] = []
    with patch.object(r, "_log", logs.append):
        t0 = time.perf_counter()
        out = r.resize(source, target)
        return out, (logs[-1] if logs else ""), time.perf_counter() - t0


def schema_errors(out: dict) -> str:
    """H9 when jsonschema is installed (dev-only dependency, plan §9.1)."""
    try:
        import jsonschema
    except ImportError:
        return "n/a"
    schema = json.loads((ROOT / "chameleon_design_schema_v0.9.json").read_text())
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(out))
    return str(len(errors))


def invariance(source: dict, target: dict, ours: dict) -> dict[str, bool]:
    """T3.8: deterministic rerun, renamed ids and group ids, shuffled element order.
    Asset ids are NOT renamed: Dev 1 reads each asset's alpha bounds by file name (plan P7), so a renamed asset is a
    missing asset and may legitimately change the layout."""
    geometry = lambda o: sorted((e["id"], e["x"], e["y"], e["width"], e["height"], e.get("font_size")) for e in o["elements"])
    rerun = run_ours(source, target)[0]
    renamed = copy.deepcopy(source)
    names = {e["id"]: f"zz{i * 7}" for i, e in enumerate(renamed["elements"])}
    for e in renamed["elements"]:
        e["id"] = names[e["id"]]
        if e.get("group_id"):
            e["group_id"] = "q" + str(e["group_id"])[::-1]
    back = {v: k for k, v in names.items()}
    out_r = run_ours(renamed, target)[0]
    for e in out_r["elements"]:
        e["id"] = back[e["id"]]
    shuffled = copy.deepcopy(source)
    random.Random(0).shuffle(shuffled["elements"])
    out_s = run_ours(shuffled, target)[0]
    return {"deterministic": json.dumps(rerun) == json.dumps(ours),
            "rename_invariant": geometry(out_r) == geometry(ours),
            "shuffle_valid": not assess(shuffled, out_s)["hard"]}


def render(design: dict, name: str, tmp: Path):
    from PIL import Image
    from renderer.converter.convert import render_design_to_png
    path = tmp / f"{name}.png"
    render_design_to_png(design, str(ROOT / "brand_kit" / "assets"), str(path))
    im = Image.open(path).convert("RGB")
    im.thumbnail((TILE_W, TILE_H))
    return im


def contact_sheet(rows: list[tuple[str, list[tuple[str, object]]]], path: Path) -> None:
    """One row per target: label, then panels (caption, image)."""
    from PIL import Image, ImageDraw
    cols = max(len(p) for _, p in rows)
    heights = [max(im.height for _, im in panels) + 44 for _, panels in rows]
    sheet = Image.new("RGB", (cols * (TILE_W + 16) + 16, sum(heights) + 16), "white")
    d = ImageDraw.Draw(sheet)
    y = 8
    for (label, panels), h in zip(rows, heights):
        d.text((16, y), label, fill="black")
        for k, (caption, im) in enumerate(panels):
            x = 16 + k * (TILE_W + 16)
            d.text((x, y + 14), caption, fill=(160, 0, 0) if "FAIL" in caption else (60, 60, 60))
            sheet.paste(im, (x, y + 28))
            d.rectangle([x - 1, y + 27, x + im.width, y + 28 + im.height], outline=(180, 180, 180))
        y += h
    sheet.save(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sizes", nargs="*", help="targets like 728x90 (default: plan §9.2 set + each sample's own)")
    ap.add_argument("--quick", action="store_true", help="each sample's own target only")
    ap.add_argument("--no-render", action="store_true", help="skip contact sheets")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    rows, checks = [], []
    for task_path in sorted((ROOT / "input").glob("*.json")):
        task = json.loads(task_path.read_text())
        source, own = task["source"], (task["target_canvas"]["width"], task["target_canvas"]["height"])
        name = task_path.stem
        level = next((p for p in name.split("_") if p.startswith("level")), name)
        sizes = ([tuple(map(int, s.split("x"))) for s in args.sizes] if args.sizes
                 else [own] if args.quick else [own] + [t for t in TARGETS if t != own])
        sheet_rows = []
        if not args.no_render:
            sheet_rows.append(("SOURCE %dx%d" % (source["canvas"]["width"], source["canvas"]["height"]),
                               [("original", render(source, "src", tmp))]))
        for w, h in sizes:
            target = {"width": w, "height": h}
            ours, log, secs = run_ours(source, target)
            mine, base = assess(source, ours), assess(source, baseline(source, target))
            chosen = log.split(" selected ")[-1].split(" score=")[0] if " selected " in log else (
                log.split(" repaired ")[-1].split(" score=")[0] + " (repaired)" if " repaired " in log else "FALLBACK")
            ref_file = REFERENCES / f"l{level[-1]}_{w}x{h}.json"
            ref = assess(source, json.loads(ref_file.read_text())) if ref_file.exists() else None
            inv = invariance(source, target, ours)
            row = {"sample": name, "target": f"{w}x{h}", "held_out": (w, h) in HELD_OUT, "chosen": chosen,
                   "fallback": "FALLBACK" in log, "hard_failures": len(mine["hard"]),
                   "self_check_warnings": len(self_check(source, ours)), "schema_errors": schema_errors(ours),
                   "score": round(mine["score"], 3), "baseline_score": round(base["score"], 3),
                   "baseline_hard_failures": len(base["hard"]),
                   "reference_score": round(ref["score"], 3) if ref else "",
                   **{k: round(v, 3) for k, v in mine["soft"].items()},
                   "min_font_px": mine["min_font"], "texts_below_floor": mine["below_floor"],
                   "runtime_s": round(secs, 3), **inv}
            rows.append(row)
            for problem in mine["hard"]:
                checks.append(f"{name} {w}x{h}: {problem}")
            for k, ok in inv.items():
                if not ok:
                    checks.append(f"{name} {w}x{h}: {k} FAILED")
            tag = " (held out)" if (w, h) in HELD_OUT else ""
            print(f"{level} {w}x{h}{tag}: {chosen} score {row['score']} vs baseline {row['baseline_score']}"
                  + (f" vs reference {row['reference_score']}" if ref else "")
                  + f", min text {row['min_font_px']} px, {row['hard_failures']} hard failures", flush=True)
            if not args.no_render:
                panels = [(f"stretch  score {row['baseline_score']}"
                           + (f"  FAIL x{len(base['hard'])}" if base["hard"] else ""), render(baseline(source, target), "base", tmp)),
                          (f"ours: {chosen}  score {row['score']}"
                           + (f"  FAIL x{len(mine['hard'])}" if mine["hard"] else ""), render(ours, "ours", tmp))]
                if ref:
                    panels.append((f"reference (hand-built)  score {row['reference_score']}",
                                   render(json.loads(ref_file.read_text()), "ref", tmp)))
                sheet_rows.append((f"{w}x{h}{tag}   min text {row['min_font_px']} px, "
                                   f"{row['texts_below_floor']} below floor", panels))
        if not args.no_render:
            contact_sheet(sheet_rows, OUT / f"{level}.png")
    with open(OUT / "summary.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (OUT / "checks.txt").write_text("\n".join(checks) + "\n" if checks else "all checks passed\n")
    # Soft scores assume the hard checks already passed, so only compare scores where both outputs are valid.
    both = [x for x in rows if not x["hard_failures"] and not x["baseline_hard_failures"]]
    print(f"\n{len(rows)} pairs: ours passes all hard checks in {sum(not x['hard_failures'] for x in rows)}, "
          f"the stretch in {sum(not x['baseline_hard_failures'] for x in rows)}; "
          f"{sum(x['fallback'] for x in rows)} fallback; "
          f"invariance failures {sum(not (x['deterministic'] and x['rename_invariant'] and x['shuffle_valid']) for x in rows)}")
    if both:
        print(f"where both are valid ({len(both)} pairs), ours scores higher in "
              f"{sum(x['score'] > x['baseline_score'] for x in both)}")
    print(f"wrote {OUT / 'summary.csv'}, {OUT / 'checks.txt'}" + ("" if args.no_render else f", contact sheets in {OUT}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
