"""Prototype of the refined template+score resize (NOT production). Module boundaries
are marked with # ===== banners to mirror the proposed engine/layout/ package."""
import sys, math, json, itertools, time, os
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root; this file lives in <repo>/prototypes/
sys.path.insert(0, REPO)
import skia
from renderer.converter.brand_fonts import load_brand_font_manager
from renderer.converter.wrap import char_widths, wrap_text
from renderer.converter.convert import _normalize_weight, _weight_style_to_font_style, _BOLD_WEIGHTS

# ===== tunables =====
READABLE_MIN = 10.0     # px; floor for the largest text in a text-bearing unit
MARGIN_FRAC, GAP_FRAC = 0.04, 0.025
W_READ, W_COLL, W_FID, W_UTIL = 0.35, 0.30, 0.20, 0.15

# ===== geometry =====
def aabb(e, x=None, y=None, w=None, h=None):
    x = e["x"] if x is None else x; y = e["y"] if y is None else y
    w = e["width"] if w is None else w; h = e["height"] if h is None else h
    rot = float(e.get("rotation") or 0)
    if not rot: return (x, y, w, h)
    a = math.radians(rot); c, s = abs(math.cos(a)), abs(math.sin(a))
    bw, bh = w * c + h * s, w * s + h * c
    return (x + w / 2 - bw / 2, y + h / 2 - bh / 2, bw, bh)

def union(bs):
    x0 = min(b[0] for b in bs); y0 = min(b[1] for b in bs)
    return (x0, y0, max(b[0] + b[2] for b in bs) - x0, max(b[1] + b[3] for b in bs) - y0)

def intentional(a, b):
    ox = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]); oy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return ox >= 0.5 * min(a[2], b[2]) and oy >= 0.5 * min(a[3], b[3])

def inter(a, b):
    return max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))

def alpha(e):
    fc = (e.get("fill_color") or "").strip()
    try: a = float(fc[5:-1].split(",")[3]) if fc.startswith("rgba") else 1.0
    except Exception: a = 1.0
    return a * float(e.get("opacity", 1.0))

# ===== textfit (renderer parity) =====
FM = load_brand_font_manager(REPO + "/brand_kit/fonts")
_MET = {}
def style_of(e):
    weight = _normalize_weight(e.get("font_weight")); bold = weight in _BOLD_WEIGHTS
    italic = (e.get("font_style") == "italic") or (e.get("italic") is True)
    return str(e.get("font_family", "Montserrat")), weight, _weight_style_to_font_style(bold, italic)

def metrics(fam, weight, style, fs):
    key = (fam, weight, style, fs)
    if key not in _MET:
        tf = skia.Typeface.MakeFromData(FM.lookup(fam, weight, style)); m = skia.Font(tf, fs).getMetrics()
        _MET[key] = (-m.fAscent, m.fDescent, m.fCapHeight)
    return _MET[key]

def measure(e, fs, box_w):
    """Mirror convert.py/_append_text + wrap.py; fs is the INT the renderer will draw with."""
    fam, weight, style = style_of(e); content = str(e.get("content", ""))
    widths = char_widths(FM, fam, weight, style, float(fs), content)
    wrapped = wrap_text(content, widths, box_w)
    ls = float(e.get("letter_spacing", 0) or 0); lines = wrapped.split("\n"); i = 0; maxw = 0.0
    for ln in lines:
        maxw = max(maxw, sum(widths[i:i + len(ln)]) + ls * max(0, len(ln) - 1)); i += len(ln) + 1
    asc, desc, cap = metrics(fam, weight, style, fs)
    lh = float(e.get("line_height", 1.2) or 1.2)
    return len(lines), maxw, (len(lines) - 1) * fs * lh + asc + desc

def fit_text(e, box_w, fs_max, box_h=None):
    """Largest int fs in [1, fs_max] whose wrapped lines fit box_w (incl. letter spacing, longest word) and box_h."""
    fs_max = max(1, int(fs_max)); ml = e.get("max_lines")
    def ok(fs):
        n, w, h = measure(e, fs, box_w)
        return w <= box_w + 0.5 and (box_h is None or h <= box_h + 0.5) and (not ml or n <= ml), (n, w, h)
    lo, hi = 1, fs_max
    while lo < hi:                       # binary search, then verify (predicate is monotone in practice)
        mid = (lo + hi + 1) // 2
        if ok(mid)[0]: lo = mid
        else: hi = mid - 1
    good, info = ok(lo)
    while not good and lo > 1: lo -= 1; good, info = ok(lo)
    return lo, info

