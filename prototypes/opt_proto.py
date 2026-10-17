"""Prototype of an optimization-first resize(): infer units, then local-search each
unit's box against a deterministic objective. NOT production code; evidence only.

Run:  cd <repo> && . .venv/bin/activate && python prototypes/opt_proto.py <sample.json> <W> <H> <out_prefix> [iters]
"""
import json, math, random, sys, time, copy, os, itertools

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root; this file lives in <repo>/prototypes/
sys.path.insert(0, REPO)
from renderer.converter.brand_fonts import load_brand_font_manager
from renderer.converter.wrap import char_widths, wrap_text
from renderer.converter.convert import render_design_to_png
from engine.main import self_check
import skia

FM = load_brand_font_manager(f"{REPO}/brand_kit/fonts")
_MCACHE = {}

# ----------------------------------------------------------------- text metrics
def _style(el):
    w = str(el.get("font_weight") or "regular").lower()
    w = "bold" if w == "bold" else "regular"
    it = el.get("font_style") == "italic"
    st = "bolditalic" if (w == "bold" and it) else ("bold" if w == "bold" else ("italic" if it else "regular"))
    return w, st

def text_metrics(el):
    """ONE skia measurement per (font, content): per-char em widths + ascent/descent per em.
    Advances are linear in size (verified: ratio 1.0000), so width(fs) = em * fs."""
    fam = str(el.get("font_family", "")); w, st = _style(el); content = str(el.get("content", ""))
    key = (fam, w, st, content)
    if key in _MCACHE:
        return _MCACHE[key]
    ws = char_widths(FM, fam, w, st, 100.0, content) if content else []
    em = [x / 100.0 for x in ws]
    pre = [0.0]
    for e in em:
        pre.append(pre[-1] + e)
    tf = skia.Typeface.MakeFromData(FM.lookup(fam, w, st))
    m = skia.Font(tf, 100.0).getMetrics()
    lw = 0.0; cur = 0.0
    for ch, e in zip(content, em):
        if ch in " \n":
            lw = max(lw, cur); cur = 0.0
        else:
            cur += e
    lw = max(lw, cur)
    res = dict(em=em, pre=pre, asc=-m.fAscent / 100.0, desc=m.fDescent / 100.0, lw=lw, content=content)
    _MCACHE[key] = res
    return res

def wrap_measure(tm, fs, box_w):
    """Mirror of renderer/converter/wrap.py (greedy, spaces only, overflow long words).
    Returns (n_lines, max_line_width). Pure arithmetic, no skia."""
    content = tm["content"]; em = tm["em"]
    if not content:
        return 1, 0.0
    n = 1; line = 0.0; last_sp = None; start = 0; maxw = 0.0
    for i, c in enumerate(content):
        if c == "\n":
            maxw = max(maxw, line); line = 0.0; last_sp = None; start = i + 1; n += 1
            continue
        w = em[i] * fs if i < len(em) else 0.0
        if line + w > box_w and last_sp is not None:
            maxw = max(maxw, (tm["pre"][last_sp] - tm["pre"][start]) * fs)
            n += 1
            start = last_sp + 1
            line = (tm["pre"][i + 1] - tm["pre"][start]) * fs
            last_sp = None
        else:
            line += w
        if c == " ":
            last_sp = i
    maxw = max(maxw, line)
    return n, maxw

def text_block_h(tm, el, fs, n):
    return (n - 1) * fs * float(el.get("line_height", 1.2)) + (tm["asc"] + tm["desc"]) * fs

# ----------------------------------------------------------------- geometry
def aabb(x, y, w, h, rot):
    if not rot:
        return (x, y, w, h)
    a = math.radians(rot); c, s = abs(math.cos(a)), abs(math.sin(a))
    bw, bh = w * c + h * s, w * s + h * c
    return (x + w / 2 - bw / 2, y + h / 2 - bh / 2, bw, bh)

def union(boxes):
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1 - x0, y1 - y0)

def inter(a, b):
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return w * h if (w > 0 and h > 0) else 0.0

def eff_alpha(el):
    a = 1.0
    fc = el.get("fill_color") or el.get("color") or ""
    try:
        if fc.strip().startswith("rgba"):
            a = float(fc.strip()[:-1].split(",")[3])
    except Exception:
        a = 1.0
    try:
        a *= float(el.get("opacity", 1.0))
    except Exception:
        pass
    return a

# ----------------------------------------------------------------- units
class Unit:
    pass

