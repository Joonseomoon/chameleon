"""PROTOTYPE (proposer:grammar) - role inference + aspect-class layout grammars.
NOT production code. Lives on the `prototypes` branch; imports the repo's renderer for
text measurement only. Never uses ids / asset names / content strings for decisions.
"""
import math, os, sys, json
from functools import lru_cache

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root; this file lives in <repo>/prototypes/
if REPO not in sys.path:
    sys.path.insert(0, REPO)
FONTS_DIR = os.path.join(REPO, "brand_kit", "fonts")

import skia  # type: ignore
from renderer.converter.brand_fonts import load_brand_font_manager
from renderer.converter.wrap import char_widths, wrap_text
from cr_renderer.fonts import FONT_WEIGHTS

_FM = load_brand_font_manager(FONTS_DIR)
BOLD = {"bold", "semibold", "extrabold", "black"}

# ============================================================================ text measurement
def _font_key(el):
    w = str(el.get("font_weight") or "regular").lower()
    w = w if w in FONT_WEIGHTS else "regular"
    b = w in BOLD
    i = (el.get("font_style") == "italic")
    st = "bolditalic" if b and i else "bold" if b else "italic" if i else "regular"
    return str(el.get("font_family", "")), w, st


@lru_cache(maxsize=None)
def _metrics(fam, w, st, fs_int):
    f = skia.Font(skia.Typeface.MakeFromData(_FM.lookup(fam, w, st)), fs_int)
    m = f.getMetrics()
    return (-m.fAscent, m.fDescent, m.fCapHeight)


@lru_cache(maxsize=None)
def _widths(fam, w, st, fs, content):
    return tuple(char_widths(_FM, fam, w, st, float(fs), content))


def measure(el, fs, box_w):
    """Reproduce the renderer: wrap with converter's wrap_text at fs (integer => wrap == draw)."""
    fam, w, st = _font_key(el)
    content = str(el.get("content", ""))
    fs_i = max(1, int(fs))
    widths = _widths(fam, w, st, fs_i, content)
    wrapped = wrap_text(content, list(widths), box_w)
    ls = float(el.get("letter_spacing") or 0)
    lines = wrapped.split("\n")
    lw, idx = [], 0
    for ln in lines:
        lw.append(sum(widths[idx:idx + len(ln)]) + ls * max(0, len(ln) - 1))
        idx += len(ln) + 1
    asc, desc, cap = _metrics(fam, w, st, fs_i)
    lh = float(el.get("line_height") or 1.2)
    n = len(lines)
    h = (n - 1) * fs_i * lh + asc + desc
    longest, idx = 0.0, 0
    for wd in content.split(" "):
        longest = max(longest, sum(widths[idx:idx + len(wd)]) + ls * max(0, len(wd) - 1))
        idx += len(wd) + 1
    return dict(n=n, w=(max(lw) if lw else 0.0), h=h, longest=longest, asc=asc, desc=desc, cap=cap, lh=lh)


def fits(el, fs, box_w, box_h, max_lines=None):
    m = measure(el, fs, box_w)
    ml = max_lines or el.get("max_lines")
    return (m["w"] <= box_w + 0.5 and m["longest"] <= box_w + 0.5 and m["h"] <= box_h + 0.5 and (not ml or m["n"] <= ml)), m


def fit_text(el, box_w, box_h, fs_max, fs_min=1, max_lines=None):
    """Largest integer fs in [fs_min, fs_max] whose wrapped text fits. Falls back to fs_min."""
    fs_max, fs_min = max(int(fs_max), 1), max(int(fs_min), 1)
    fs_max = max(fs_max, fs_min)
    lo, hi, best = fs_min, fs_max, None
    while lo <= hi:
        mid = (lo + hi) // 2
        ok, m = fits(el, mid, box_w, box_h, max_lines)
        if ok:
            best = (mid, m); lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        return fs_min, measure(el, fs_min, box_w)
    fs, m = best
    while fs > fs_min and not fits(el, fs, box_w, box_h, max_lines)[0]:
        fs -= 1; m = measure(el, fs, box_w)
    return fs, m