# ===== analyze =====
def analyze(source):
    SW, SH = source["canvas"]["width"], source["canvas"]["height"]; els = source["elements"]
    units = {}
    for e in els: units.setdefault(e.get("group_id") or ("__" + e["id"]), []).append(e)
    bg, deco, content = [], [], []
    for key, mem in units.items():
        ub = union([aabb(m) for m in mem]); grouped = not key.startswith("__")
        cov = inter(ub, (0, 0, SW, SH)) / (SW * SH)
        if not grouped and all(m["type"] != "text" for m in mem) and cov >= 0.9: bg += mem; continue
        if not grouped and all(m["type"] == "shape" for m in mem) and (
                ub[0] < 0 or ub[1] < 0 or ub[0] + ub[2] > SW or ub[1] + ub[3] > SH or alpha(mem[0]) <= 0.5):
            deco += mem; continue
        # ink box: text singleton tightened by measured extent at source size (text is top-aligned, box h unused)
        ink = ub
        if len(mem) == 1 and mem[0]["type"] == "text":
            n, w, h = measure(mem[0], max(1, int(mem[0]["font_size"])), mem[0]["width"]); ink = (ub[0], ub[1], ub[2], h)
        texts = [m for m in mem if m["type"] == "text"]
        content.append(dict(key=key, mem=mem, box=ub, ink=ink, rigid=not (len(mem) == 1 and mem[0]["type"] == "text"),
                            fs_max=max([m["font_size"] for m in texts], default=0), has_img=any(m["type"] == "image" for m in mem)))
    # reading-order blocks: cluster by ink-box y overlap (>= 50% of the smaller), order by y then x
    content.sort(key=lambda u: (u["ink"][1], u["ink"][0]))
    blocks = []
    for u in content:
        if blocks:
            b = blocks[-1]; by0 = min(v["ink"][1] for v in b); by1 = max(v["ink"][1] + v["ink"][3] for v in b)
            ov = min(by1, u["ink"][1] + u["ink"][3]) - max(by0, u["ink"][1])
            if ov >= 0.5 * min(u["ink"][3], by1 - by0): b.append(u); continue
        blocks.append([u])
    for b in blocks: b.sort(key=lambda u: u["box"][0])
    return dict(SW=SW, SH=SH, els=els, bg=bg, deco=deco, blocks=blocks, content=content,
                fs_top=max([u["fs_max"] for u in content], default=1))

# ===== candidates + fit =====
def block_box(b): return union([u["box"] for u in b])

def unit_scale(u, k, floor):
    """Readability floor first: a text-bearing unit never scales below floor/fs_max (its biggest text hits the floor)."""
    if u["fs_max"] > 0: return max(k, floor / u["fs_max"])
    return k

def place_unit(out, u, k, x, y, lane_w=None, floor=0.0):
    """Returns the occupied (x,y,w,h). Rigid: uniform scale k about unit origin (keeps wrap + rotation exact).
    Text singleton: box = lane width, font refit by real wrap."""
    ub = u["box"]
    if u["rigid"]:
        for m in u["mem"]:
            o = out[m["id"]]; cx = m["x"] + m["width"] / 2; cy = m["y"] + m["height"] / 2
            w, h = m["width"] * k, m["height"] * k
            o.update(x=x + (cx - ub[0]) * k - w / 2, y=y + (cy - ub[1]) * k - h / 2, width=w, height=h)
            if m["type"] == "text": o["font_size"] = max(1, int(m["font_size"] * k))
        return (x, y, ub[2] * k, ub[3] * k)
    e = u["mem"][0]; o = out[e["id"]]
    bw = lane_w if lane_w is not None else max(8.0, e["width"] * k)
    cap = max(e["font_size"] * k, floor if u["fs_max"] > 0 else 1)
    fs, (n, w, h) = fit_text(e, bw, cap)
    o.update(x=x, y=y, width=bw, height=math.ceil(h) + 1, font_size=fs)
    return (x, y, bw, math.ceil(h) + 1)

