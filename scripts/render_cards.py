#!/usr/bin/env python3
"""Render the profile README's animated SVG cards into cards/.

    pip install fonttools brotli
    python scripts/render_cards.py            # animated cards -> cards/
    STATIC=1 python scripts/render_cards.py   # final frame    -> preview/ (open preview/index.html)

Every card is written twice, `<name>-dark.svg` and `<name>-light.svg`; the
README picks one with <picture>. The cards are shown through <img>, so they
carry no scripts and no external requests: animation is CSS keyframes in an
inline <style> plus SMIL, and the fonts are subset with pyftsubset to the
glyphs each card uses and embedded as base64 WOFF.

Every animation only describes where an element comes *from*. The markup is
the final frame, so turning animation off (prefers-reduced-motion, or
STATIC=1) leaves the finished card rather than a blank one.

Data: data/contributions.json, the raw GraphQL response the workflow in
.github/workflows/cards.yml refreshes daily. Output is deterministic for a
given JSON file, so the workflow only commits when something changed.

Optional portrait: put assets/portrait.jpg (or .png / .webp), ideally square
and about 400px, and the hero shows it in place of the monogram.

Colours and type come from lloyd-portfolio (DESIGN.md, src/styles/global.css).
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import json
import os
import shutil
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
STATIC = os.environ.get("STATIC", "") not in ("", "0")
OUT = ROOT / ("preview" if STATIC else "cards")
DATA = ROOT / "data" / "contributions.json"

# ---------------------------------------------------------------- content

PROFILE = {
    "name": "Lloyd D’Souza",
    "line": "I build AI software that shows its work.",
    "role": "AI/ML engineering student · applied machine learning and backend systems",
}

# Three tiles, left to right. The README wraps each in a link to `href`.
TILES = [
    {"id": "portfolio", "icon": "globe", "label": "Portfolio",
     "sub": "lloyd-portfolio-sand.vercel.app", "href": "https://lloyd-portfolio-sand.vercel.app"},
    {"id": "linkedin", "icon": "linkedin", "label": "LinkedIn",
     "sub": "linkedin.com/in/lloyddso", "href": "https://www.linkedin.com/in/lloyddso"},
    {"id": "email", "icon": "mail", "label": "Email",
     "sub": "lloyd25dsouza@gmail.com", "href": "mailto:lloyd25dsouza@gmail.com"},
]

# One card each, in this order. `repo` is the repository name in the JSON;
# the description, language and last push are read from there. `title` and
# `stack` are optional and match the wording on the portfolio.
FEATURED = [
    {"repo": "TND-The-News-Detector", "title": "Trustworthy News Detector",
     "stack": "Python, FastAPI, Pydantic, Ollama, Trafilatura, HTTPX"},
    {"repo": "DataSentry", "title": "DataSentry",
     "stack": "Python, FastAPI, Pydantic, pandas, NumPy, scikit-learn"},
]

# ---------------------------------------------------------------- tokens

THEMES = {
    "dark": dict(ground="#16161a", sheet="#1e1e23", sheet2="#25252b", ink="#ede6da",
                 ink2="#a39e94", rule="#33333a", rule2="#4d4d55", accent="#e0a84f"),
    "light": dict(ground="#f2eee6", sheet="#fbf9f4", sheet2="#ebe6dc", ink="#1b1b1f",
                  ink2="#57524a", rule="#d5cfc3", rule2="#a9a397", accent="#8f5b0e"),
}

FONT_FILES = {
    "sans": ROOT / "fonts" / "archivo-latin-wdth-normal.woff2",
    "mono": ROOT / "fonts" / "jetbrains-mono-latin-wght-normal.woff2",
}
FONT_FACE = {
    "sans": ("Archivo Card", "font-weight:100 900;font-stretch:62% 125%"),
    "mono": ("JetBrains Mono Card", "font-weight:100 800"),
}
FONT_STACK = {
    "sans": "'Archivo Card',Archivo,system-ui,sans-serif",
    "mono": "'JetBrains Mono Card','JetBrains Mono',ui-monospace,monospace",
}

# name: (face, size px, weight, width %, letter-spacing em)
STYLES = {
    "display": ("sans", 64, 700, 125, -0.035),
    "head": ("sans", 22, 620, 118, -0.02),
    "state": ("sans", 25, 560, 100, -0.015),
    "title": ("sans", 18, 620, 118, -0.01),
    "body": ("sans", 15.5, 400, 100, 0),
    "small": ("sans", 13.5, 400, 100, 0),
    "strong": ("sans", 14.5, 600, 100, 0),
    "label": ("sans", 13.5, 600, 78, 0.01),
    "mono": ("mono", 12.5, 400, 100, 0),
}

W = 840            # full-width card, in SVG user units
PAD = 36
TILE_W = 273       # 32.5% of W, so tile text is the same size as card text
EASE = "cubic-bezier(.22,1,.36,1)"   # --ease-smooth-out on the site

ICONS = {  # 24px grid, 1.75 stroke: the site's icon set
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.5 3.9 5.5 3.9 9s-1.3 6.5-3.9 9c-2.6-2.5-3.9-5.5-3.9-9S9.4 5.5 12 3Z"/>',
    "linkedin": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 10.5V16M8 7.5v.01M12 16v-5.5M12 13a2.5 2.5 0 0 1 5 0v3"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="1.5"/><path d="m3.5 6 8.5 7 8.5-7"/>',
}
ARROW = "M7 17 17 7M8 7h9v9"
MONOGRAM = "M8 8v16h7M18 8v16h2.5a5.5 5.5 0 0 0 5.5-5.5v-5A5.5 5.5 0 0 0 20.5 8H18"

MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()

# ---------------------------------------------------------------- fonts


@lru_cache(maxsize=None)
def advances(face: str, weight: float, width: float) -> dict[int, float]:
    """Advance width in em per code point at one weight/width, for layout."""
    font = TTFont(FONT_FILES[face])
    axes = {a.axisTag: (a.minValue, a.maxValue) for a in font["fvar"].axes}
    want = {"wght": weight, "wdth": width}
    loc = {t: min(max(want[t], lo), hi) for t, (lo, hi) in axes.items()}
    inst = instancer.instantiateVariableFont(font, loc)
    upm = inst["head"].unitsPerEm
    hmtx = inst["hmtx"]
    return {cp: hmtx[g][0] / upm for cp, g in inst.getBestCmap().items()}


def measure(text: str, style: str, size: float | None = None) -> float:
    face, base, weight, width, track = STYLES[style]
    size = size or base
    adv = advances(face, weight, width)
    return sum(adv.get(ord(c), 0.6) + track for c in text) * size


def wrap(text: str, style: str, max_w: float, max_lines: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and measure(trial, style) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        last = lines[max_lines - 1]
        while last and measure(last + "…", style) > max_w:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        lines = lines[: max_lines - 1] + [last.rstrip(",.;: ") + "…"]
    return lines


@lru_cache(maxsize=None)
def woff_data_uri(face: str, chars: str) -> str:
    """pyftsubset the variable font to `chars` and return a WOFF data URI."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "subset.woff"
        subset.main([  # fontTools.subset.main is the pyftsubset entry point
            str(FONT_FILES[face]),
            f"--unicodes={','.join(f'U+{ord(c):04X}' for c in chars)}",
            "--flavor=woff",
            "--layout-features=kern,tnum",
            "--no-hinting",
            "--name-IDs=",
            "--notdef-outline",
            f"--output-file={out}",
        ])
        return "data:font/woff;base64," + base64.b64encode(out.read_bytes()).decode()


