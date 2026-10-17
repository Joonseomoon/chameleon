"""Dev 1 acceptance tests: source analysis and candidate generation (plan tasks T1.1-T1.6).
Run: python -m pytest tests/test_layout.py   (or python tests/test_layout.py)"""
import copy
import glob
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.resize import analyze_source, generate_candidates, _rotated_aabb  # noqa: E402

SAMPLES = {Path(p).stem.split("_")[2]: json.load(open(p)) for p in sorted(glob.glob("input/chameleon_sample_*.json"))}
GRID = [(160, 600), (300, 250), (320, 50), (728, 90), (970, 90), (1080, 1350), (1080, 1920), (1200, 628),
        (1280, 720), (1500, 500), (1600, 400), (2480, 3508), (1080, 1080)]


def _src(name):
    return copy.deepcopy(SAMPLES[name]["source"])


def _members(an, uids):
    return sorted(sorted(an.units[u].member_ids) for u in uids)


def test_level2_classification_and_units():
    an = analyze_source(_src("level2"))
    assert _members(an, an.backgrounds) == [["bg"]]
    assert _members(an, an.decorations) == [["deco_circle_1"], ["deco_circle_2"]]
    assert ["badge_bg", "badge_label"] in _members(an, an.content) and ["cta_bg", "cta_label"] in _members(an, an.content)
    assert [an.units[u].edge_anchor for u in an.content if "badge_bg" in an.units[u].member_ids] == [("top", "left")]


def test_level3_furniture_and_siblings():
    an = analyze_source(_src("level3"))
    by = {m: an.units[u] for u in an.content for m in an.units[u].member_ids}
    assert by["ribbon_bg"].edge_anchor == ("top", "right") and by["holiday_watermark"].edge_anchor == ("bottom", "right")
    assert by["legal_text"].band == "bottom"
    sets = {by[f"badge{i}_bg"].sibling_set for i in (1, 2, 3)}
    assert len(sets) == 1 and None not in sets
    assert by["ribbon_bg"].aspect_locked and by["ribbon_bg"].rotation == -12


def test_implicit_containment_without_group_ids():
    src = _src("level2")
    for e in src["elements"]:
        e.pop("group_id", None)
    an = analyze_source(src)
    assert ["badge_bg", "badge_label"] in _members(an, an.content) and ["cta_bg", "cta_label"] in _members(an, an.content)


def test_never_mutates_and_is_deterministic():
    src = _src("level3")
    before = json.dumps(src, sort_keys=True)
    a1, a2 = analyze_source(src), analyze_source(src)
    assert json.dumps(src, sort_keys=True) == before and a1.blocks == a2.blocks
    t = {"width": 728, "height": 90}
    c1, c2 = generate_candidates(a1, t), generate_candidates(a2, t)
    assert [(c.name, c.slots, c.k) for c in c1] == [(c.name, c.slots, c.k) for c in c2]


def test_rename_invariance():
    src = _src("level2")
    ren = copy.deepcopy(src)
    for i, e in enumerate(ren["elements"]):
        e["id"] = f"zz{i * 7}"
        if e.get("group_id"):
            e["group_id"] = "q" + e["group_id"][::-1]
        if e.get("asset_id"):
            e["asset_id"] = e["asset_id"]  # asset names stay: they are file names, but nothing may branch on them
    t = {"width": 1600, "height": 400}
    a, b = generate_candidates(analyze_source(src), t), generate_candidates(analyze_source(ren), t)
    assert [(c.name, {u: s for u, s in c.slots.items()}) for c in a] == [(c.name, {u: s for u, s in c.slots.items()}) for c in b]


def test_candidates_are_complete_and_inside_the_canvas():
    for name, task in SAMPLES.items():
        an = analyze_source(copy.deepcopy(task["source"]))
        for w, h in GRID:
            cands = generate_candidates(an, {"width": w, "height": h})
            assert cands and cands[0].name == "anchor"
            for c in cands:
                assert set(c.slots) == set(an.content) and set(c.deco) == set(an.decorations), (name, w, h, c.name)
                assert set(c.align) >= set(an.content) and set(c.scales) >= set(an.content)
                for uid, b in list(c.slots.items()) + list(c.deco.items()):
                    assert all(math.isfinite(v) for v in (b.x, b.y, b.w, b.h)) and b.w > 0 and b.h > 0
                    assert b.x >= -0.5 and b.y >= -0.5 and b.right <= w + 0.5 and b.bottom <= h + 0.5, (name, w, h, c.name, uid, b)


def test_level2_wide_offers_several_lane_candidates():
    cands = generate_candidates(analyze_source(_src("level2")), {"width": 1600, "height": 400})
    assert sum(c.lanes >= 2 for c in cands) >= 3


def test_near_identity_returns_anchor_only():
    cands = generate_candidates(analyze_source(_src("level1")), {"width": 1080, "height": 1080})
    assert [c.name for c in cands] == ["anchor"] and abs(cands[0].k - 1.0) < 1e-9


def test_many_elements_stay_fast():
    src = _src("level3")
    base = src["elements"]
    for i in range(3):                       # `base` is the growing list itself, so this doubles it: 129 elements
        for e in base[:]:
            if e["type"] != "image" or e["id"] != "bg":
                c = copy.deepcopy(e)
                c["id"] = f"{e['id']}_c{i}"
                if c.get("group_id"):
                    c["group_id"] = f"{c['group_id']}_c{i}"
                c["y"] = c["y"] * 0.9 + i
                src["elements"].append(c)
    an = analyze_source(src)
    t0 = time.perf_counter()
    for w, h in ((1600, 400), (160, 600), (728, 90)):
        assert generate_candidates(an, {"width": w, "height": h})
    # Guards against blow-ups (e.g. exponential partitions), not a benchmark: shared CI runners are several times
    # slower than a laptop. Measured: ~1.9 s on an M-series Mac, ~4.1 s in Docker under amd64 emulation.
    elapsed = time.perf_counter() - t0
    assert elapsed < 15.0, f"{elapsed:.1f} s for {len(src['elements'])} elements"


def test_degenerate_inputs_do_not_raise():
    one = {"canvas": {"width": 100, "height": 100}, "elements": [
        {"id": "t", "type": "text", "x": 0, "y": 0, "width": 50, "height": 10, "content": "hi", "font_family": "Solstice Sans",
         "font_size": 12, "color": "rgba(0,0,0,1)"}]}
    empty = {"canvas": {"width": 100, "height": 100}, "elements": []}
    for src in (one, empty):
        an = analyze_source(src)
        for w, h in GRID:
            assert generate_candidates(an, {"width": w, "height": h})


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