def layout_lanes(an, T, lanes, out, floor):
    """lanes: list of lists of block indices. Wide target -> columns stacking vertically; tall -> rows laid horizontally.
    Finds the largest common scale k such that every lane fits (text at max(k*fs, floor))."""
    TW, TH = T["width"], T["height"]; wide = (TW / TH) >= (an["SW"] / an["SH"])
    margin = max(4.0, MARGIN_FRAC * min(TW, TH)); gap = max(4.0, GAP_FRAC * min(TW, TH))
    along, across = (TH - 2 * margin, TW - 2 * margin) if wide else (TW - 2 * margin, TH - 2 * margin)
    blocks = an["blocks"]
    def bdim(bi):  # source block (w,h) -> (across_extent, along_extent)
        bb = block_box(blocks[bi]); return (bb[2], bb[3]) if wide else (bb[3], bb[2])
    shares = [max(bdim(bi)[0] for bi in L) for L in lanes]; tot = sum(shares)
    lane_ext = [(across - gap * (len(lanes) - 1)) * s / tot for s in shares]

    def sim(k, do_place):
        """Simulate (or place) at scale k; returns True if every lane fits."""
        fits = True; pos = margin; placed = {}
        for L, lw in zip(lanes, lane_ext):
            items = []  # (block idx, across_size, along_size) at this k
            for bi in L:
                b = blocks[bi]
                if len(b) == 1 and not b[0]["rigid"]:
                    u = b[0]; e = u["mem"][0]; cap = max(e["font_size"] * k, floor)
                    bw = lw if wide else min(lw * 0.0 + (e["width"] * max(k, floor / e["font_size"])), along)
                    fs, (n, w, h) = fit_text(e, bw, cap)
                    items.append((bi, (bw, h + 1) if wide else (h + 1, bw)))
                else:
                    ks = max(unit_scale(u, k, floor) for u in b); bb = block_box(b)
                    items.append((bi, (bb[2] * ks, bb[3] * ks) if wide else (bb[3] * ks, bb[2] * ks)))
            need = sum(it[1][1] for it in items) + gap * (len(items) - 1)
            if need > along + 0.5 or any(it[1][0] > lw + 0.5 for it in items): fits = False
            if do_place:
                cur = margin + max(0, (along - need) / 2)
                for bi, (ax, al) in items:
                    b = blocks[bi]; bb = block_box(b)
                    ks = max(unit_scale(u, k, floor) for u in b)
                    if wide:
                        bx = pos + (lw - ax) / 2; by = cur
                        if len(b) == 1 and not b[0]["rigid"]: place_unit(out, b[0], k, pos, by, lane_w=lw, floor=floor)
                        else:
                            for u in b: place_unit(out, u, ks, bx + (u["box"][0] - bb[0]) * ks, by + (u["box"][1] - bb[1]) * ks)
                    else:
                        by = pos + (lw - ax) / 2; bx = cur
                        if len(b) == 1 and not b[0]["rigid"]: place_unit(out, b[0], k, bx, by, lane_w=al, floor=floor)
                        else:
                            for u in b: place_unit(out, u, ks, bx + (u["box"][0] - bb[0]) * ks, by + (u["box"][1] - bb[1]) * ks)
                    cur += al + gap
            pos += lw + gap
        return fits
    lo, hi = 0.0, max(TW / an["SW"], TH / an["SH"])
    if not sim(lo + 1e-3, False): return None
    for _ in range(24):
        mid = (lo + hi) / 2
        if sim(mid, False): lo = mid
        else: hi = mid
    sim(lo, True); return lo

def candidates(an, T):
    TW, TH = T["width"], T["height"]; r = (TW / TH) / (an["SW"] / an["SH"]); nb = len(an["blocks"])
    cands = [("anchor", None), ("stack", [[i] for i in range(nb)])]
    if r < 1.0: cands.append(("stack-units", "units"))
    if r > 1.0 and nb >= 2:
        dem = [block_box(b)[3] for b in an["blocks"]]; parts = []
        for n in range(2, min(8, nb) + 1):
            for cuts in itertools.combinations(range(1, nb), n - 1):
                idx = [0, *cuts, nb]; L = [list(range(idx[i], idx[i + 1])) for i in range(n)]
                parts.append((max(sum(dem[j] for j in l) for l in L), f"cols{n}:{cuts}", L))
        parts.sort(key=lambda t: t[0]); cands += [(nm, L) for _, nm, L in parts[:120]]
    return cands