def build_units(src):
    SW, SH = src["canvas"]["width"], src["canvas"]["height"]
    groups = {}; order = []
    for i, e in enumerate(src["elements"]):
        key = e.get("group_id") or f"__{i}"
        if key not in groups:
            groups[key] = []; order.append(key)
        groups[key].append(e)
    units = []
    for key in order:
        ms = groups[key]
        u = Unit(); u.uid = key; u.members = ms
        boxes = [aabb(m["x"], m["y"], m["width"], m["height"], float(m.get("rotation") or 0)) for m in ms]
        u.box = union(boxes)
        bx, by, bw, bh = u.box
        cover = max(0, min(bx + bw, SW) - max(bx, 0)) * max(0, min(by + bh, SH) - max(by, 0)) / (SW * SH)
        types = {m["type"] for m in ms}
        grouped = bool(ms[0].get("group_id"))
        bleed = bx < -0.5 or by < -0.5 or bx + bw > SW + 0.5 or by + bh > SH + 0.5
        if "text" not in types and cover >= 0.9 and not grouped:
            u.kind = "bg"
        elif types == {"shape"} and not grouped and (bleed or max(eff_alpha(m) for m in ms) <= 0.5):
            u.kind = "deco"
        else:
            u.kind = "content"
        u.is_text = len(ms) == 1 and ms[0]["type"] == "text"
        u.is_image = len(ms) == 1 and ms[0]["type"] == "image"
        u.rot = float(ms[0].get("rotation") or 0)
        u.tm = {m["id"]: text_metrics(m) for m in ms if m["type"] == "text"}
        u.n_src = {}
        for m in ms:
            if m["type"] == "text":
                u.n_src[m["id"]], _ = wrap_measure(u.tm[m["id"]], float(m["font_size"]), float(m["width"]))
        # label -> host shape (source containment) and source padding, for groups
        u.host = {}
        for m in ms:
            if m["type"] != "text":
                continue
            mcx, mcy = m["x"] + m["width"] / 2, m["y"] + m["height"] / 2
            for sh in ms:
                if sh["type"] == "shape" and sh["x"] - 0.5 <= mcx <= sh["x"] + sh["width"] + 0.5 and sh["y"] - 0.5 <= mcy <= sh["y"] + sh["height"] + 0.5:
                    n, mw = wrap_measure(u.tm[m["id"]], float(m["font_size"]), float(m["width"]))
                    th = text_block_h(u.tm[m["id"]], m, float(m["font_size"]), n)
                    u.host[m["id"]] = (sh["id"], max(0.0, sh["width"] - mw), max(0.0, sh["height"] - th))
        if u.is_text:
            m = ms[0]; tm = u.tm[m["id"]]; fs = float(m["font_size"])
            n, mw = wrap_measure(tm, fs, float(m["width"]))
            h = text_block_h(tm, m, fs, n)
            al = m.get("text_align", "left")
            x = m["x"] + ((m["width"] - mw) / 2 if al == "center" else (m["width"] - mw if al == "right" else 0))
            u.ink = (x, m["y"], mw, h)
        else:
            u.ink = u.box
        u.src_cx = (u.ink[0] + u.ink[2] / 2) / SW; u.src_cy = (u.ink[1] + u.ink[3] / 2) / SH
        u.area_frac = (bw * bh) / (SW * SH)
        u.anchor = None
        if u.kind == "content" and u.area_frac < 0.03:
            L = bx / SW; R = (SW - bx - bw) / SW; T = by / SH; B = (SH - by - bh) / SH
            hx = "left" if L <= 0.06 else ("right" if R <= 0.06 else None)
            vy = "top" if T <= 0.06 else ("bottom" if B <= 0.06 else None)
            if hx and vy:
                u.anchor = (hx, vy)
        units.append(u)
    maxfs = max([float(m["font_size"]) for u in units for m in u.members if m["type"] == "text"] or [1])
    maximg = max([m["width"] * m["height"] for u in units if u.kind == "content" for m in u.members if m["type"] == "image"] or [1])
    for u in units:
        ps = []
        for m in u.members:
            if m["type"] == "text":
                ps.append(float(m["font_size"]) / maxfs)
            elif m["type"] == "image":
                ps.append(math.sqrt(m["width"] * m["height"] / maximg))
        u.prom = 0.3 + 0.7 * (max(ps) if ps else 0.3)
        u.fs_src = max([float(m["font_size"]) for m in u.members if m["type"] == "text"] or [0])
    content = [u for u in units if u.kind == "content"]
    content.sort(key=lambda u: (u.ink[1] + u.ink[3] / 2, u.ink[0]))
    rows = []
    for u in content:
        placed = False
        for r in rows:
            ry0 = min(v.ink[1] for v in r); ry1 = max(v.ink[1] + v.ink[3] for v in r)
            ov = min(ry1, u.ink[1] + u.ink[3]) - max(ry0, u.ink[1])
            if ov >= 0.5 * min(u.ink[3], ry1 - ry0):
                r.append(u); placed = True; break
        if not placed:
            rows.append([u])
    rows.sort(key=lambda r: min(v.ink[1] for v in r))
    for ri, r in enumerate(rows):
        r.sort(key=lambda v: v.ink[0])
        for ci, v in enumerate(r):
            v.row = ri; v.col = ci
    for u in content:
        u.sig = tuple(sorted(m["type"] for m in u.members)); u.sib = None
    sid = 0
    for r in rows:
        for i, a in enumerate(r):
            for b in r[i + 1:]:
                if a.sig == b.sig and len(a.members) > 1 and abs(a.box[2] * a.box[3] - b.box[2] * b.box[3]) <= 0.2 * max(a.box[2] * a.box[3], b.box[2] * b.box[3]):
                    if a.sib is None:
                        a.sib = sid; sid += 1
                    b.sib = a.sib
    # intentional overlaps: >= 15% of the smaller ink area in the source
    src_over = set()
    for i, a in enumerate(content):
        for b in content[i + 1:]:
            ov = inter(a.ink, b.ink)
            if ov >= 0.15 * min(a.ink[2] * a.ink[3], b.ink[2] * b.ink[3]):
                src_over.add((a.uid, b.uid)); src_over.add((b.uid, a.uid))
    return units, content, rows, src_over