# ---------------------------------------------------------------- svg


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def n(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def mix(a: str, b: str, t: float) -> str:
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


class Card:
    def __init__(self, name: str, w: float, h: float, theme: str, label: str):
        self.name, self.w, self.h, self.theme, self.label = name, w, h, theme, label
        self.c = THEMES[theme]
        self.parts: list[str] = []
        self.chars: dict[str, set[str]] = {"sans": set(), "mono": set()}
        self.defs: list[str] = []

    # -- animation attributes ------------------------------------------
    def anim(self, kind: str | None, delay: float, extra_cls: str = "") -> str:
        cls = " ".join(x for x in (extra_cls, kind) if x)
        out = f' class="{cls}"' if cls else ""
        if kind and not STATIC:
            out += f' style="animation-delay:{n(delay)}s"'
        return out

    # -- primitives ----------------------------------------------------
    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, s, style, fill="ink", anchor="start", kind=None, delay=0.0, size=None):
        self.chars[STYLES[style][0]].update(s)
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        fs = f' font-size="{n(size)}"' if size else ""
        self.add(f'<text x="{n(x)}" y="{n(y)}" fill="{self.c[fill]}"{a}{fs}'
                 f'{self.anim(kind, delay, "t-" + style)}>{esc(s)}</text>')

    def rule(self, x1, y, x2, color="rule", width=1, kind="gx", delay=0.0, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<path d="M{n(x1)} {n(y)}H{n(x2)}" stroke="{self.c[color]}" '
                 f'stroke-width="{n(width)}"{d}{self.anim(kind, delay)}/>')

    def icon(self, x, y, size, body, color="ink", kind="fade", delay=0.0):
        s = size / 24
        self.add(f'<g transform="translate({n(x)} {n(y)}) scale({n(s)})"><g fill="none" '
                 f'stroke="{self.c[color]}" stroke-width="1.75" stroke-linecap="round" '
                 f'stroke-linejoin="round"{self.anim(kind, delay)}>{body}</g></g>')

    def arrow(self, x, y, size, delay):
        body = f'<path d="{ARROW}" pathLength="1"{self.anim("draw", delay + 0.1)}/>'
        self.icon(x, y, size, body, color="ink2", kind=None)

    # -- output --------------------------------------------------------
    def css(self) -> str:
        rules = []
        for face, chars in self.chars.items():
            if chars:
                fam, desc = FONT_FACE[face]
                uri = woff_data_uri(face, "".join(sorted(chars)))
                rules.append(f"@font-face{{font-family:'{fam}';{desc};src:url({uri}) format('woff')}}")
        rules.append("text{font-variant-numeric:tabular-nums;font-kerning:normal}")
        for name, (face, size, weight, width, track) in STYLES.items():
            r = f".t-{name}{{font-family:{FONT_STACK[face]};font-size:{n(size)}px;font-weight:{weight}"
            if face == "sans" and width != 100:
                r += f";font-stretch:{width}%"
            if track:
                r += f";letter-spacing:{n(track)}em"
            rules.append(r + "}")
        if STATIC:
            rules.append("*{animation:none!important}")
        else:
            rules += [
                f".up{{animation:up .7s {EASE} both}}",
                f".fade{{animation:fade .6s {EASE} both}}",
                f".rise{{animation:rise .9s {EASE} both}}",
                f".gx{{transform-box:fill-box;transform-origin:0 50%;animation:gx .8s {EASE} both}}",
                f".gy{{transform-box:fill-box;transform-origin:50% 100%;animation:gy .8s {EASE} both}}",
                f".pop{{transform-box:fill-box;transform-origin:50% 50%;animation:pop .5s {EASE} both}}",
                f".draw{{stroke-dasharray:1;animation:draw .8s {EASE} both}}",
                "@keyframes up{from{opacity:0;transform:translateY(8px)}}",
                "@keyframes fade{from{opacity:0}}",
                "@keyframes rise{from{opacity:0;transform:translateY(42px)}}",
                "@keyframes gx{from{transform:scaleX(0)}}",
                "@keyframes gy{from{transform:scaleY(0)}}",
                "@keyframes pop{from{opacity:0;transform:scale(.4)}}",
                "@keyframes draw{from{stroke-dashoffset:1}}",
                # Reduced motion: remove the animations (the markup is the final
                # frame) and take the SMIL-driven elements out of the render tree.
                "@media (prefers-reduced-motion:reduce){*{animation:none!important}.smil{display:none}}",
            ]
        return "".join(rules)

    def svg(self) -> str:
        c = self.c
        frame = (f'<rect x=".5" y=".5" width="{n(self.w - 1)}" height="{n(self.h - 1)}" rx="2" '
                 f'fill="{c["ground"]}" stroke="{c["rule"]}"/>')
        defs = f'<defs>{"".join(self.defs)}</defs>' if self.defs else ""
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n(self.w)} {n(self.h)}" '
                f'width="{n(self.w)}" height="{n(self.h)}" role="img" aria-label="{esc(self.label)}">'
                f'<title>{esc(self.label)}</title><style>{self.css()}</style>{defs}{frame}'
                f'{"".join(self.parts)}</svg>\n')

    def write(self) -> Path:
        path = OUT / f"{self.name}-{self.theme}.svg"
        path.write_text(self.svg(), encoding="utf-8")
        return path