def build(an, T, cand):
    TW, TH = T["width"], T["height"]; SW, SH = an["SW"], an["SH"]; sx, sy = TW / SW, TH / SH; s = min(sx, sy)
    out = {e["id"]: dict(e) for e in an["els"]}
    for e in an["bg"]: out[e["id"]].update(x=0, y=0, width=TW, height=TH)
    for e in an["deco"]:
        w, h = e["width"] * s, e["height"] * s; cx, cy = (e["x"] + e["width"] / 2) * sx, (e["y"] + e["height"] / 2) * sy
        out[e["id"]].update(x=min(max(cx - w / 2, 0), TW - w), y=min(max(cy - h / 2, 0), TH - h), width=w, height=h)
    name, lanes = cand; k = None
    if lanes == "units":
        an2 = dict(an); an2["blocks"] = [[u] for u in an["content"]]; lanes = [[i] for i in range(len(an2["blocks"]))]; an = an2
    if lanes is None:
        k = anchor_place(an, T, out)
    else:
        floor = READABLE_MIN
        for _ in range(12):
            k = layout_lanes(an, T, lanes, out, floor)
            if k is not None: break
            floor *= 0.8
        if k is None: return None
    return dict(name=name, out=out, k=k)

def anchor_place(an, T, out):
    """Keep normalized centers at scale s=min(sx,sy); readability floor first; then push-apart along the source flow
    axis (y: blocks are rows) in reading order; if the stack overflows, relax the floor and retry."""
    TW, TH = T["width"], T["height"]; SW, SH = an["SW"], an["SH"]; sx, sy = TW / SW, TH / SH; s = min(sx, sy)
    margin = max(4.0, MARGIN_FRAC * min(TW, TH)); gap = max(4.0, GAP_FRAC * min(TW, TH))
    for attempt in range(12):
        floor = READABLE_MIN * (0.8 ** attempt); occ = []
        for u in an["content"]:
            ub = u["box"]; cx, cy = (ub[0] + ub[2] / 2) * sx, (ub[1] + ub[3] / 2) * sy
            if u["rigid"]:
                ks = min(unit_scale(u, s, floor), max(sx, sy)); w, h = ub[2] * ks, ub[3] * ks
                b = place_unit(out, u, ks, min(max(cx - w / 2, margin), TW - margin - w), min(max(cy - h / 2, margin), TH - margin - h))
            else:
                e = u["mem"][0]; bw = min(e["width"] * sx, TW - 2 * margin); x = min(max(cx - bw / 2, margin), TW - margin - bw)
                b = place_unit(out, u, s, x, cy - (ub[3] * s) / 2, lane_w=bw, floor=floor)
            occ.append([u, list(b), list(b)])
        occ.sort(key=lambda t: t[1][1])
        for i in range(1, len(occ)):
            for j in range(i):
                a, b = occ[j][1], occ[i][1]
                if inter(tuple(a), tuple(b)) > 0 and not intentional(occ[j][0]["ink"], occ[i][0]["ink"]):
                    b[1] = max(b[1], a[1] + a[3] + gap)
        top = min(b[1] for _, b, _o in occ); bottom = max(b[1] + b[3] for _, b, _o in occ)
        if bottom - top > TH - 2 * margin:
            # compact: re-stack BLOCKS (units in a block keep their relative y) in reading order with equal gaps
            byunit = {id(u): b for u, b, _o in occ}; rows = []
            for blk in an["blocks"]:
                bs = [byunit[id(u)] for u in blk]; y0 = min(b[1] for b in bs); y1 = max(b[1] + b[3] for b in bs)
                rows.append((blk, bs, y0, y1 - y0))
            rows.sort(key=lambda r: r[2]); total = sum(r[3] for r in rows); avail = TH - 2 * margin
            if total + gap * (len(rows) - 1) > avail: continue             # cannot fit at this floor: relax, retry
            g = min(3 * gap, (avail - total) / max(1, len(rows) - 1)); cur = margin + (avail - total - g * (len(rows) - 1)) / 2
            for blk, bs, y0, h in rows:
                for b in bs: b[1] += cur - y0
                cur += h + g
            top = min(b[1] for _, b, _o in occ); bottom = max(b[1] + b[3] for _, b, _o in occ)
        shift = min(0.0, (TH - margin) - bottom)                           # pull up only as much as needed
        for u, b, o0 in occ:
            d = b[1] + shift - o0[1]
            for m in u["mem"]: out[m["id"]]["y"] += d
        return s
    return s