# ----------------------------------------------------------------- placement (state -> boxes)
class State:
    """cx, cy (target px); s (unit scale). Text singles: w (box width), fs (int).
    Groups: r >= 1 = label scale / shape scale ("padding compression")."""
    pass

def place_unit(u, st, W, H):
    out = []
    if u.is_text:
        m = u.members[0]; tm = u.tm[m["id"]]
        fs = max(1, int(st.fs)); w = st.w
        n, mw = wrap_measure(tm, fs, w)
        h = text_block_h(tm, m, fs, n)
        x = st.cx - w / 2; y = st.cy - h / 2
        e = dict(m); e.update(x=x, y=y, width=w, height=h, font_size=fs)
        out.append(e)
        al = m.get("text_align", "left")
        ix = x + ((w - mw) / 2 if al == "center" else (w - mw if al == "right" else 0))
        return out, aabb(x, y, w, h, u.rot), (ix, y, mw, h), n
    bx, by, bw, bh = u.box
    ucx, ucy = bx + bw / 2, by + bh / 2
    s = st.s; r = getattr(st, "r", 1.0)
    byid = {}
    for m in u.members:
        mcx = (m["x"] + m["width"] / 2 - ucx) * s + st.cx
        mcy = (m["y"] + m["height"] / 2 - ucy) * s + st.cy
        w, h = m["width"] * s, m["height"] * s
        e = dict(m); e.update(x=mcx - w / 2, y=mcy - h / 2, width=w, height=h)
        out.append(e); byid[e["id"]] = e
    for e in out:
        if e["type"] != "text":
            continue
        tm = u.tm[e["id"]]
        fs = max(1, int(math.floor(float(e["font_size"]) * s * r)))
        host = u.host.get(e["id"])
        if host is not None:
            hs = byid[host[0]]
            # shape may grow (shapes are not proportion-locked) so the label keeps >= 50% of its scaled padding
            n_src = u.n_src[e["id"]]
            while fs > 1:
                n, mw = wrap_measure(tm, fs, 10 * W)  # natural width (no wrap) for a label
                th = text_block_h(tm, e, fs, n)
                need_w = mw + 0.5 * host[1] * s; need_h = th + 0.5 * host[2] * s
                if need_w <= W and need_h <= H:
                    break
                fs -= 1
            n, mw = wrap_measure(tm, fs, 10 * W); th = text_block_h(tm, e, fs, n)
            hcx, hcy = hs["x"] + hs["width"] / 2, hs["y"] + hs["height"] / 2
            hs["width"] = max(hs["width"], mw + 0.5 * host[1] * s)
            hs["height"] = max(hs["height"], th + 0.5 * host[2] * s)
            hs["x"] = hcx - hs["width"] / 2; hs["y"] = hcy - hs["height"] / 2
            e["width"] = max(e["width"], hs["width"]); e["x"] = hcx - e["width"] / 2
            e["y"] = hcy - th / 2; e["height"] = max(th, e["height"])
        else:
            while fs > 1:
                n, mw = wrap_measure(tm, fs, e["width"])
                if n <= u.n_src[e["id"]] and mw <= e["width"] + 0.01:
                    break
                fs -= 1
            n, _ = wrap_measure(tm, fs, e["width"])
            e["height"] = max(e["height"], text_block_h(tm, e, fs, n))
        e["font_size"] = fs
    boxes = [aabb(e["x"], e["y"], e["width"], e["height"], float(e.get("rotation") or 0)) for e in out]
    box = union(boxes)
    return out, box, box, 1