# ---------------------------------------------------------------- data


def load_data() -> dict:
    user = json.loads(DATA.read_text(encoding="utf-8"))["data"]["user"]
    days = [(dt.date.fromisoformat(d["date"]), int(d["contributionCount"]))
            for wk in user["contributionsCollection"]["contributionCalendar"]["weeks"]
            for d in wk["contributionDays"]]
    days.sort()
    repos = {r["name"]: r for r in user["repositories"]["nodes"]}
    return {"days": days, "repos": repos}


def stats(days: list[tuple[dt.date, int]]) -> dict:
    longest = run = 0
    for _, cnt in days:
        run = run + 1 if cnt else 0
        longest = max(longest, run)
    # A streak is still alive if today has nothing yet but yesterday did.
    tail = days[:-1] if days and days[-1][1] == 0 else days
    current = 0
    for _, cnt in reversed(tail):
        if not cnt:
            break
        current += 1
    busiest = max(days, key=lambda d: (d[1], d[0]))
    return {
        "total": sum(c for _, c in days),
        "active": sum(1 for _, c in days if c),
        "span": len(days),
        "current": current,
        "longest": longest,
        "busiest": busiest,
    }


def monthly(days: list[tuple[dt.date, int]], count: int = 12) -> list[tuple[int, int, int]]:
    """(year, month, total) for the last `count` calendar months, oldest first."""
    last = days[-1][0]
    keys = []
    y, m = last.year, last.month
    for _ in range(count):
        keys.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    keys.reverse()
    totals = dict.fromkeys(keys, 0)
    for d, c in days:
        if (d.year, d.month) in totals:
            totals[(d.year, d.month)] += c
    return [(y, m, t) for (y, m), t in totals.items()]