# ===== validate + score =====
def evaluate(an, T, fc):
    TW, TH = T["width"], T["height"]; out = fc["out"]; hard = []
    boxes = {}
    for e in an["els"]:
        o = out[e["id"]]; b = aabb(o); boxes[e["id"]] = b
        if o["width"] < 1 or o["height"] < 1: hard.append(f"{e['id']} zero-size")
        if o["type"] == "text" and o["font_size"] < 1: hard.append(f"{e['id']} fs<1")
        if b[0] < -0.5 or b[1] < -0.5 or b[0] + b[2] > TW + 0.5 or b[1] + b[3] > TH + 0.5: hard.append(f"{e['id']} off-canvas")
        if o["type"] == "image" and e not in an["bg"]:
            if abs((o["width"] / o["height"]) / (e["width"] / e["height"]) - 1) > 0.01: hard.append(f"{e['id']} aspect")
        if o["type"] == "text":
            n, w, h = measure(o, int(o["font_size"]), o["width"])
            if w > o["width"] + 0.5 or h > o["height"] + 0.5: hard.append(f"{e['id']} text overflow")
    # collisions between content units not overlapping in source
    content = an["content"]; src_ov = set(); coll = 0.0; area = 0.0; text_text = False
    for i, u in enumerate(content):
        for j in range(i + 1, len(content)):
            v = content[j]
            if intentional(u["ink"], v["ink"]): src_ov.add((i, j))
    for i, u in enumerate(content):
        ub = union([boxes[m["id"]] for m in u["mem"]]); area += ub[2] * ub[3]
        for j in range(i + 1, len(content)):
            v = content[j]; vb = union([boxes[m["id"]] for m in v["mem"]])
            if (i, j) in src_ov: continue
            ov = inter(ub, vb)
            if ov <= 0: continue
            ut, vt = u["mem"][0]["type"] == "text" and not u["rigid"], v["mem"][0]["type"] == "text" and not v["rigid"]
            if ut or vt: text_text = True
            coll += ov * (3 if (ut and v["has_img"]) or (vt and u["has_img"]) else 1)
    if text_text: hard.append("text overlaps other content")
    s_coll = max(0.0, 1 - coll / max(area, 1))
    # readability: prominence weighted; partial credit fs/floor
    num = den = 0.0; k_area = math.sqrt(TW * TH / (an["SW"] * an["SH"]))
    for e in an["els"]:
        if e["type"] != "text": continue
        p = e["font_size"] / an["fs_top"]; fo = out[e["id"]]["font_size"]
        num += p * (0.5 * min(1.0, fo / READABLE_MIN) + 0.5 * min(1.0, fo / (e["font_size"] * k_area))); den += p
    s_read = num / den if den else 1.0
    # fidelity: mean normalized center displacement
    d = []
    for u in content:
        ub = union([boxes[m["id"]] for m in u["mem"]]); sb = u["box"]
        d.append(math.hypot((ub[0] + ub[2] / 2) / TW - (sb[0] + sb[2] / 2) / an["SW"], (ub[1] + ub[3] / 2) / TH - (sb[1] + sb[3] / 2) / an["SH"]))
    s_fid = max(0.0, 1 - (sum(d) / len(d)) / 0.5) if d else 1.0
    cov = sum(union([boxes[m["id"]] for m in u["mem"]])[2] * union([boxes[m["id"]] for m in u["mem"]])[3] for u in content) / (TW * TH)
    s_util = max(0.0, 1 - abs(cov - 0.55) / 0.55)
    r = (TW / TH) / (an["SW"] / an["SH"]); w_fid = W_FID * max(0.0, 1 - abs(math.log(r)) / math.log(2))
    score = (W_READ * s_read + W_COLL * s_coll + w_fid * s_fid + W_UTIL * s_util) / (W_READ + W_COLL + w_fid + W_UTIL)
    return hard, dict(read=round(s_read, 3), coll=round(s_coll, 3), fid=round(s_fid, 3), util=round(s_util, 3), score=round(score, 3))