def project(u, st, W, H):
    """Hard constraints: int fs >= 1, text width >= longest word (renderer never breaks words),
    unit fits the canvas, rotated AABB inside the canvas."""
    if u.is_text:
        m = u.members[0]; tm = u.tm[m["id"]]
        st.fs = max(1, int(round(st.fs)))
        st.w = min(max(st.w, 1.0), W - 4.0)
        while st.fs > 1 and tm["lw"] * st.fs > st.w:
            st.fs -= 1
    else:
        st.s = max(st.s, 1e-3); st.r = min(max(getattr(st, "r", 1.0), 1.0), 3.0)
        for _ in range(40):
            _, box, _, _ = place_unit(u, st, W, H)
            if box[2] <= W and box[3] <= H:
                break
            if st.r > 1.0:
                st.r = max(1.0, st.r * 0.9)
            else:
                st.s *= 0.9
    _, box, _, _ = place_unit(u, st, W, H)
    x, y, w, h = box
    dx = 0.0; dy = 0.0
    if x < 0: dx = -x
    if x + w > W: dx = W - (x + w)
    if y < 0: dy = -y
    if y + h > H: dy = H - (y + h)
    st.cx += dx; st.cy += dy

# ----------------------------------------------------------------- objective
WEIGHTS = dict(overlap=60.0, margin=10.0, pos=1.5, order=25.0, size=6.0, hier=8.0, sib=10.0,
               align=2.0, util=4.0, read=20.0, corner=10.0, lines=3.0, row=5.0)

def unit_fs(u, st):
    if u.is_text:
        return st.fs
    return max(1, int(u.fs_src * st.s * getattr(st, "r", 1.0))) if u.fs_src > 0 else 0