def nice_ticks(peak: int) -> list[int]:
    """0 plus two or three round gridline values that cover `peak`."""
    peak = max(peak, 1)
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000):
        if step * 3 >= peak:
            top = -(-peak // step)
            return [step * i for i in range(max(top, 2) + 1)]
    return [0, peak]


def plural(v: int, word: str) -> str:
    return f"{v} {word}" + ("" if v == 1 else "s")


def day_label(d: dt.date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


# ---------------------------------------------------------------- cards


def hero(theme: str, t0: float) -> Card:
    h = 248
    card = Card("hero", W, h, theme, f"{PROFILE['name']}. {PROFILE['line']} {PROFILE['role']}")
    c = card.c
    box = 148
    bx, by = W - PAD - box, (h - box) / 2
    text_w = bx - PAD - PAD

    # Background grid, as on the site: ink hairlines on a 56px cell.
    grid = "".join(f"M{x} 0V{h}" for x in range(56, W, 56)) + "".join(f"M0 {y}H{W}" for y in range(56, h, 56))
    card.add(f'<path d="{grid}" stroke="{c["ink"]}" stroke-opacity=".07"{card.anim("fade", t0)}/>')
    if not STATIC:
        # One amber signal runs the bottom grid line once, then is gone.
        card.add(f'<rect class="smil" x="-80" y="223.25" width="72" height="1.5" fill="{c["accent"]}">'
                 f'<animate attributeName="x" from="-80" to="{W}" begin="{n(t0 + 0.9)}s" dur="1.5s" '
                 f'calcMode="spline" keyTimes="0;1" keySplines=".45 0 .25 1" fill="freeze"/></rect>')

    size = STYLES["display"][1]
    size = min(size, size * text_w / measure(PROFILE["name"], "display"))
    card.defs.append(f'<clipPath id="n"><rect x="0" y="40" width="{n(bx - PAD / 2)}" height="84"/></clipPath>')
    card.add('<g clip-path="url(#n)">')
    card.text(PAD, 108, PROFILE["name"], "display", kind="rise", delay=t0 + 0.1, size=size)
    card.add("</g>")
    card.rule(PAD, 136, PAD + text_w, "rule2", delay=t0 + 0.35)
    card.text(PAD, 174, PROFILE["line"], "state", kind="up", delay=t0 + 0.5)
    card.text(PAD, 204, PROFILE["role"], "body", fill="ink2", kind="up", delay=t0 + 0.62)

    portrait = next((p for p in (ROOT / "assets" / f"portrait.{e}" for e in ("jpg", "jpeg", "png", "webp"))
                     if p.exists()), None)
    frame = (f'<rect x="{n(bx + .5)}" y="{n(by + .5)}" width="{box - 1}" height="{box - 1}" rx="2" '
             f'fill="{{fill}}" stroke="{c["rule2"]}"/>')
    card.add(f'<g{card.anim("fade", t0 + 0.3)}>')
    if portrait:
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}[portrait.suffix[1:]]
        uri = f"data:image/{mime};base64," + base64.b64encode(portrait.read_bytes()).decode()
        card.defs.append(f'<clipPath id="p"><rect x="{n(bx)}" y="{n(by)}" width="{box}" height="{box}" rx="2"/></clipPath>')
        card.add(f'<image href="{uri}" x="{n(bx)}" y="{n(by)}" width="{box}" height="{box}" '
                 f'preserveAspectRatio="xMidYMid slice" clip-path="url(#p)"/>')
        card.add(frame.format(fill="none"))
        card.add("</g>")
    else:
        card.add(frame.format(fill=mix(c["ground"], c["sheet"], 0.55)))
        card.add("</g>")
        s = 100 / 32
        card.add(f'<g transform="translate({n(bx + 24)} {n(by + 24)}) scale({n(s)})">'
                 f'<path d="{MONOGRAM}" pathLength="1" fill="none" stroke="{c["ink"]}" stroke-width="2.5" '
                 f'stroke-linejoin="round"{card.anim("draw", t0 + 0.5)}/>'
                 f'<circle cx="13" cy="24" r="1.6" fill="{c["accent"]}"{card.anim("pop", t0 + 1.2)}/></g>')
    return card


def tile(theme: str, spec: dict, t0: float) -> Card:
    card = Card(f"tile-{spec['id']}", TILE_W, 100, theme, f"{spec['label']}: {spec['sub']}")
    card.icon(20, 18, 22, ICONS[spec["icon"]], delay=t0)
    card.arrow(TILE_W - 20 - 18, 20, 18, t0 + 0.15)
    card.text(20, 67, spec["label"], "title", kind="up", delay=t0 + 0.08)
    card.text(20, 85, spec["sub"], "small", fill="ink2", kind="up", delay=t0 + 0.16, size=12.5)
    return card


def activity(theme: str, data: dict, t0: float) -> Card:
    st = stats(data["days"])
    months = monthly(data["days"])
    busy_day, busy_n = st["busiest"]
    card = Card("activity", W, 336, theme,
                f"Contributions per month over the last 12 months: {st['total']} in total. "
                f"Current streak {plural(st['current'], 'day')}, longest {plural(st['longest'], 'day')}, "
                f"active on {st['active']} of {st['span']} days, busiest day {day_label(busy_day)} with {busy_n}.")
    c = card.c
    card.text(PAD, 58, "Contributions per month", "head", kind="up", delay=t0)
    card.text(PAD, 82, f"{plural(st['total'], 'contribution')} in the last 12 months", "small",
              fill="ink2", kind="up", delay=t0 + 0.08)

    # -- bars: one series, so ink carries it; no legend needed ----------
    x0, x1, top, base = PAD + 30, 528, 124, 276
    ticks = nice_ticks(max(t for *_, t in months))
    scale = (base - top) / ticks[-1]
    for v in ticks:
        y = base - v * scale
        card.rule(x0, y, x1, "rule2" if v == 0 else "rule", delay=t0 + 0.15)
        card.text(x0 - 8, y + 4.5, str(v), "small", fill="ink2", anchor="end", kind="fade",
                  delay=t0 + 0.2, size=12.5)
    slot = (x1 - x0) / len(months)
    bw = 20
    peak_i = max(range(len(months)), key=lambda i: months[i][2])
    last_i = len(months) - 1
    for i, (year, month, total) in enumerate(months):
        cx = x0 + slot * (i + 0.5)
        delay = t0 + 0.3 + i * 0.045
        if total:
            hgt = max(total * scale, 2)
            r = min(2, hgt)
            d = (f"M{n(cx - bw / 2)} {n(base)}V{n(base - hgt + r)}q0 {n(-r)} {n(r)} {n(-r)}"
                 f"H{n(cx + bw / 2 - r)}q{n(r)} 0 {n(r)} {n(r)}V{n(base)}Z")
            if i == last_i:
                # The month still in progress is drawn open: dashed outline, sheet fill.
                card.add(f'<path d="{d}" fill="{c["sheet2"]}" stroke="{c["ink"]}" stroke-width="1.25" '
                         f'stroke-dasharray="3 3"{card.anim("gy", delay)}/>')
            else:
                card.add(f'<path d="{d}" fill="{c["ink"]}"{card.anim("gy", delay)}/>')
            if i in (peak_i, last_i):
                card.text(cx, base - hgt - 7, str(total), "strong", anchor="middle", kind="fade",
                          delay=delay + 0.45, size=13)
        card.text(cx, base + 19, MONTHS[month - 1], "label", fill="ink2", anchor="middle",
                  kind="fade", delay=t0 + 0.25, size=12.5)
        if i == 0 or month == 1:
            card.text(cx, base + 35, str(year), "small", fill="ink2", anchor="middle",
                      kind="fade", delay=t0 + 0.25, size=11.5)
    note = "Month in progress"
    lx = x1 - measure(note, "label", 12.5)
    card.text(lx, 82, note, "label", fill="ink2", kind="fade", delay=t0 + 1.0, size=12.5)
    card.add(f'<g{card.anim("fade", t0 + 1.0)}><rect x="{n(lx - 18)}" y="72.5" width="11" height="11" rx="1.5" '
             f'fill="{c["sheet2"]}" stroke="{c["ink"]}" stroke-dasharray="3 3"/></g>')

    # -- key/value rows ---------------------------------------------------
    kx, kx2, row = 572, W - PAD, 38
    rows = [
        ("Current streak", plural(st["current"], "day")),
        ("Longest streak", plural(st["longest"], "day")),
        ("Active days", f"{st['active']} of {st['span']}"),
        ("Busiest day", f"{busy_n} on {busy_day.day} {MONTHS[busy_day.month - 1]}"),
    ]
    for i, (key, val) in enumerate(rows):
        y = top + i * row
        delay = t0 + 0.55 + i * 0.09
        card.rule(kx, y, kx2, "rule2" if i == 0 else "rule", delay=delay)
        card.text(kx, y + 24.5, key, "label", fill="ink2", kind="up", delay=delay + 0.05)
        card.text(kx2, y + 24.5, val, "strong", anchor="end", kind="up", delay=delay + 0.1)
    card.rule(kx, top + len(rows) * row, kx2, "rule", delay=t0 + 0.55 + len(rows) * 0.09)
    return card


def repo_card(theme: str, spec: dict, repo: dict, t0: float) -> Card:
    desc = wrap((spec.get("description") or repo.get("description") or "").strip(), "body", W - 2 * PAD - 60, 3)
    rows = []
    if spec.get("stack"):
        rows.append(("Stack", spec["stack"]))
    elif (repo.get("primaryLanguage") or {}).get("name"):
        rows.append(("Language", repo["primaryLanguage"]["name"]))
    if repo.get("stargazerCount"):
        rows.append(("Stars", str(repo["stargazerCount"])))
    if repo.get("pushedAt"):
        rows.append(("Last push", day_label(dt.date.fromisoformat(repo["pushedAt"][:10]))))
    title = spec.get("title") or repo["name"]
    slug = f"{repo.get('owner', 'lloydaq')}/{repo['name']}"

    desc_y = 114
    rows_y = desc_y + (len(desc) - 1) * 23 + 24 if desc else 100
    h = rows_y + len(rows) * 36 + 26
    card = Card(f"repo-{spec['repo'].lower()}", W, h, theme, f"{title} ({slug}). {' '.join(desc)}")
    # Each project opens with a strong ink rule, as on the site.
    card.rule(PAD, 28, W - PAD, "ink", width=2, delay=t0)
    card.text(PAD, 66, title, "head", kind="up", delay=t0 + 0.12)
    card.text(PAD, 87, slug, "mono", fill="ink2", kind="up", delay=t0 + 0.2)
    card.arrow(W - PAD - 20, 46, 20, t0 + 0.3)
    for i, line in enumerate(desc):
        card.text(PAD, desc_y + i * 23, line, "body", kind="up", delay=t0 + 0.28 + i * 0.06)
    for i, (key, val) in enumerate(rows):
        y = rows_y + i * 36
        delay = t0 + 0.5 + i * 0.09
        card.rule(PAD, y, W - PAD, "rule", delay=delay)
        card.text(PAD, y + 23.5, key, "label", fill="ink2", kind="up", delay=delay + 0.05)
        card.text(PAD + 104, y + 23.5, val, "small", kind="up", delay=delay + 0.1, size=14)
    card.rule(PAD, rows_y + len(rows) * 36, W - PAD, "rule", delay=t0 + 0.5 + len(rows) * 0.09)
    return card


def graph(theme: str, data: dict, t0: float) -> Card:
    days = data["days"]
    total = sum(cnt for _, cnt in days)
    card = Card("graph", W, 222, theme,
                f"Contribution graph: {plural(total, 'contribution')} between "
                f"{day_label(days[0][0])} and {day_label(days[-1][0])}.")
    c = card.c
    # Sequential ramp in one hue: ground -> brand amber. Empty days stay neutral.
    ramp = [mix(c["ground"], c["ink"], 0.07)] + [mix(c["ground"], c["accent"], t) for t in (0.3, 0.52, 0.76, 1)]
    nonzero = sorted(cnt for _, cnt in days if cnt)
    cuts = [nonzero[min(len(nonzero) - 1, int(len(nonzero) * q))] for q in (0.25, 0.5, 0.75)] if nonzero else []

    def level(cnt: int) -> int:
        return 0 if not cnt else 1 + sum(cnt > q for q in cuts)

    card.text(PAD, 56, f"{plural(total, 'contribution')} in the last year", "head", kind="up", delay=t0)

    # Columns are weeks starting on Sunday, like the graph on github.com.
    first = days[0][0]
    start = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    weeks = (days[-1][0] - start).days // 7 + 1
    gx0, gy0 = PAD + 32, 100
    pitch = min(14.0, (W - PAD - gx0) / weeks)
    cell = pitch - 2.5
    cols: dict[int, list[str]] = {}
    prev_label_x = -99.0
    for d, cnt in days:
        wk, dow = (d - start).days // 7, (d.weekday() + 1) % 7
        cols.setdefault(wk, []).append(
            f'<rect x="{n(gx0 + wk * pitch)}" y="{n(gy0 + dow * pitch)}" width="{n(cell)}" '
            f'height="{n(cell)}" rx="2" fill="{ramp[level(cnt)]}"/>')
        if d.day == 1 or d == first:
            x = gx0 + (wk + (1 if dow and d != first else 0)) * pitch
            if x - prev_label_x > 30 and x < W - PAD - 20:
                card.text(x, gy0 - 9, MONTHS[d.month - 1], "label", fill="ink2", kind="fade",
                          delay=t0 + 0.2 + wk * 0.014, size=12)
                prev_label_x = x
    for wk in sorted(cols):
        card.add(f'<g{card.anim("pop", t0 + 0.25 + wk * 0.014)}>{"".join(cols[wk])}</g>')
    for dow, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        card.text(PAD, gy0 + dow * pitch + cell - 1.5, name, "label", fill="ink2", kind="fade",
                  delay=t0 + 0.2, size=12)

    # Legend, top right.
    lx = W - PAD - measure("More", "label", 12)
    card.text(lx, 52, "More", "label", fill="ink2", kind="fade", delay=t0 + 0.2, size=12)
    lx -= 8
    card.add(f'<g{card.anim("fade", t0 + 0.2)}>')
    for i, col in reversed(list(enumerate(ramp))):
        lx -= 11.5
        card.add(f'<rect x="{n(lx)}" y="42" width="11.5" height="11.5" rx="2" fill="{col}"/>')
        lx -= 2.5
    card.add("</g>")
    card.text(lx - 5.5, 52, "Less", "label", fill="ink2", anchor="end", kind="fade", delay=t0 + 0.2, size=12)
    return card


# ---------------------------------------------------------------- main


def preview_page(names: list[str]) -> str:
    def stack(theme: str) -> str:
        out = []
        for name in names:
            width = "32.5%" if name.startswith("tile-") else "100%"
            out.append(f'<img src="{name}-{theme}.svg" width="{width}" alt="">')
        return "\n".join(out)
    bg = {"dark": "#0d1117", "light": "#ffffff"}
    cols = "".join(f'<div style="background:{bg[t]};padding:32px"><div style="max-width:846px;margin:auto">'
                   f'{stack(t)}</div></div>' for t in THEMES)
    return ("<!doctype html><meta charset=utf-8><title>Card preview</title>"
            "<style>body{margin:0}img{margin:0 0 8px}img[width='32.5%']{margin-right:.2%}</style>" + cols)


def main() -> int:
    data = load_data()
    missing = [f["repo"] for f in FEATURED if f["repo"] not in data["repos"]]
    if missing:
        print(f"featured repos not in {DATA.name}: {', '.join(missing)}", file=sys.stderr)
        return 1

    if OUT.exists():
        for old in OUT.glob("*.svg"):
            old.unlink()
    OUT.mkdir(exist_ok=True)

    names: list[str] = []
    for theme in THEMES:
        # Start times step down the page so the README plays top to bottom.
        cards = [hero(theme, 0.0)]
        cards += [tile(theme, spec, 0.8 + i * 0.12) for i, spec in enumerate(TILES)]
        cards.append(activity(theme, data, 1.3))
        cards += [repo_card(theme, f, data["repos"][f["repo"]], 2.1 + i * 0.35) for i, f in enumerate(FEATURED)]
        cards.append(graph(theme, data, 2.1 + len(FEATURED) * 0.35 + 0.2))
        for card in cards:
            path = card.write()
            if card.name not in names:
                names.append(card.name)
            print(f"{path.relative_to(ROOT)}  {path.stat().st_size / 1024:.1f} kB")
    if STATIC:
        (OUT / "index.html").write_text(preview_page(names), encoding="utf-8")
        print(f"final frames: open {(OUT / 'index.html').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    shutil.rmtree(OUT, ignore_errors=True) if STATIC else None
    sys.exit(main())