def text_min_width(el, h, fs):
    """Smallest width in which `el` fits within height h at font fs (used for allocation minimums)."""
    m1 = measure(el, fs, 1e9)
    lines_max = max(1, int((h - m1["asc"] - m1["desc"]) // (max(1, int(fs)) * m1["lh"])) + 1)
    w = max(m1["longest"], m1["w"] / lines_max * 1.12)
    for _ in range(12):
        ok, m = fits(el, fs, w, h)
        if ok:
            return w
        w *= 1.12
    return w


# ============================================================================ geometry
def aabb(e):
    x, y, w, h = float(e["x"]), float(e["y"]), float(e["width"]), float(e["height"])
    r = float(e.get("rotation") or 0)
    if not r:
        return (x, y, w, h)
    a = math.radians(r)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    bw, bh = w * c + h * s, w * s + h * c
    return (x + w / 2 - bw / 2, y + h / 2 - bh / 2, bw, bh)


def union(bs):
    x0 = min(b[0] for b in bs); y0 = min(b[1] for b in bs)
    x1 = max(b[0] + b[2] for b in bs); y1 = max(b[1] + b[3] for b in bs)
    return (x0, y0, x1 - x0, y1 - y0)


def inter(a, b):
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def contains(outer, inner, tol=0.5):
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol and
            inner[0] + inner[2] <= outer[0] + outer[2] + tol and inner[1] + inner[3] <= outer[1] + outer[3] + tol)


def rgba_alpha(s):
    try:
        s = str(s).strip()
        if s.startswith("rgba"):
            return float(s[5:-1].split(",")[3])
    except Exception:
        pass
    return 1.0


def rgb_tuple(s):
    try:
        return tuple(int(float(v)) for v in str(s).strip()[5:-1].split(",")[:3])
    except Exception:
        return (0, 0, 0)


# ============================================================================ units & roles
class Unit:
    def __init__(self, uid, members):
        self.uid, self.members = uid, members
        self.box = union([aabb(e) for e in members])
        self.types = {e["type"] for e in members}
        self.rot = max((abs(float(e.get("rotation") or 0)) for e in members), default=0)
        self.z = min(int(e.get("z_index", 0)) for e in members)
        self.role, self.conf = "other", 0.0
        self.texts = [e for e in members if e["type"] == "text"]
        self.shapes = [e for e in members if e["type"] == "shape"]
        self.images = [e for e in members if e["type"] == "image"]

    cx = property(lambda s: s.box[0] + s.box[2] / 2)
    cy = property(lambda s: s.box[1] + s.box[3] / 2)
    area = property(lambda s: s.box[2] * s.box[3])


def build_units(els):
    groups, order = {}, []
    for e in els:
        g = e.get("group_id")
        key = ("g", g) if g else ("e", e["id"])
        if key not in groups:
            groups[key] = []; order.append(key)
        groups[key].append(e)
    return [Unit(k, groups[k]) for k in order]


def infer_roles(src):
    W, H = float(src["canvas"]["width"]), float(src["canvas"]["height"])
    CA = W * H
    units = build_units(src["elements"])
    # implicit containment: ungrouped shape wholly containing an ungrouped text drawn above it -> button unit
    merged = True
    while merged:
        merged = False
        for s in units:
            if s.types == {"shape"} and len(s.members) == 1 and s.area < 0.3 * CA:
                for t in units:
                    if t is not s and t.types == {"text"} and len(t.members) == 1 and t.z > s.z and contains(s.box, t.box, 2):
                        s.__init__(s.uid, s.members + t.members); units.remove(t); merged = True; break
            if merged:
                break
    for u in units:
        u.cover = inter(u.box, (0, 0, W, H)) / CA
        u.eff_alpha = min((float(e.get("opacity", 1)) * rgba_alpha(e.get("fill_color")) for e in u.shapes), default=1.0)
        u.anchored = (u.area < 0.03 * CA and (u.box[0] <= 0.08 * W or u.box[0] + u.box[2] >= 0.92 * W)
                      and (u.box[1] <= 0.08 * H or u.box[1] + u.box[3] >= 0.92 * H))
    # 1 background: single image/shape covering >=85% with nothing non-background below it
    for u in units:
        if not u.texts and len(u.members) == 1 and u.cover >= 0.85:
            if not [v for v in units if v is not u and v.z < u.z and v.cover < 0.85]:
                u.role, u.conf = "background", 1.0
    # 2 decoration: shape-only, translucent / bleeding / layered under content
    for u in units:
        if u.role == "other" and u.types == {"shape"}:
            beyond = not contains((0, 0, W, H), u.box, 0.5)
            below = any(v is not u and v.role == "other" and v.z > u.z and v.types != {"shape"} and inter(u.box, v.box) > 0 for v in units)
            if u.eff_alpha <= 0.5 or beyond or below:
                u.role, u.conf = "decoration", (0.9 if u.eff_alpha <= 0.5 else 0.6)
    # 3 ribbon: rotated unit carrying text
    for u in units:
        if u.role == "other" and u.rot >= 2 and u.texts:
            u.role, u.conf = "ribbon", 0.9
    # 4 buttons: shape(s) + one text inside -> cta (largest, lower half) else badge
    buttons = [u for u in units if u.role == "other" and u.shapes and len(u.texts) == 1
               and contains(union([aabb(s) for s in u.shapes]), aabb(u.texts[0]), 3)]
    if buttons:
        best = max(buttons, key=lambda u: u.area / CA + (0.5 if u.cy > 0.5 * H else 0.0))
        if best.cy > 0.45 * H:
            best.role, best.conf = "cta", 0.8
        for u in buttons:
            if u.role == "other":
                u.role, u.conf = "badge", 0.8
    # 5 images: logo (wide, small, top) / watermark (tiny, bottom corner) / hero (largest) / image
    imgs = sorted([u for u in units if u.role == "other" and u.types == {"image"} and len(u.members) == 1], key=lambda u: -u.area)
    for i, u in enumerate(imgs):
        asp = u.box[2] / max(1e-6, u.box[3]); af = u.area / CA
        if asp >= 1.8 and af <= 0.08 and u.cy < 0.3 * H:
            u.role, u.conf = "logo", 0.9
        elif af <= 0.03 and u.cy > 0.75 * H and (u.cx < 0.25 * W or u.cx > 0.75 * W):
            u.role, u.conf = "watermark", 0.7
        elif i == 0 and af >= 0.04:
            u.role, u.conf = "hero", 0.8
        else:
            u.role, u.conf = "image", 0.5
    # 6 free texts: headline (largest) / legal / cta(text) / subhead / body / caption
    texts = [u for u in units if u.role == "other" and u.types == {"text"} and len(u.members) == 1]
    texts.sort(key=lambda u: (-float(u.texts[0].get("font_size", 1)), 0 if str(u.texts[0].get("font_weight", "")).lower() in BOLD else 1, -u.area))
    if texts:
        head = texts[0]; head.role = "headline"
        hfs = float(head.texts[0]["font_size"])
        head.conf = 1.0 if len(texts) == 1 or float(texts[1].texts[0]["font_size"]) <= 0.8 * hfs else 0.6
        head_color = rgb_tuple(head.texts[0].get("color"))
        sub = False
        for u in texts[1:]:
            e = u.texts[0]; fs = float(e.get("font_size", 1)); n = len(str(e.get("content", "")))
            bold = str(e.get("font_weight", "")).lower() in BOLD
            alpha = rgba_alpha(e.get("color"))
            if n >= 40 and ((fs <= 0.45 * hfs and u.cy >= 0.9 * H) or (fs <= 0.35 * hfs and alpha <= 0.75)):
                u.role, u.conf = "legal", 0.9
            elif bold and n <= 32 and rgb_tuple(e.get("color")) != head_color and u.cy > head.cy and not any(v.role == "cta" for v in units):
                u.role, u.conf = "cta", 0.7
            elif not sub and fs >= 0.3 * hfs:
                u.role, u.conf = "subhead", 0.8; sub = True
            elif n > 60:
                u.role, u.conf = "body", 0.7
            else:
                u.role, u.conf = "caption", 0.5
    return units, W, H


# ============================================================================ allocation
def allocate(items, length, gap, cross, axis):
    """Distribute `length` along axis among items. Each item: pref(aw,ah)->(w,h), weight, optional minw(h)/minh(w).
    Returns sizes, or None when the minimums do not fit."""
    n = len(items)
    if n == 0:
        return []
    avail = max(0.0, length - gap * (n - 1))
    mins = []
    for it in items:
        f = it.get("minw" if axis == "x" else "minh")
        mins.append(float(f(cross)) if f else 0.0)
    if sum(mins) > avail + 0.5:
        return None
    fixed, sizes = {}, list(mins)
    for _ in range(4):
        free = [i for i in range(n) if i not in fixed]
        wsum = sum(items[i]["weight"] for i in free)
        rem = avail - sum(fixed.values()) - sum(mins[i] for i in free)
        changed = False
        for i in range(n):
            if i in fixed:
                sizes[i] = fixed[i]; continue
            share = mins[i] + (rem * items[i]["weight"] / wsum if wsum > 0 else 0)
            aw, ah = (cross, share) if axis == "y" else (share, cross)
            pw, ph = items[i]["pref"](aw, ah)
            need = max(mins[i], ph if axis == "y" else pw)
            if need < share - 0.5:
                fixed[i] = need; changed = True
            sizes[i] = min(share, need) if need > 0 else share
        if not changed:
            break
    return sizes


def place_sequence(items, origin, length, gap, cross, axis):
    sizes = allocate(items, length, gap, cross, axis)
    if sizes is None:
        return None
    used = sum(sizes) + gap * (len(items) - 1)
    extra = max(0.0, length - used) / (len(items) + 1)
    pos, out = origin + extra, {}
    for it, sz in zip(items, sizes):
        out[it["name"]] = (pos, sz)
        pos += sz + gap + extra
    return out


# ============================================================================ engine
class LayoutFail(Exception):
    pass


class Engine:
    BASE_FLOOR = {"normal": dict(headline=14, subhead=11, body=11, caption=10, label=10, legal=8),
                  "small": dict(headline=12, subhead=9, body=9, caption=8, label=8, legal=7),
                  "micro": dict(headline=8, subhead=7, body=7, caption=6, label=7, legal=5)}

    def __init__(self, src, target):
        self.src = src
        self.units, self.W, self.H = infer_roles(src)
        self.TW, self.TH = float(target["width"]), float(target["height"])
        r = self.TW / self.TH
        self.cls = ("ultra_tall" if r < 0.45 else "tall" if r < 0.85 else "square" if r <= 1.2 else "wide" if r <= 3 else "ultra_wide")
        self.short = min(self.TW, self.TH)
        self.size_cls = "micro" if self.short <= 120 else "small" if self.short <= 360 else "normal"
        self.s_area = math.sqrt((self.TW * self.TH) / (self.W * self.H))
        self.s_min = min(self.TW / self.W, self.TH / self.H)
        self.m = max(4.0, min(0.04 * self.short, 48))
        self.g = max(3.0, min(0.025 * self.short, 32))
        self.src_fs = {u.role: float(u.texts[0]["font_size"]) for u in self.units if u.texts and u.role in ("headline", "subhead", "body", "caption", "legal")}
        self.reset(1.0)

    def reset(self, floor_scale):
        self.out = {e["id"]: dict(e) for e in self.src["elements"]}
        self.log, self.head_fs = [], None
        self.floor = {k: max(1, int(round(v * floor_scale))) for k, v in self.BASE_FLOOR[self.size_cls].items()}

    def role(self, r):
        return [u for u in self.units if u.role == r]

    def one(self, r):
        us = self.role(r); return us[0] if us else None

    def set_box(self, e, x, y, w, h):
        o = self.out[e["id"]]
        o["x"], o["y"], o["width"], o["height"] = round(x, 2), round(y, 2), round(max(1.0, w), 2), round(max(1.0, h), 2)

    def floor_of(self, u):
        return self.floor.get(u.role, self.floor["label" if u.role in ("cta", "badge", "ribbon") else "caption"])

    # ---- font caps: relative size preserved via area scale; hierarchy ratio tied to the headline
    def text_cap(self, u):
        e = u.texts[0]; sfs = float(e.get("font_size", 12))
        cap = sfs * self.s_area * 1.15
        if u.role == "headline":
            cap = max(cap, sfs * self.s_min)
        elif self.head_fs and "headline" in self.src_fs:
            cap = min(cap, self.head_fs * (sfs / self.src_fs["headline"]) * 1.1)
        return max(1, int(round(cap)))

    # ---- items ------------------------------------------------------------
    def item(self, u, weight, **kw):
        if u.role in ("logo", "hero", "image", "watermark"):
            return self.item_image(u, weight, **kw)
        if u.role in ("cta", "badge") and u.shapes:
            return self.item_button(u, weight)
        if u.texts and len(u.members) == 1:
            return self.item_text(u, weight)
        return self.item_rigid(u, weight)

    def item_image(self, u, weight, hmax_frac=None, wmax_frac=None):
        asp = u.box[2] / max(1e-6, u.box[3])

        def pref(aw, ah):
            hm = ah if hmax_frac is None else min(ah, hmax_frac * self.TH)
            wm = aw if wmax_frac is None else min(aw, wmax_frac * self.TW)
            h = max(0.0, min(hm, wm / asp))
            return (h * asp, h)
        return dict(name=u.uid, unit=u, kind="image", weight=weight, pref=pref,
                    minw=lambda h: min(0.35 * h * asp, 0.08 * self.TW), minh=lambda w: min(0.35 * w / asp, 0.08 * self.TH))

    def item_text(self, u, weight):
        e = u.texts[0]

        def pref(aw, ah):
            fs, m = fit_text(e, aw, ah, self.text_cap(u), self.floor_of(u))
            return (min(aw, m["w"]), m["h"])
        return dict(name=u.uid, unit=u, kind="text", weight=weight, pref=pref,
                    minw=lambda h: text_min_width(e, h, self.floor_of(u)),
                    minh=lambda w: measure(e, self.floor_of(u), w)["h"])

    def button_geom(self, u, w, h):
        """pill of size (w,h): label fs, measure, widened pill width if label needs it."""
        shape, label = u.shapes[0], u.texts[0]
        s = w / float(shape["width"])
        padx = max(3.0, 0.6 * self.floor_of(u))
        fs, m = fit_text(label, w - 2 * padx, h, max(1, int(round(float(label.get("font_size", 12)) * s * 1.05))), self.floor_of(u), max_lines=1)
        return fs, m, (m["w"] + 2 * padx if m["w"] > w - 2 * padx else w)

    def item_button(self, u, weight):
        shape, label = u.shapes[0], u.texts[0]
        sw, sh = float(shape["width"]), float(shape["height"])

        def pref(aw, ah):
            s = min(aw / sw, ah / sh, self.s_area * 1.3)
            fs, m, w2 = self.button_geom(u, sw * s, sh * s)
            return (min(aw, max(sw * s, w2)), sh * s)

        def minw(h):
            fl = self.floor_of(u); m = measure(label, fl, 1e9)
            return m["w"] + 2 * max(3.0, 0.6 * fl)

        def minh(w):
            fl = self.floor_of(u); m = measure(label, fl, 1e9)
            return m["asc"] + m["desc"] + 2 * max(2.0, 0.25 * fl)
        return dict(name=u.uid, unit=u, kind="button", weight=weight, pref=pref, minw=minw, minh=minh)

    def item_rigid(self, u, weight):
        bw, bh = u.box[2], u.box[3]; asp = bw / bh
        s_floor = 0.0
        for t in u.texts:
            s_floor = max(s_floor, self.floor_of(u) / max(1.0, float(t.get("font_size", 12))))

        def pref(aw, ah):
            h = max(0.0, min(ah, aw / asp, bh * self.s_area * 1.3))
            return (h * asp, h)
        return dict(name=u.uid, unit=u, kind="rigid", weight=weight, pref=pref,
                    minw=lambda h: bw * s_floor, minh=lambda w: bh * s_floor)

    def item_group(self, name, subitems, axis, weight):
        """Pseudo-item for a nested row/column (badges, text stack)."""
        g = self.g

        def pref(aw, ah):
            if axis == "y":
                sizes = allocate(subitems, ah, g, aw, "y")
                if sizes is None:
                    return (aw, ah * 10)
                return (aw, sum(sizes) + g * (len(subitems) - 1))
            sizes = allocate(subitems, aw, g, ah, "x")
            if sizes is None:
                return (aw * 10, ah)
            return (sum(sizes) + g * (len(subitems) - 1), ah)

        def minw(h):
            if axis == "y":
                return max(it["minw"](h / len(subitems)) for it in subitems)
            return sum(it["minw"](h) for it in subitems) + g * (len(subitems) - 1)

        def minh(w):
            if axis == "x":
                return max(it["minh"]((w - g * (len(subitems) - 1)) / len(subitems)) for it in subitems)
            return sum(it["minh"](w) for it in subitems) + g * (len(subitems) - 1)
        return dict(name=name, kind="group", axis=axis, items=subitems, weight=weight, pref=pref, minw=minw, minh=minh)

    # ---- writers ----------------------------------------------------------
    def write(self, it, slot, align=("center", "center")):
        if it["kind"] == "group":
            if it["axis"] == "y":
                self.col(it["items"], slot, align[0])
            else:
                self.row(it["items"], slot, align[1])
            return
        u = it["unit"]
        {"image": self.put_image, "button": self.put_button, "text": self.put_text, "rigid": self.put_rigid}[it["kind"]](u, slot, align)

    def col(self, items, slot, halign="center"):
        x, y, w, h = slot
        boxes = place_sequence(items, y, h, self.g, w, "y")
        if boxes is None:
            raise LayoutFail("col")
        for it in items:
            by, bh = boxes[it["name"]]
            self.write(it, (x, by, w, bh), (halign, "center"))

    def row(self, items, slot, valign="center"):
        x, y, w, h = slot
        boxes = place_sequence(items, x, w, self.g, h, "x")
        if boxes is None:
            raise LayoutFail("row")
        for it in items:
            bx, bw = boxes[it["name"]]
            self.write(it, (bx, y, bw, h), ("center", valign))

    @staticmethod
    def _align(x, y, w, h, nw, nh, align):
        px = x + (w - nw) / 2 if align[0] == "center" else x if align[0] == "left" else x + w - nw
        py = y + (h - nh) / 2 if align[1] == "center" else y if align[1] == "top" else y + h - nh
        return px, py

    def put_image(self, u, slot, align=("center", "center")):
        x, y, w, h = slot
        asp = u.box[2] / max(1e-6, u.box[3])
        nh = min(h, w / asp); nw = nh * asp
        px, py = self._align(x, y, w, h, nw, nh, align)
        self.set_box(u.members[0], px, py, nw, nh)

    def put_text(self, u, slot, align=("center", "center")):
        e = u.texts[0]; x, y, w, h = slot
        fs, m = fit_text(e, w, h, self.text_cap(u), self.floor_of(u))
        if u.role == "headline":
            self.head_fs = fs
        ty = y + (h - m["h"]) / 2 if align[1] == "center" else y if align[1] == "top" else y + h - m["h"]
        self.set_box(e, x, ty, w, max(1.0, m["h"]))
        self.out[e["id"]]["font_size"] = fs
        if not fits(e, fs, w, h)[0]:
            raise LayoutFail(f"text {e['id']}")
        return fs

    def put_button(self, u, slot, align=("center", "center")):
        shape, label = u.shapes[0], u.texts[0]; x, y, w, h = slot
        sw, sh = float(shape["width"]), float(shape["height"])
        s = min(w / sw, h / sh, self.s_area * 1.3)
        nw, nh = sw * s, sh * s
        fs, m, nw2 = self.button_geom(u, nw, nh)
        nw = max(nw, nw2)
        if nw > w + 0.5 or nh > h + 0.5:
            raise LayoutFail(f"button {u.uid}")
        px, py = self._align(x, y, w, h, nw, nh, align)
        self.set_box(shape, px, py, nw, nh)
        ty = py + nh / 2 + m["cap"] / 2 - m["asc"]            # cap-centre (renderer top-aligns text)
        self.set_box(label, px, ty, nw, m["asc"] + m["desc"])
        self.out[label["id"]]["font_size"] = fs
        for e in u.members:
            if e is not shape and e is not label:
                self.set_box(e, px + (float(e["x"]) - float(shape["x"])) * s, py + (float(e["y"]) - float(shape["y"])) * s,
                             float(e["width"]) * s, float(e["height"]) * s)

    def put_rigid(self, u, slot, align=("center", "center")):
        """Uniform scale of a (possibly rotated) group about its AABB; rotation preserved."""
        x, y, w, h = slot; bx, by, bw, bh = u.box
        s = min(w / bw, h / bh)
        nw, nh = bw * s, bh * s
        px, py = self._align(x, y, w, h, nw, nh, align)
        for e in u.members:
            ecx, ecy = float(e["x"]) + float(e["width"]) / 2, float(e["y"]) + float(e["height"]) / 2
            ncx, ncy = px + (ecx - bx) * s, py + (ecy - by) * s
            ew, eh = float(e["width"]) * s, float(e["height"]) * s
            self.set_box(e, ncx - ew / 2, ncy - eh / 2, ew, eh)
            if e["type"] == "text":
                fs, m = fit_text(e, ew, max(eh, 1), max(1, int(float(e.get("font_size", 12)) * s)), 1, max_lines=1)
                self.out[e["id"]]["font_size"] = fs
                self.set_box(e, ncx - ew / 2, ncy + m["cap"] / 2 - m["asc"], ew, m["asc"] + m["desc"])

    def put_background(self):
        for u in self.role("background"):
            self.set_box(u.members[0], 0, 0, self.TW, self.TH)

    def put_decorations(self):
        s = self.s_min
        for u in self.role("decoration"):
            for e in u.members:
                w, h = float(e["width"]) * s, float(e["height"]) * s
                cx = (float(e["x"]) + float(e["width"]) / 2) / self.W * self.TW
                cy = (float(e["y"]) + float(e["height"]) / 2) / self.H * self.TH
                self.set_box(e, min(max(cx - w / 2, 0), self.TW - w), min(max(cy - h / 2, 0), self.TH - h), w, h)

    def pin_corner(self, u, h, put, bottom=None):
        """Pin a unit to the corner it occupied in the source, size by height h, aspect locked.
        `bottom` lets callers keep it above an already-placed legal line."""
        left, top = u.cx < self.W / 2, u.cy < self.H / 2
        w = h * u.box[2] / u.box[3]
        x = self.m if left else self.TW - self.m - w
        y = self.m if top else (self.TH - self.m if bottom is None else bottom) - h
        put(u, (x, y, w, h))

    # ---- shared pieces ------------------------------------------------------
    def legal_line(self, x0, x1, y1, hmax_frac):
        legal = self.one("legal")
        if not legal:
            return y1
        e = legal.texts[0]
        fs, m = fit_text(e, x1 - x0, hmax_frac * self.TH, self.text_cap(legal), self.floor_of(legal))
        self.put_text(legal, (x0, y1 - m["h"], x1 - x0, m["h"]), ("center", "bottom"))
        return y1 - m["h"] - self.g

    def top_bar(self, x0, x1, y0, bar_h, logo_in_bar=True):
        """badge(s) left | logo centre | ribbon right. Returns new y0 (unchanged if nothing to place)."""
        logo = self.one("logo"); ribbon = self.one("ribbon")
        anchored_badges = [u for u in self.role("badge") if u.anchored]
        lanes = {}
        if logo and logo_in_bar:
            lanes["c"] = [self.item(logo, 1.0, hmax_frac=0.10)]
        if ribbon:
            lanes.setdefault("r" if ribbon.cx >= self.W / 2 else "l", []).append(self.item(ribbon, 1.0))
        for b in anchored_badges:
            lanes.setdefault("l" if b.cx < self.W / 2 else "r", []).append(self.item(b, 1.0))
        if not lanes:
            return y0, []
        lane_w = (x1 - x0) / 3
        for k, its in lanes.items():
            lx = x0 + {"l": 0, "c": lane_w, "r": 2 * lane_w}[k]
            al = {"l": "left", "c": "center", "r": "right"}[k]
            self.row(its, (lx, y0, lane_w, bar_h), "top") if len(its) > 1 else self.write(its[0], (lx, y0, lane_w, bar_h), (al, "top"))
        return y0 + bar_h + self.g, [u for u in anchored_badges] + ([ribbon] if ribbon else []) + ([logo] if logo and logo_in_bar else [])

    def text_items(self):
        return [self.item(u, w) for r, w in (("headline", 2.0), ("subhead", 1.2), ("body", 1.2), ("caption", 0.6)) for u in self.role(r)]

    def misc_items(self):
        return [self.item(u, 0.8) for u in self.units if u.role in ("other", "image")]

    def badge_block(self, badges, w_hint, axis_pref):
        """badges as a row if their minimum widths fit, else a column."""
        its = [self.item(b, 1.0) for b in badges]
        if axis_pref == "x" and sum(it["minw"](self.TH) for it in its) + self.g * (len(its) - 1) <= w_hint:
            return self.item_group("__badges", its, "x", 0.7)
        return self.item_group("__badges", its, "y", 0.7)

    # ---- grammars -------------------------------------------------------------
    def grammar_stack(self, variant):
        """square / tall / ultra_tall: [top bar] / hero / headline / subhead / body / badges / cta / legal."""
        TW, TH, m, g = self.TW, self.TH, self.m, self.g
        x0, x1, y0, y1 = m, TW - m, m, TH - m
        narrow = self.cls == "ultra_tall"
        y1 = self.legal_line(x0, x1, y1, 0.08)
        bar_h = max(24.0, min(0.10 * TH, 0.5 * (x1 - x0) / 3))
        placed = []
        if narrow:
            logo = self.one("logo")
            if logo:
                self.put_image(logo, (x0, y0, x1 - x0, bar_h)); y0 += bar_h + g; placed.append(logo)
            rb = self.one("ribbon")
            if rb:
                self.pin_corner(rb, max(14.0, 0.05 * TH), self.put_rigid); placed.append(rb)
        else:
            y0, placed = self.top_bar(x0, x1, y0, bar_h)
        items = []
        hero = self.one("hero")
        if hero:
            items.append(self.item(hero, 3.0 if variant == "A" else 1.5, wmax_frac=0.9, hmax_frac=(0.5 if variant == "A" else 0.25)))
        items += self.text_items() + self.misc_items()
        badges = [b for b in self.role("badge") if b not in placed]
        if badges:
            items.append(self.badge_block(badges, x1 - x0, "y" if narrow else "x"))
        cta = self.one("cta")
        if cta:
            items.append(self.item(cta, 0.9))
        self.col(items, (x0, y0, x1 - x0, y1 - y0))
        wm = self.one("watermark")
        if wm:
            self.pin_corner(wm, max(12.0, 0.06 * TH), self.put_image, bottom=y1 + g)

    def grammar_split(self, variant):
        """wide (1.2..3): [top bar] / hero column | text column / legal."""
        TW, TH, m, g = self.TW, self.TH, self.m, self.g
        x0, x1, y0, y1 = m, TW - m, m, TH - m
        y1 = self.legal_line(x0, x1, y1, 0.08)
        bar_h = max(24.0, min(0.12 * TH, 0.5 * (x1 - x0) / 3))
        y0, placed = self.top_bar(x0, x1, y0, bar_h, logo_in_bar=(self.one("ribbon") is not None or any(b.anchored for b in self.role("badge"))))
        hero = self.one("hero")
        hero_left = hero is not None and hero.cx <= self.W / 2
        tx0, tw = x0, x1 - x0
        if hero:
            cw = (0.40 if variant == "A" else 0.30) * (x1 - x0)
            self.put_image(hero, (x0 if hero_left else x1 - cw, y0, cw, y1 - y0))
            tx0, tw = (x0 + cw + g if hero_left else x0), (x1 - x0) - cw - g
        items = []
        logo = self.one("logo")
        if logo and logo not in placed:
            items.append(self.item(logo, 0.8, hmax_frac=0.14))
        items += self.text_items() + self.misc_items()
        badges = [b for b in self.role("badge") if b not in placed]
        if badges:
            items.append(self.badge_block(badges, tw, "x"))
        cta = self.one("cta")
        if cta:
            items.append(self.item(cta, 0.8))
        self.col(items, (tx0, y0, tw, y1 - y0))
        wm = self.one("watermark")
        if wm:
            self.pin_corner(wm, max(12.0, 0.08 * TH), self.put_image, bottom=y1 + g)

    def grammar_strip(self, variant):
        """ultra_wide (>3): logo | hero | headline+subhead | badges | ribbon | cta  ; legal thin line ; watermark corner."""
        TW, TH, m, g = self.TW, self.TH, self.m, self.g
        x0, x1, y0, y1 = m, TW - m, m, TH - m
        y1 = self.legal_line(x0, x1, y1, 0.12)
        wm = self.one("watermark")
        if wm:
            h = max(10.0, 0.22 * TH)
            self.pin_corner(wm, h, self.put_image); x1 -= h * wm.box[2] / wm.box[3] + g
        items = []
        logo, hero = self.one("logo"), self.one("hero")
        anchored = [b for b in self.role("badge") if b.anchored]
        if logo and anchored:   # edge-anchored badge(s) stack with the logo in the first column
            items.append(self.item_group("__brand", [self.item(b, 1.0) for b in anchored] + [self.item(logo, 1.0)], "y", 1.0))
        elif logo:
            items.append(self.item(logo, 1.0))
        if hero:
            items.append(self.item(hero, 1.0))
        texts = self.text_items()
        if texts:
            items.append(self.item_group("__texts", texts, "y", 4.0))
        side = [self.item(b, 1.0) for b in self.role("badge") if not (logo and b.anchored)]
        ribbon = self.one("ribbon")
        if variant == "B" and ribbon:
            side.append(self.item(ribbon, 1.0))
        if side:
            items.append(self.item_group("__side", side, "y", 1.2))
        if ribbon and variant != "B":
            items.append(self.item(ribbon, 1.0))
        items += self.misc_items()
        cta = self.one("cta")
        if cta:
            items.append(self.item(cta, 1.4))
        self.row(items, (x0, y0, x1 - x0, y1 - y0))

    # ---- validation / fallback / driver ------------------------------------------
    def validate(self):
        probs, TW, TH = [], self.TW, self.TH
        els = list(self.out.values())
        for e in els:
            b = aabb(e)
            if b[0] < -0.5 or b[1] < -0.5 or b[0] + b[2] > TW + 0.5 or b[1] + b[3] > TH + 0.5:
                probs.append(f"offcanvas {e['id']} {tuple(round(v, 1) for v in b)}")
            if e["type"] == "image":
                src = next(s for s in self.src["elements"] if s["id"] == e["id"])
                if src.get("fit") != "cover" and abs((src["width"] / src["height"]) / (e["width"] / e["height"]) - 1) > 0.01:
                    probs.append(f"aspect {e['id']}")
            if e["type"] == "text" and not fits(e, e["font_size"], e["width"], e["height"])[0]:
                probs.append(f"textfit {e['id']}")
        uid_of = {e["id"]: u.uid for u in self.units for e in u.members}
        texts = [e for e in els if e["type"] == "text"]
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                a, b = texts[i], texts[j]
                if uid_of[a["id"]] != uid_of[b["id"]] and inter(aabb(a), aabb(b)) > 1:
                    probs.append(f"text-overlap {a['id']}x{b['id']}")
        return probs

    def fallback(self):
        self.reset(1.0); self.put_background()
        s = self.s_min
        ox, oy = (self.TW - self.W * s) / 2, (self.TH - self.H * s) / 2
        for u in self.units:
            if u.role == "background":
                continue
            for e in u.members:
                self.set_box(e, ox + float(e["x"]) * s, oy + float(e["y"]) * s, float(e["width"]) * s, float(e["height"]) * s)
                if e["type"] == "text":
                    self.out[e["id"]]["font_size"] = max(1, int(float(e.get("font_size", 12)) * s))
                b = aabb(self.out[e["id"]])      # clamp rotated AABB into canvas by shifting
                dx = min(0, -b[0]) + min(0, self.TW - (b[0] + b[2])); dy = min(0, -b[1]) + min(0, self.TH - (b[1] + b[3]))
                self.out[e["id"]]["x"] = round(self.out[e["id"]]["x"] + dx, 2); self.out[e["id"]]["y"] = round(self.out[e["id"]]["y"] + dy, 2)
        self.log.append("FALLBACK")

    def run(self):
        grammar = {"ultra_wide": self.grammar_strip, "wide": self.grammar_split}.get(self.cls, self.grammar_stack)
        tried = []
        for fscale in (1.0, 0.85, 0.7, 0.55, 0.4, 0.25):
            for variant in ("A", "B"):
                self.reset(fscale)
                try:
                    self.put_background(); self.put_decorations(); grammar(variant)
                    probs = self.validate()
                    if not any(p.startswith(("offcanvas", "textfit", "text-overlap")) for p in probs):
                        self.log += probs + [f"variant={variant} floor_scale={fscale}"] + [f"tried={tried}"] if tried else probs + [f"variant={variant} floor_scale={fscale}"]
                        return self.result()
                    tried.append((fscale, variant, probs[:2]))
                except LayoutFail as lf:
                    tried.append((fscale, variant, str(lf)))
                except Exception as ex:  # never raise
                    tried.append((fscale, variant, f"EXC {type(ex).__name__}: {ex}"))
        self.fallback(); self.log.append(f"tried={tried[:3]}...")
        return self.result()

    def result(self):
        return {"canvas": {"width": int(self.TW), "height": int(self.TH)}, "elements": [self.out[e["id"]] for e in self.src["elements"]]}


def resize(source, target_canvas):
    eng = Engine(source, target_canvas)
    return eng.run(), eng