def objective(units, content, rows, src_over, states, W, H, SW, SH, breakdown=False):
    D = math.sqrt(W * H)
    m = max(4.0, 0.03 * min(W, H))
    wide = (W / H) > (SW / SH) * 1.25
    s_ref = math.sqrt((W * H) / (SW * SH))
    placed = {u.uid: place_unit(u, states[u.uid], W, H) for u in content}
    E = dict((k, 0.0) for k in WEIGHTS)
    for i, a in enumerate(content):
        ia = placed[a.uid][2]
        for b in content[i + 1:]:
            if (a.uid, b.uid) in src_over:
                continue
            ib = placed[b.uid][2]
            ov = inter(ia, ib)
            if ov > 0:
                mult = 3.0 if (a.is_text and b.is_text) else 1.0
                E["overlap"] += mult * ov / max(1.0, min(ia[2] * ia[3], ib[2] * ib[3]))
    xs0 = []; xs1 = []; ys0 = []; ys1 = []
    for u in content:
        st = states[u.uid]; _, box, ink, n = placed[u.uid]
        x, y, w, h = box
        xs0.append(x); xs1.append(x + w); ys0.append(y); ys1.append(y + h)
        if u.anchor is None:
            viol = max(0, m - x) + max(0, x + w - (W - m)) + max(0, m - y) + max(0, y + h - (H - m))
            E["margin"] += (viol / D) ** 2 * 100
            E["pos"] += u.prom * (((x + w / 2) / W - u.src_cx) ** 2 + ((y + h / 2) / H - u.src_cy) ** 2)
        else:
            gx = x if u.anchor[0] == "left" else W - (x + w)
            gy = y if u.anchor[1] == "top" else H - (y + h)
            E["corner"] += ((max(0, gx - m) / W) ** 2 + (max(0, gy - m) / H) ** 2) * 100
        s = (st.fs / u.fs_src) if u.is_text else st.s
        E["size"] += u.prom * (max(0, math.log(s_ref / s)) ** 2 + 0.6 * max(0, math.log(s / (1.25 * s_ref))) ** 2)
        if u.fs_src > 0:
            fs = unit_fs(u, st)
            fs_min = min(16.0, max(7.0, 0.012 * min(W, H))) * (0.6 + 0.4 * u.prom)
            E["read"] += u.prom * max(0, fs_min - fs) / fs_min
        if u.is_text:
            E["lines"] += u.prom * (n - u.n_src[u.members[0]["id"]]) ** 2
    if content:
        span_x = (max(xs1) - min(xs0)) / max(1.0, W - 2 * m); span_y = (max(ys1) - min(ys0)) / max(1.0, H - 2 * m)
        E["util"] += max(0, 1 - span_x) ** 2 + max(0, 1 - span_y) ** 2
    tx = [u for u in content if u.fs_src > 0]
    for i, a in enumerate(tx):
        for b in tx[i + 1:]:
            big, small = (a, b) if a.fs_src >= b.fs_src else (b, a)
            if big.fs_src == small.fs_src:
                continue
            fb = unit_fs(big, states[big.uid]); fsm = unit_fs(small, states[small.uid])
            r_src = big.fs_src / small.fs_src; r_out = fb / max(1, fsm)
            E["hier"] += max(0, math.log(r_src / r_out)) ** 2
    sibs = {}
    for u in content:
        if u.sib is not None:
            sibs.setdefault(u.sib, []).append(math.log(max(1, unit_fs(u, states[u.uid]))) + math.log(states[u.uid].s))
    for ss in sibs.values():
        mu = sum(ss) / len(ss)
        E["sib"] += sum((v - mu) ** 2 for v in ss)
    sibc = {}
    for u in content:
        if u.sib is not None:
            b = placed[u.uid][1]; sibc.setdefault(u.sib, []).append(b[1] + b[3] / 2)
    for cys in sibc.values():
        mu = sum(cys) / len(cys)
        E["row"] += sum(((c - mu) / H) ** 2 for c in cys) * 4
    for i, a in enumerate(content):
        if a.anchor: continue
        for b in content[i + 1:]:
            if b.anchor: continue
            if a.row == b.row:
                first, second = (a, b) if a.col < b.col else (b, a)
                f = placed[first.uid][1]; sdd = placed[second.uid][1]
                E["order"] += (max(0, (f[0] + f[2] / 2) - (sdd[0] + sdd[2] / 2)) / W) ** 2
                continue
            first, second = (a, b) if a.row < b.row else (b, a)
            f = placed[first.uid][1]; sdd = placed[second.uid][1]
            fcx, fcy = f[0] + f[2] / 2, f[1] + f[3] / 2; scx, scy = sdd[0] + sdd[2] / 2, sdd[1] + sdd[3] / 2
            if wide:
                E["order"] += (max(0, fcx - scx - 0.5 * min(f[2], sdd[2])) / W) ** 2
                xov = min(f[0] + f[2], sdd[0] + sdd[2]) - max(f[0], sdd[0])
                if xov > 0.3 * min(f[2], sdd[2]):
                    E["order"] += (max(0, fcy - scy) / H) ** 2
            else:
                E["order"] += (max(0, fcy - scy) / H) ** 2
    for i, a in enumerate(content):
        ba = placed[a.uid][1]; acx = ba[0] + ba[2] / 2
        if abs(a.src_cx - 0.5) < 0.02 and not wide:
            E["align"] += ((acx - W / 2) / W) ** 2 * 4
        for b in content[i + 1:]:
            if abs(a.src_cx - b.src_cx) < 0.02:
                bb = placed[b.uid][1]; bcx = bb[0] + bb[2] / 2
                xov = min(ba[0] + ba[2], bb[0] + bb[2]) - max(ba[0], bb[0])
                if xov > 0:
                    E["align"] += ((acx - bcx) / W) ** 2 * 4
    total = sum(WEIGHTS[k] * E[k] for k in E)
    if breakdown:
        return total, {k: round(WEIGHTS[k] * E[k], 3) for k in E}
    return total

# ----------------------------------------------------------------- initializations
def _text_state(u, st, s_w, s_f):
    mm = u.members[0]
    st.fs = max(1, int(round(float(mm["font_size"]) * s_f))); st.w = float(mm["width"]) * s_w