def fallback(source, T):
    SW, SH = source["canvas"]["width"], source["canvas"]["height"]; TW, TH = T["width"], T["height"]
    s = min(TW / SW, TH / SH); ox, oy = (TW - SW * s) / 2, (TH - SH * s) / 2; res = []
    for e in source["elements"]:
        o = dict(e); o.update(x=e["x"] * s + ox, y=e["y"] * s + oy, width=max(1, e["width"] * s), height=max(1, e["height"] * s))
        if e["type"] == "text": o["font_size"] = max(1, int(e["font_size"] * s))
        b = aabb(o); o["x"] -= min(0, b[0]) + max(0, b[0] + b[2] - TW); o["y"] -= min(0, b[1]) + max(0, b[1] + b[3] - TH)
        res.append(o)
    return {"canvas": {"width": TW, "height": TH}, "elements": res}

def resize(source, T, log=None):
    try:
        an = analyze(source); best = None; rows = []
        for cand in candidates(an, T):
            fc = build(an, T, cand)
            if fc is None: rows.append((cand[0], "infeasible")); continue
            hard, soft = evaluate(an, T, fc); rows.append((cand[0], hard, soft))
            if not hard and (best is None or soft["score"] > best[1]["score"]): best = (fc, soft)
        if log is not None: log.extend(rows)
        if best is None: return fallback(source, T), "FALLBACK"
        fc, soft = best; els = []
        for e in source["elements"]:
            o = fc["out"][e["id"]]
            for f in ("x", "y", "width", "height"): o[f] = round(float(o[f]), 2)
            els.append(o)
        return {"canvas": {"width": T["width"], "height": T["height"]}, "elements": els}, f"{fc['name']} k={fc['k']:.3f} {soft}"
    except Exception as ex:
        return fallback(source, T), f"FALLBACK exc={ex!r}"

def parity(o):
    from renderer.converter.convert import convert_chameleon_to_crello
    ex, _ = convert_chameleon_to_crello(o, FM, REPO + "/brand_kit/assets"); bad = []
    els = sorted(o["elements"], key=lambda e: e.get("z_index", 0))
    for i, e in enumerate(els):
        if e["type"] != "text": continue
        n_conv = ex["text_line"][i][-1] + 1 if ex["text_line"][i] else 1
        n_mine, w, h = measure(e, int(e["font_size"]), e["width"])
        if n_conv != n_mine or w > e["width"] + 0.5 or h > e["height"] + 0.5: bad.append((e["id"], n_conv, n_mine, round(w, 1), e["width"], round(h, 1), e["height"]))
    return bad

if __name__ == "__main__":
    from renderer.converter.convert import render_design_to_png
    from engine.main import self_check
    OUT = os.path.dirname(__file__) + "/" + os.environ.get("OUTDIR", "out"); os.makedirs(OUT, exist_ok=True)
    targets = {"own": None, "story": (1080, 1920), "rect": (300, 250), "sky": (160, 600), "linkad": (1200, 628), "yt": (1280, 720)}
    for p in sorted(os.listdir(REPO + "/input")):
        task = json.load(open(REPO + "/input/" + p)); stem = p.replace("chameleon_sample_", "").replace(".json", "")
        for name, wh in targets.items():
            T = task["target_canvas"] if wh is None else {"width": wh[0], "height": wh[1]}
            t0 = time.perf_counter(); log = []; o, why = resize(task["source"], T, log); dt = time.perf_counter() - t0
            probs = self_check(task["source"], o)
            tag = f"{stem}__{name}_{T['width']}x{T['height']}"
            json.dump(o, open(f"{OUT}/{tag}.json", "w"), indent=1)
            render_design_to_png(o, REPO + "/brand_kit/assets", f"{OUT}/{tag}.png")
            fs = {e["id"]: e["font_size"] for e in o["elements"] if e["type"] == "text"}
            print(f"{tag:<40} {dt*1000:6.0f}ms cands={len(log)} selfcheck={len(probs)} parity_mismatch={parity(o)} -> {why}\n    fonts={fs}")
            for pr in probs: print("     -", pr)