def init_anchor(content, W, H, SW, SH):
    s = min(W / SW, H / SH)
    states = {}
    for u in content:
        st = State(); st.cx = u.src_cx * W; st.cy = u.src_cy * H; st.s = s; st.r = 1.0
        if u.is_text:
            _text_state(u, st, W / SW, s)
        states[u.uid] = st
    return states

def init_lanes(content, rows, W, H, SW, SH, lanes, wide):
    """lanes: list of lists of rows (contiguous). Wide: lanes are columns, rows stack inside.
    Tall/same: lanes are bands top-to-bottom. Common scale so everything fits."""
    m = max(4.0, 0.03 * min(W, H)); gap = max(4.0, 0.025 * min(W, H))
    anch = [u for u in content if u.anchor]
    def row_dims(r):
        us = [u for u in r if not u.anchor]
        if not us: return 0.0, 0.0, us
        return sum(u.box[2] for u in us) + 0 * gap, max(u.box[3] for u in us), us
    lane_rows = [[row_dims(r) for r in L] for L in lanes]
    lane_rows = [[d for d in L if d[2]] for L in lane_rows]
    lane_rows = [L for L in lane_rows if L]
    if wide:
        along = H - 2 * m; across = W - 2 * m
        dem_along = [sum(d[1] for d in L) for L in lane_rows]; dem_across = [max(d[0] for d in L) for L in lane_rows]
        gaps_along = [gap * (len(L) - 1) for L in lane_rows]
        s = min(min((along - g) / max(1e-6, d) for d, g in zip(dem_along, gaps_along)),
                (across - gap * (len(lane_rows) - 1) - gap * max(sum(len(d[2]) - 1 for d in L) for L in lane_rows)) / max(1e-6, sum(dem_across)))
    else:
        along = W - 2 * m; across = H - 2 * m
        dem_along = [max(d[0] for d in L) for L in lane_rows]; dem_across = [sum(d[1] for d in L) for L in lane_rows]
        gaps_across = [gap * (len(L) - 1) for L in lane_rows]
        s = min(min(along / max(1e-6, d) for d in dem_along),
                (across - gap * (len(lane_rows) - 1) - sum(gaps_across)) / max(1e-6, sum(dem_across)))
    s = max(1e-3, min(s, 1.25 * math.sqrt((W * H) / (SW * SH))))
    states = {}
    if wide:
        used = sum(d * s for d in dem_across) + gap * (len(lane_rows) - 1)
        spread = (across - used) / (len(lane_rows) + 1)
        x = m + spread
        for L, dc in zip(lane_rows, dem_across):
            tot_h = sum(d[1] * s for d in L) + gap * (len(L) - 1)
            y = m + (along - tot_h) / 2
            for rw, rh, us in L:
                rx = x + (dc * s - (rw * s + gap * (len(us) - 1))) / 2
                for u in us:
                    st = State(); st.s = s; st.r = 1.0
                    st.cx = rx + u.box[2] * s / 2; st.cy = y + rh * s / 2
                    if u.is_text: _text_state(u, st, s, s)
                    states[u.uid] = st
                    rx += u.box[2] * s + gap
                y += rh * s + gap
            x += dc * s + spread + gap
    else:
        used = sum(d * s for d in dem_across) + sum(gaps_across) + gap * (len(lane_rows) - 1)
        spread = (across - used) / (len(lane_rows) + 1)
        y = m + spread
        for L in lane_rows:
            for rw, rh, us in L:
                rx = m + (along - (rw * s + gap * (len(us) - 1))) / 2
                for u in us:
                    st = State(); st.s = s; st.r = 1.0
                    st.cx = rx + u.box[2] * s / 2; st.cy = y + rh * s / 2
                    if u.is_text: _text_state(u, st, s, s)
                    states[u.uid] = st
                    rx += u.box[2] * s + gap
                y += rh * s + gap
            y += spread
    for u in anch:
        st = State(); st.s = s; st.r = 1.0
        bx, by, bw, bh = u.box
        st.cx = (m + bw * s / 2) if u.anchor[0] == "left" else (W - m - bw * s / 2)
        st.cy = (m + bh * s / 2) if u.anchor[1] == "top" else (H - m - bh * s / 2)
        if u.is_text: _text_state(u, st, s, s)
        states[u.uid] = st
    return states

def partitions(n, k):
    for cuts in itertools.combinations(range(1, n), k - 1):
        idx = [0, *cuts, n]
        yield [list(range(idx[i], idx[i + 1])) for i in range(k)]

# ----------------------------------------------------------------- local search
def search(units, content, rows, src_over, states, W, H, SW, SH, iters, seed):
    rng = random.Random(seed)
    D = math.sqrt(W * H)
    for u in content:
        project(u, states[u.uid], W, H)
    cur = objective(units, content, rows, src_over, states, W, H, SW, SH)
    best = cur; best_states = copy.deepcopy(states)
    T0 = 0.05 * max(cur, 1.0)
    for it in range(iters):
        frac = it / iters
        step = D * (0.08 * (1 - frac) + 0.004)
        T = T0 * max(0.0, 1 - frac / 0.6)
        u = content[rng.randrange(len(content))]
        st = states[u.uid]; old = copy.copy(st)
        r = rng.random()
        if u.sib is not None and r < 0.15:
            # compound sibling move: translate-all / scale-all / re-pack into one row
            sibs_u = [v for v in content if v.sib == u.sib]
            olds = {v.uid: copy.copy(states[v.uid]) for v in sibs_u}
            kind = rng.random()
            if kind < 0.35:
                ddx = rng.choice((-1, 1)) * step * rng.random() * (1 if rng.random() < 0.5 else 0)
                ddy = rng.choice((-1, 1)) * step * rng.random() * (0 if ddx else 1)
                for v in sibs_u: states[v.uid].cx += ddx; states[v.uid].cy += ddy
            elif kind < 0.7:
                f = rng.choice((1.1, 0.92, 1.04)); fr = rng.choice((1.0, 1.08, 0.93))
                for v in sibs_u: states[v.uid].s *= f; states[v.uid].r = min(3.0, max(1.0, states[v.uid].r * fr))
            else:
                gap = max(4.0, 0.025 * min(W, H))
                sibs_u.sort(key=lambda v: v.col)
                ws = [place_unit(v, states[v.uid], W, H)[1][2] for v in sibs_u]
                mcx = sum(states[v.uid].cx for v in sibs_u) / len(sibs_u); mcy = sum(states[v.uid].cy for v in sibs_u) / len(sibs_u)
                tot = sum(ws) + gap * (len(ws) - 1); x = mcx - tot / 2
                for v, wv in zip(sibs_u, ws):
                    states[v.uid].cx = x + wv / 2; states[v.uid].cy = mcy; x += wv + gap
            for v in sibs_u: project(v, states[v.uid], W, H)
            new = objective(units, content, rows, src_over, states, W, H, SW, SH)
            d = new - cur
            if d <= 0 or (T > 0 and rng.random() < math.exp(-d / T)):
                cur = new
                if cur < best: best = cur; best_states = copy.deepcopy(states)
            else:
                for v in sibs_u: states[v.uid] = olds[v.uid]
            continue
        if r < 0.38:
            if rng.random() < 0.5: st.cx += rng.choice((-1, 1)) * step * rng.random()
            else: st.cy += rng.choice((-1, 1)) * step * rng.random()
        elif r < 0.58:
            f = rng.choice((1.12, 0.9, 1.04, 0.96))
            if u.is_text: st.fs = max(1, int(round(st.fs * f))) if abs(st.fs * f - st.fs) >= 1 else st.fs + (1 if f > 1 else -1)
            else: st.s *= f
        elif r < 0.70 and u.is_text:
            st.w *= rng.choice((1.15, 0.87, 1.05, 0.95))
        elif r < 0.78 and u.is_text:
            st.fs += rng.choice((-1, 1))
        elif r < 0.88 and not u.is_text and not u.is_image and u.fs_src > 0:
            st.r = min(3.0, max(1.0, st.r * rng.choice((1.15, 0.87, 1.05))))
        else:
            v = content[rng.randrange(len(content))]
            if rng.random() < 0.5: st.cx = states[v.uid].cx if v is not u else W / 2
            else: st.cy = states[v.uid].cy if v is not u else H / 2
        project(u, st, W, H)
        new = objective(units, content, rows, src_over, states, W, H, SW, SH)
        d = new - cur
        if d <= 0 or (T > 0 and rng.random() < math.exp(-d / T)):
            cur = new
            if cur < best:
                best = cur; best_states = copy.deepcopy(states)
        else:
            states[u.uid] = old
    # greedy grow pass: deterministic, prominence order
    states = best_states; cur = best
    for u in sorted(content, key=lambda u: -u.prom):
        for _ in range(30):
            st = states[u.uid]; old = copy.copy(st)
            if u.is_text: st.fs += 1
            elif u.fs_src > 0 and st.r < 3.0 and u.is_image is False and rng.random() < 0.5: st.r *= 1.05
            else: st.s *= 1.04
            project(u, st, W, H)
            new = objective(units, content, rows, src_over, states, W, H, SW, SH)
            if new < cur: cur = new
            else:
                states[u.uid] = old; break
    return cur, states

def resize_opt(source, target, iters=2000, seed=1234, log=print):
    SW, SH = source["canvas"]["width"], source["canvas"]["height"]
    W, H = target["width"], target["height"]
    units, content, rows, src_over = build_units(source)
    wide = (W / H) > (SW / SH) * 1.25; tall = (W / H) < (SW / SH) / 1.25
    starts = [("anchor", init_anchor(content, W, H, SW, SH))]
    nrows = len(rows)
    # template enumeration: contiguous partitions of reading rows into k lanes, ranked by initial objective
    cands = []
    ks = [2, 3, 4] if wide else [1]
    for k in ks:
        if k > nrows: continue
        for part in partitions(nrows, k):
            lanes = [[rows[i] for i in idxs] for idxs in part]
            st = init_lanes(content, rows, W, H, SW, SH, lanes, wide)
            for u in content: project(u, st[u.uid], W, H)
            o = objective(units, content, rows, src_over, st, W, H, SW, SH)
            cands.append((o, f"lanes{k}:{part}", st))
    cands.sort(key=lambda c: c[0])
    starts += [(n, s) for _, n, s in cands[:4]]
    results = []
    for name, st in starts:
        t0 = time.time()
        best, bst = search(units, content, rows, src_over, st, W, H, SW, SH, iters, seed)
        tot, br = objective(units, content, rows, src_over, bst, W, H, SW, SH, breakdown=True)
        log(f"  start={name:28s} obj={best:9.4f} {time.time()-t0:5.2f}s  {br}")
        results.append((best, name, bst))
    results.sort(key=lambda r: r[0])
    best, name, states = results[0]
    log(f"  chosen={name} obj={best:.4f}  ({len(cands)} lane templates enumerated)")
    s_min = min(W / SW, H / SH)
    out = {}
    for u in units:
        if u.kind == "bg":
            for m in u.members:
                e = dict(m); e.update(x=0, y=0, width=W, height=H); out[m["id"]] = e
        elif u.kind == "deco":
            for m in u.members:
                w, h = m["width"] * s_min, m["height"] * s_min
                cx, cy = (m["x"] + m["width"] / 2) / SW * W, (m["y"] + m["height"] / 2) / SH * H
                x = min(max(cx - w / 2, 0), W - w); y = min(max(cy - h / 2, 0), H - h)
                e = dict(m); e.update(x=x, y=y, width=w, height=h); out[m["id"]] = e
        else:
            els, box, ink, n = place_unit(u, states[u.uid], W, H)
            for e in els:
                out[e["id"]] = e
    elements = [out[e["id"]] for e in source["elements"]]
    for e in elements:
        for k in ("x", "y", "width", "height"):
            e[k] = round(float(e[k]), 2)
        if e["type"] == "text":
            e["font_size"] = int(max(1, e["font_size"]))
    return {"canvas": {"width": W, "height": H}, "elements": elements}, (units, content, rows)

if __name__ == "__main__":
    path, W, H, prefix = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    iters = int(sys.argv[5]) if len(sys.argv) > 5 else 2000
    task = json.load(open(path))
    src = task["source"]
    t0 = time.time()
    out, (units, content, rows) = resize_opt(src, {"width": W, "height": H}, iters=iters)
    dt = time.time() - t0
    print("units: " + ", ".join(f"{u.uid}:{u.kind}{'/anchor' + str(u.anchor) if u.anchor else ''}" for u in units))
    print("rows: " + " | ".join(",".join(u.uid for u in r) for r in rows))
    probs = self_check(src, out)
    print(f"resize time {dt:.2f}s; self_check: {probs or 'OK'}")
    json.dump(out, open(prefix + ".json", "w"), indent=1)
    warns = render_design_to_png(out, f"{REPO}/brand_kit/assets", prefix + ".png")
    print("rendered", prefix + ".png", warns)
    for e in out["elements"]:
        if e["type"] == "text":
            print(f"   {e['id']:18s} fs={e['font_size']:3d} box=({e['x']:.0f},{e['y']:.0f},{e['width']:.0f},{e['height']:.0f})")
