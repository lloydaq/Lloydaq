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

Entrance animations only describe where an element comes *from*: the markup
is the final frame, so prefers-reduced-motion (or STATIC=1) leaves the
finished card. Ambient loops (aurora, pulses, marquee, radar) are switched
off the same way, and SMIL-only decoration carries class "smil", which
reduced motion removes from the render tree.

Data: data/contributions.json, the raw GraphQL response the workflow in
.github/workflows/cards.yml refreshes daily. Output is deterministic for a
given JSON file, so the workflow only commits when something changed.

Optional portrait: put assets/portrait.jpg (or .png / .webp), square and
about 400px, and the hero shows it inside the orbit in place of the monogram.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import json
import math
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
    "handle": "lloydaq",
    "line": "I build AI software that shows its work.",
    "role": "AI/ML engineering student · applied machine learning · backend systems",
    "tags": ["Applied ML", "Explainable AI", "Forecasting", "Backend APIs"],
}

# Three tiles, left to right. The README wraps each in a link to `href`.
TILES = [
    {"id": "portfolio", "icon": "globe", "label": "Portfolio", "kicker": "WEB",
     "sub": "lloyd-portfolio-sand.vercel.app", "href": "https://lloyd-portfolio-sand.vercel.app",
     "hue": "amber"},
    {"id": "linkedin", "icon": "linkedin", "label": "LinkedIn", "kicker": "NETWORK",
     "sub": "linkedin.com/in/lloyddso", "href": "https://www.linkedin.com/in/lloyddso",
     "hue": "cyan"},
    {"id": "email", "icon": "mail", "label": "Email", "kicker": "INBOX",
     "sub": "lloyd25dsouza@gmail.com", "href": "mailto:lloyd25dsouza@gmail.com",
     "hue": "rose"},
]

# One card each, in this order. `repo` is the repository name in the JSON;
# the description, language, stars and last push are read from there.
# `visual` picks the animated illustration on the card's left.
FEATURED = [
    {"repo": "TND-The-News-Detector", "title": "Trustworthy News Detector", "visual": "scan",
     "hue": "rose", "kicker": "MISINFORMATION · LLM ANALYSIS",
     "stack": ["Python", "FastAPI", "Pydantic", "Ollama", "Trafilatura", "HTTPX"]},
    {"repo": "DataSentry", "title": "DataSentry", "visual": "grid", "hue": "cyan",
     "kicker": "DATA QUALITY · LEAKAGE · LABELS",
     "stack": ["Python", "FastAPI", "Pydantic", "pandas", "NumPy", "scikit-learn"]},
    {"repo": "flowcast", "title": "Flowcast", "visual": "forecast", "hue": "amber",
     "kicker": "SPATIO-TEMPORAL FORECASTING",
     "description": "Traffic speed forecasting for 207 Los Angeles highway sensors, 15–60 min ahead. "
                    "Leakage-tested features, LightGBM and a DCRNN graph network compared with bootstrap "
                    "significance, plus a React dashboard that replays each forecast against what actually happened.",
     "stack": ["Python", "LightGBM", "DCRNN", "FastAPI", "React"]},
    {"repo": "CampusSignal-pro", "title": "CampusSignal Pro", "visual": "radar", "hue": "violet",
     "kicker": "MULTI-COLLEGE CAMPUS PLATFORM", "stack": ["Python"]},
]

# The toolkit marquee: two rows that drift in opposite directions.
TOOLKIT = [
    ["Python", "FastAPI", "Pydantic", "scikit-learn", "LightGBM", "pandas", "NumPy", "DCRNN"],
    ["React", "TypeScript", "JavaScript", "Ollama", "HTTPX", "Trafilatura", "Git", "GitHub Actions"],
]

# Section headers between blocks of the README: (id, number, title, note).
SECTIONS = [
    ("toolkit", "01", "Toolkit", "what I reach for"),
    ("signal", "02", "Signal", "last 12 months"),
    ("work", "03", "Selected work", "{n} projects"),
    ("city", "04", "Contribution city", "one tower per day"),
]

# ---------------------------------------------------------------- tokens

THEMES = {
    "dark": dict(ground="#07080d", sheet="#0e1018", sheet2="#151824", ink="#f3eee6",
                 ink2="#9b98a3", rule="#1f2230", rule2="#343849",
                 amber="#ffb547", ember="#ff7a45", rose="#ff4d8d", violet="#9b7bff",
                 cyan="#3fd8ff", lime="#9cf26b", glow=1.0),
    "light": dict(ground="#fbf8f2", sheet="#ffffff", sheet2="#f1ede5", ink="#15151c",
                  ink2="#5d5a64", rule="#e4dfd5", rule2="#c9c3b7",
                  amber="#c27400", ember="#d9480f", rose="#d6246b", violet="#6a4ce0",
                  cyan="#0786b3", lime="#3f8f12", glow=0.55),
}
HUES = ["amber", "rose", "violet", "cyan", "ember", "lime"]

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
    "display": ("sans", 76, 820, 125, -0.04),
    "head": ("sans", 27, 720, 120, -0.02),
    "state": ("sans", 21, 520, 100, -0.01),
    "title": ("sans", 19, 700, 118, -0.01),
    "body": ("sans", 15, 420, 100, 0),
    "small": ("sans", 13, 450, 100, 0),
    "strong": ("sans", 14, 650, 100, 0),
    "label": ("sans", 11.5, 700, 112, 0.14),
    "mono": ("mono", 12.5, 450, 100, 0),
    "monob": ("mono", 13, 700, 100, 0),
    "num": ("mono", 46, 800, 100, -0.02),
    "ghost": ("sans", 120, 900, 125, -0.05),
}

W = 840            # full-width card, in SVG user units
PAD = 34
TILE_W = 273       # 32.5% of W, so tile text is the same size as card text
EASE = "cubic-bezier(.22,1,.36,1)"

ICONS = {  # 24px grid, 1.75 stroke
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.5 3.9 5.5 3.9 9s-1.3 6.5-3.9 9c-2.6-2.5-3.9-5.5-3.9-9S9.4 5.5 12 3Z"/>',
    "linkedin": '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 10.5V16M8 7.5v.01M12 16v-5.5M12 13a2.5 2.5 0 0 1 5 0v3"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6 8.5 7 8.5-7"/>',
    "star": '<path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.8-5.2 2.8 1-5.8-4.3-4.1 5.9-.9Z"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "code": '<path d="m8.5 8-4 4 4 4M15.5 8l4 4-4 4M13.5 5.5l-3 13"/>',
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


def wrap(text: str, style: str, max_w: float, max_lines: int, size: float | None = None) -> list[str]:
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and measure(trial, style, size) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        last = lines[max_lines - 1]
        while last and measure(last + "…", style, size) > max_w:
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


# ---------------------------------------------------------------- svg helpers


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def n(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def mix(a: str, b: str, t: float) -> str:
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def rr(x: float, y: float, w: float, h: float, r: float) -> str:
    """Rounded-rectangle path."""
    r = min(r, w / 2, h / 2)
    return (f"M{n(x + r)} {n(y)}H{n(x + w - r)}a{n(r)} {n(r)} 0 0 1 {n(r)} {n(r)}V{n(y + h - r)}"
            f"a{n(r)} {n(r)} 0 0 1 {n(-r)} {n(r)}H{n(x + r)}a{n(r)} {n(r)} 0 0 1 {n(-r)} {n(-r)}"
            f"V{n(y + r)}a{n(r)} {n(r)} 0 0 1 {n(r)} {n(-r)}Z")


class Rand:
    """Tiny deterministic PRNG so the illustrations are stable between runs."""

    def __init__(self, seed: int):
        self.s = seed & 0xFFFFFFFF or 1

    def __call__(self) -> float:
        self.s ^= (self.s << 13) & 0xFFFFFFFF
        self.s ^= self.s >> 17
        self.s ^= (self.s << 5) & 0xFFFFFFFF
        return (self.s & 0xFFFFFF) / 0x1000000


class Card:
    def __init__(self, name: str, w: float, h: float, theme: str, label: str, frame: bool = True):
        self.name, self.w, self.h, self.theme, self.label = name, w, h, theme, label
        self.c = THEMES[theme]
        self.frame = frame
        self.parts: list[str] = []
        self.chars: dict[str, set[str]] = {"sans": set(), "mono": set()}
        self.defs: list[str] = []
        self.keyframes: dict[str, str] = {}
        self.uid = 0

    def col(self, key: str) -> str:
        return self.c.get(key, key) if isinstance(self.c.get(key, key), str) else key

    def id(self, prefix: str) -> str:
        self.uid += 1
        return f"{prefix}{self.uid}"

    # -- animation attributes ------------------------------------------
    def anim(self, kind: str | None, delay: float = 0.0, extra_cls: str = "", dur: float | None = None) -> str:
        cls = " ".join(x for x in (extra_cls, kind) if x)
        out = f' class="{cls}"' if cls else ""
        if kind and not STATIC:
            st = f"animation-delay:{n(delay)}s"
            if dur:
                st += f";animation-duration:{n(dur)}s"
            out += f' style="{st}"'
        return out

    def keyframe(self, name: str, body: str) -> str:
        self.keyframes[name] = body
        return name

    def loop(self, name: str, dur: float, delay: float = 0.0, timing: str = "linear",
             direction: str = "normal", origin: str | None = None) -> str:
        """Style attribute for an ambient infinite animation."""
        if STATIC:
            return ""
        o = f"transform-box:fill-box;transform-origin:{origin};" if origin else ""
        return (f' style="{o}animation:{name} {n(dur)}s {timing} {n(delay)}s infinite {direction} both"')

    # -- primitives ----------------------------------------------------
    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, s, style, fill="ink", anchor="start", kind=None, delay=0.0, size=None,
             extra="", opacity=None):
        self.chars[STYLES[style][0]].update(s)
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        fs = f' font-size="{n(size or STYLES[style][1])}"'
        op = f' fill-opacity="{n(opacity)}"' if opacity is not None else ""
        self.add(f'<text x="{n(x)}" y="{n(y)}" fill="{self.col(fill)}"{a}{fs}{op}{extra}'
                 f'{self.anim(kind, delay, "t-" + style)}>{esc(s)}</text>')

    def rule(self, x1, y, x2, color="rule", width=1, kind="gx", delay=0.0, dash=None, opacity=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        o = f' stroke-opacity="{n(opacity)}"' if opacity is not None else ""
        self.add(f'<path d="M{n(x1)} {n(y)}H{n(x2)}" stroke="{self.col(color)}" '
                 f'stroke-width="{n(width)}"{d}{o}{self.anim(kind, delay)}/>')

    def icon(self, x, y, size, body, color="ink", kind="fade", delay=0.0, width=1.75):
        s = size / 24
        self.add(f'<g transform="translate({n(x)} {n(y)}) scale({n(s)})"><g fill="none" '
                 f'stroke="{self.col(color)}" stroke-width="{n(width)}" stroke-linecap="round" '
                 f'stroke-linejoin="round"{self.anim(kind, delay)}>{body}</g></g>')

    def arrow(self, x, y, size, delay, color="ink2"):
        body = f'<path d="{ARROW}" pathLength="1"{self.anim("draw", delay + 0.1)}/>'
        self.icon(x, y, size, body, color=color, kind=None)

    def lingrad(self, stops: list[tuple[float, str, float]], x1=0, y1=0, x2=1, y2=0,
                units: str = "objectBoundingBox", extra: str = "", spread: str = "") -> str:
        gid = self.id("lg")
        st = "".join(f'<stop offset="{n(o)}" stop-color="{self.col(c)}"'
                     + (f' stop-opacity="{n(a)}"' if a != 1 else "") + "/>" for o, c, a in stops)
        u = f' gradientUnits="{units}"' if units != "objectBoundingBox" else ""
        sp = f' spreadMethod="{spread}"' if spread else ""
        self.defs.append(f'<linearGradient id="{gid}" x1="{n(x1)}" y1="{n(y1)}" x2="{n(x2)}" '
                         f'y2="{n(y2)}"{u}{sp}>{st}{extra}</linearGradient>')
        return f"url(#{gid})"

    def radgrad(self, color: str, alpha: float = 1.0, mid: float = 0.35) -> str:
        gid = self.id("rg")
        c = self.col(color)
        self.defs.append(f'<radialGradient id="{gid}"><stop offset="0" stop-color="{c}" stop-opacity="{n(alpha)}"/>'
                         f'<stop offset="{n(mid)}" stop-color="{c}" stop-opacity="{n(alpha * 0.45)}"/>'
                         f'<stop offset="1" stop-color="{c}" stop-opacity="0"/></radialGradient>')
        return f"url(#{gid})"

    def glow_filter(self, std: float = 3.0) -> str:
        fid = self.id("gf")
        self.defs.append(f'<filter id="{fid}" x="-50%" y="-50%" width="200%" height="200%">'
                         f'<feGaussianBlur stdDeviation="{n(std)}" result="b"/>'
                         f'<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>')
        return f"url(#{fid})"

    def chip(self, x, y, label, hue, delay, h=26, style="mono", size=12, dot=True) -> float:
        w = measure(label, style, size) + (34 if dot else 22)
        c = self.col(hue)
        self.add(f'<g{self.anim("up", delay)}>'
                 f'<path d="{rr(x, y, w, h, h / 2)}" fill="{c}" fill-opacity=".09" stroke="{c}" '
                 f'stroke-opacity=".38"/>')
        if dot:
            self.add(f'<circle cx="{n(x + 13)}" cy="{n(y + h / 2)}" r="3.2" fill="{c}"/>')
        self.add("</g>")
        self.text(x + (23 if dot else 11), y + h / 2 + 4.2, label, style, size=size, kind="up", delay=delay)
        return w

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
            # font-size stays a presentation attribute on each <text> (a class
            # rule would override it), so per-use sizes take effect.
            r = f".t-{name}{{font-family:{FONT_STACK[face]};font-weight:{weight}"
            if face == "sans" and width != 100:
                r += f";font-stretch:{width}%"
            if track:
                r += f";letter-spacing:{n(track)}em"
            rules.append(r + "}")
        if STATIC:
            rules.append("*{animation:none!important}.smil{display:none}")
            return "".join(rules)
        rules += [
            f".up{{animation:up .8s {EASE} both}}",
            f".fade{{animation:fade .7s {EASE} both}}",
            f".rise{{animation:rise 1.1s {EASE} both}}",
            f".gx{{transform-box:fill-box;transform-origin:0 50%;animation:gx .9s {EASE} both}}",
            f".gy{{transform-box:fill-box;transform-origin:50% 100%;animation:gy .9s {EASE} both}}",
            f".pop{{transform-box:fill-box;transform-origin:50% 50%;animation:pop .6s {EASE} both}}",
            f".draw{{stroke-dasharray:1;animation:draw 1s {EASE} both}}",
            f".blur{{animation:blur 1s {EASE} both}}",
            f".type{{animation:type 1.6s steps(42,end) both}}",
            "@keyframes up{from{opacity:0;transform:translateY(10px)}}",
            "@keyframes fade{from{opacity:0}}",
            "@keyframes rise{from{opacity:0;transform:translateY(60px)}}",
            "@keyframes gx{from{transform:scaleX(0)}}",
            "@keyframes gy{from{transform:scaleY(0)}}",
            "@keyframes pop{from{opacity:0;transform:scale(.3)}}",
            "@keyframes draw{from{stroke-dashoffset:1}}",
            "@keyframes blur{from{opacity:0;filter:blur(12px);transform:scale(1.06)}}",
            "@keyframes type{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}",
            "@keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}",
            "@keyframes spin{to{transform:rotate(360deg)}}",
            "@keyframes spinr{to{transform:rotate(-360deg)}}",
            "@keyframes breathe{0%,100%{opacity:.55;transform:scale(.92)}50%{opacity:1;transform:scale(1.08)}}",
            "@keyframes ping{0%{opacity:.9;transform:scale(.2)}80%,100%{opacity:0;transform:scale(1)}}",
            "@keyframes flicker{0%,100%{opacity:1}50%{opacity:.35}}",
        ]
        rules += [f"@keyframes {k}{{{v}}}" for k, v in self.keyframes.items()]
        rules.append("@media (prefers-reduced-motion:reduce){*{animation:none!important}.smil{display:none}}")
        return "".join(rules)

    def svg(self) -> str:
        c = self.c
        frame = ""
        if self.frame:
            edge = self.lingrad([(0, "rule2", 1), (0.5, "rule", 1), (1, "rule2", 1)], 0, 0, 1, 1)
            frame = (f'<rect x=".5" y=".5" width="{n(self.w - 1)}" height="{n(self.h - 1)}" rx="16" '
                     f'fill="{c["ground"]}" stroke="{edge}"/>')
        body = "".join(self.parts)
        if self.frame:
            self.defs.append(f'<clipPath id="cardclip"><rect width="{n(self.w)}" height="{n(self.h)}" rx="16"/></clipPath>')
            body = f'<g clip-path="url(#cardclip)">{body}</g>'
        defs = f'<defs>{"".join(self.defs)}</defs>' if self.defs else ""
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n(self.w)} {n(self.h)}" '
                f'width="{n(self.w)}" height="{n(self.h)}" role="img" aria-label="{esc(self.label)}">'
                f'<title>{esc(self.label)}</title><style>{self.css()}</style>{defs}{frame}'
                f'{body}</svg>\n')

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
    return {"days": days, "repos": repos, "login": user.get("login", PROFILE["handle"])}


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
    last30 = sum(c for _, c in days[-30:])
    return {
        "total": sum(c for _, c in days),
        "active": sum(1 for _, c in days if c),
        "span": len(days),
        "current": current,
        "longest": longest,
        "busiest": busiest,
        "last30": last30,
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


def plural(v: int, word: str) -> str:
    return f"{v} {word}" + ("" if v == 1 else "s")


def day_label(d: dt.date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


# ---------------------------------------------------------------- shared decoration


def aurora(card: Card, blobs: list[tuple[float, float, float, str, float]], t0: float = 0.0) -> None:
    """Soft drifting colour fields made of radial gradients (no blur filter)."""
    g = card.c["glow"]
    for i, (x, y, r, hue, a) in enumerate(blobs):
        name = card.keyframe(f"drift{i}", f"50%{{transform:translate({n(r * .22 * (1 if i % 2 else -1))}px,"
                                          f"{n(r * .14 * (-1 if i % 3 else 1))}px) scale({1.12 if i % 2 else .9})}}")
        card.add(f'<g{card.anim("fade", t0 + i * .1)}><ellipse cx="{n(x)}" cy="{n(y)}" rx="{n(r)}" ry="{n(r * .72)}" '
                 f'fill="{card.radgrad(hue, a * g)}"{card.loop(name, 9 + i * 2.3, -i * 1.7, "ease-in-out", "alternate", "50% 50%")}/></g>')


def dot_grid(card: Card, x0, y0, x1, y1, step=22, opacity=.16, delay=0.0) -> None:
    pid = card.id("dots")
    card.defs.append(f'<pattern id="{pid}" width="{step}" height="{step}" patternUnits="userSpaceOnUse">'
                     f'<circle cx="{step / 2}" cy="{step / 2}" r=".9" fill="{card.c["ink"]}"/></pattern>')
    card.add(f'<rect x="{n(x0)}" y="{n(y0)}" width="{n(x1 - x0)}" height="{n(y1 - y0)}" fill="url(#{pid})" '
             f'opacity="{n(opacity)}"{card.anim("fade", delay)}/>')


def corner_marks(card: Card, x, y, w, h, color="rule2", L=10, delay=0.0) -> None:
    d = (f"M{n(x)} {n(y + L)}V{n(y)}H{n(x + L)}M{n(x + w - L)} {n(y)}H{n(x + w)}V{n(y + L)}"
         f"M{n(x + w)} {n(y + h - L)}V{n(y + h)}H{n(x + w - L)}M{n(x + L)} {n(y + h)}H{n(x)}V{n(y + h - L)}")
    card.add(f'<path d="{d}" fill="none" stroke="{card.col(color)}" stroke-width="1.5"{card.anim("fade", delay)}/>')


def odometer(card: Card, x: float, y: float, value: str, style: str, fill: str, delay: float,
             size: float | None = None) -> float:
    """Digits that roll into place like a mechanical counter. Returns the width."""
    face, base, *_ = STYLES[style]
    size = size or base
    lh = size * 1.1
    cw = measure("0", style, size)
    cid = card.id("od")
    card.defs.append(f'<clipPath id="{cid}"><rect x="{n(x - 2)}" y="{n(y - size * .86)}" '
                     f'width="{n(cw * len(value) + 6)}" height="{n(size * 1.12)}"/></clipPath>')
    card.add(f'<g clip-path="url(#{cid})">')
    for i, ch in enumerate(value):
        cx = x + i * cw
        if not ch.isdigit():
            card.text(cx, y, ch, style, fill=fill, size=size, kind="fade", delay=delay)
            continue
        d = int(ch)
        spins = 10 + d          # one full turn, then land on the digit
        seq = [str(k % 10) for k in range(spins + 1)]
        offset = spins * lh
        name = card.keyframe(f"od{int(offset * 10)}", f"from{{transform:translateY({n(offset)}px)}}")
        dur = 1.4 + i * .25
        st = "" if STATIC else (f' style="animation:{name} {n(dur)}s cubic-bezier(.16,1,.3,1) '
                                f'{n(delay + i * .05)}s both"')
        card.chars["mono" if face == "mono" else "sans"].update(seq)
        texts = "".join(f'<text x="{n(cx)}" y="{n(y + (k - spins) * lh)}" class="t-{style}" '
                        f'font-size="{n(size)}" fill="{card.col(fill)}">{s}</text>'
                        for k, s in enumerate(seq))
        card.add(f"<g{st}>{texts}</g>")
    card.add("</g>")
    return cw * len(value)


# ---------------------------------------------------------------- hero


def hero(theme: str, data: dict, st: dict, t0: float) -> Card:
    h = 430
    card = Card("hero", W, h, theme, f"{PROFILE['name']} (@{data['login']}). {PROFILE['line']} {PROFILE['role']}.")
    c = card.c
    dark = theme == "dark"

    # -- atmosphere ---------------------------------------------------------
    aurora(card, [(150, 40, 360, "violet", .55), (620, 70, 330, "rose", .42),
                  (760, 330, 300, "amber", .38), (330, 380, 300, "cyan", .30)], t0)
    dot_grid(card, 0, 0, W, 300, 24, .14 if dark else .2, t0)

    # Synthwave floor: converging rails and rungs that run toward the viewer.
    hz, fl = 318, h
    floor_g = card.lingrad([(0, "violet", 0), (1, "rose", .9 if dark else .6)], 0, 0, 0, 1)
    card.defs.append(f'<clipPath id="floor"><rect x="0" y="{hz}" width="{W}" height="{fl - hz}"/></clipPath>')
    card.add(f'<g clip-path="url(#floor)"{card.anim("fade", t0 + .2)}>')
    card.add(f'<rect x="0" y="{hz}" width="{W}" height="{fl - hz}" fill="{card.lingrad([(0, "violet", .0), (1, "violet", .22 if dark else .1)], 0, 0, 0, 1)}"/>')
    vx = W / 2
    rails = "".join(f"M{n(vx + k * 18)} {hz}L{n(vx + k * 150)} {fl}" for k in range(-14, 15))
    card.add(f'<path d="{rails}" stroke="{floor_g}" stroke-width="1" fill="none"/>')
    rung_n = 7
    for i in range(rung_n):
        f = (i + 1) / rung_n
        ys = hz + (fl - hz) * f ** 2.2
        name = card.keyframe(f"rung{i}", f"0%{{transform:translateY({n(hz - ys)}px);opacity:0}}"
                                          f"15%{{opacity:1}}100%{{transform:translateY({n(fl - ys)}px);opacity:1}}")
        card.add(f'<path d="M0 {n(ys)}H{W}" stroke="{c["rose"]}" stroke-opacity="{n(.25 + .5 * f)}" '
                 f'stroke-width="{n(.6 + f * 1.1)}"{card.loop(name, 3.2, -3.2 * (1 - f) ** .45, "cubic-bezier(.55,0,1,.45)")}/>')
    card.add("</g>")
    # Horizon glow
    card.add(f'<rect x="0" y="{hz - 1}" width="{W}" height="2" fill="{card.lingrad([(0, "violet", 0), (.3, "rose", 1), (.5, "amber", 1), (.7, "rose", 1), (1, "violet", 0)])}"'
             f'{card.anim("gx", t0 + .3)}/>')
    card.add(f'<ellipse cx="{vx}" cy="{hz}" rx="380" ry="26" fill="{card.radgrad("rose", .5 * c["glow"])}"{card.anim("fade", t0 + .4)}/>')

    # -- top HUD bar -------------------------------------------------------
    card.text(PAD, 40, f"{data['login'].upper()}://PROFILE", "monob", fill="ink2", kind="fade", delay=t0 + .1, size=11.5)
    last_day = data["days"][-1][0]
    rev = f"REV {last_day:%Y.%m.%d}"
    card.text(W - PAD, 40, rev, "monob", fill="ink2", anchor="end", kind="fade", delay=t0 + .1, size=11.5)
    sx = W - PAD - measure(rev, "monob", 11.5) - 104
    card.add(f'<g{card.anim("fade", t0 + .2)}><path d="{rr(sx, 26, 90, 20, 10)}" fill="{c["lime"]}" fill-opacity=".1" '
             f'stroke="{c["lime"]}" stroke-opacity=".45"/>'
             f'<circle cx="{n(sx + 12)}" cy="36" r="6" fill="{c["lime"]}" fill-opacity=".35"{card.loop("ping", 1.8, 0, "ease-out", origin="50% 50%")}/>'
             f'<circle cx="{n(sx + 12)}" cy="36" r="3" fill="{c["lime"]}"/></g>')
    card.text(sx + 22, 40, "SHIPPING", "monob", fill="lime", kind="fade", delay=t0 + .2, size=10.5)
    card.rule(PAD, 56, W - PAD, "rule2", delay=t0 + .1, opacity=.8)

    # -- right: neural constellation in an orbit ----------------------------
    ox, oy, orad = 668, 186, 118
    orbit = card.lingrad([(0, "amber", 1), (.5, "rose", 1), (1, "violet", 1)], 0, 0, 1, 1)
    card.add(f'<g{card.anim("blur", t0 + .35)}>')
    card.add(f'<circle cx="{ox}" cy="{oy}" r="{orad + 34}" fill="{card.radgrad("violet", .22 * c["glow"] + .05, .5)}"/>')
    card.add(f'<g{card.loop("spin", 40, 0, origin="50% 50%")}><circle cx="{ox}" cy="{oy}" r="{orad}" fill="none" stroke="{orbit}" '
             f'stroke-width="1.5" stroke-dasharray="2 7" stroke-linecap="round"/></g>')
    card.add(f'<g{card.loop("spinr", 26, 0, origin="50% 50%")}><circle cx="{ox}" cy="{oy}" r="{orad - 14}" fill="none" '
             f'stroke="{c["ink"]}" stroke-opacity=".18" stroke-dasharray="60 14 4 14"/>'
             f'<circle cx="{ox + orad - 14}" cy="{oy}" r="3.5" fill="{c["amber"]}"/></g>')
    card.add(f'<g{card.loop("spin", 14, 0, origin="50% 50%")}><circle cx="{ox}" cy="{oy}" r="{orad + 16}" fill="none" '
             f'stroke="{c["rose"]}" stroke-opacity=".0"/><circle cx="{ox}" cy="{n(oy - orad - 16)}" r="2.5" fill="{c["rose"]}"/></g>')
    # tick ring
    ticks = "".join(
        f"M{n(ox + math.cos(a) * (orad + 26))} {n(oy + math.sin(a) * (orad + 26))}"
        f"L{n(ox + math.cos(a) * (orad + (32 if k % 6 == 0 else 29)))} {n(oy + math.sin(a) * (orad + (32 if k % 6 == 0 else 29)))}"
        for k in range(72) for a in [k / 72 * 2 * math.pi])
    card.add(f'<path d="{ticks}" stroke="{c["ink"]}" stroke-opacity=".28"/>')
    card.add("</g>")

    portrait = next((p for p in (ROOT / "assets" / f"portrait.{e}" for e in ("jpg", "jpeg", "png", "webp"))
                     if p.exists()), None)
    layers = [3, 5, 5, 2]
    lx = [ox - 72, ox - 24, ox + 24, ox + 72]
    nodes = [[(lx[li], oy + (k - (cnt - 1) / 2) * 30) for k in range(cnt)] for li, cnt in enumerate(layers)]
    rnd = Rand(7)
    if portrait:
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}[portrait.suffix[1:]]
        uri = f"data:image/{mime};base64," + base64.b64encode(portrait.read_bytes()).decode()
        pr = orad - 26
        card.defs.append(f'<clipPath id="pp"><circle cx="{ox}" cy="{oy}" r="{pr}"/></clipPath>')
        card.add(f'<g{card.anim("blur", t0 + .5)}><image href="{uri}" x="{ox - pr}" y="{oy - pr}" width="{2 * pr}" '
                 f'height="{2 * pr}" preserveAspectRatio="xMidYMid slice" clip-path="url(#pp)"/>'
                 f'<circle cx="{ox}" cy="{oy}" r="{pr}" fill="none" stroke="{orbit}" stroke-width="2"/></g>')
    else:
        edges = []
        for li in range(len(layers) - 1):
            for a in nodes[li]:
                for b in nodes[li + 1]:
                    edges.append((a, b))
        ed = "".join(f"M{n(a[0])} {n(a[1])}L{n(b[0])} {n(b[1])}" for a, b in edges)
        card.add(f'<path d="{ed}" stroke="{c["ink"]}" stroke-opacity=".13" stroke-width="1"{card.anim("fade", t0 + .6)}/>')
        # Pulses: signals that run input -> output along a random path.
        if not STATIC:
            for p in range(7):
                path = [nodes[0][int(rnd() * layers[0])]]
                for li in range(1, len(layers)):
                    path.append(nodes[li][int(rnd() * layers[li])])
                d = "M" + "L".join(f"{n(px)} {n(py)}" for px, py in path)
                hue = ["amber", "rose", "cyan", "violet"][p % 4]
                dur = 2.4 + rnd() * 1.4
                card.add(f'<path class="smil" d="{d}" stroke="{c[hue]}" stroke-width="1.6" fill="none" stroke-opacity="0" '
                         f'pathLength="1" stroke-dasharray=".14 .86">'
                         f'<animate attributeName="stroke-dashoffset" values=".14;-.86" dur="{n(dur)}s" '
                         f'begin="{n(t0 + 1 + p * .37)}s" repeatCount="indefinite"/>'
                         f'<set attributeName="stroke-opacity" to=".95" begin="{n(t0 + 1 + p * .37)}s"/></path>')
        for li, layer in enumerate(nodes):
            for k, (px, py) in enumerate(layer):
                hue = ["cyan", "violet", "rose", "amber"][li]
                dl = t0 + .7 + li * .12 + k * .04
                card.add(f'<g{card.anim("pop", dl)}><circle cx="{n(px)}" cy="{n(py)}" r="9" fill="{c[hue]}" fill-opacity=".14"'
                         f'{card.loop("breathe", 2.6 + (li + k) % 3 * .6, -(li * 0.7 + k * .45), "ease-in-out", origin="50% 50%")}/>'
                         f'<circle cx="{n(px)}" cy="{n(py)}" r="4.2" fill="{c["ground"]}" stroke="{c[hue]}" stroke-width="1.8"/></g>')
        card.text(ox - 72, oy + 104, "x", "mono", fill="ink2", anchor="middle", kind="fade", delay=t0 + 1, size=11)
        card.text(ox + 72, oy + 104, "y", "mono", fill="ink2", anchor="middle", kind="fade", delay=t0 + 1, size=11)

    # -- left: identity ----------------------------------------------------
    text_w = ox - orad - 40 - PAD
    size = STYLES["display"][1]
    size = min(size, size * text_w / measure(PROFILE["name"], "display"))
    name_g = card.lingrad([(0, "ink", 1), (.42, "ink", 1), (.5, "amber", 1), (.58, "rose", 1), (.66, "ink", 1), (1, "ink", 1)],
                          0, 0, W, 0, units="userSpaceOnUse", spread="pad",
                          extra="" if STATIC else
                          f'<animateTransform attributeName="gradientTransform" type="translate" values="-{W} 0;{W} 0" '
                          f'dur="5s" begin="{n(t0 + 1.4)}s" repeatCount="indefinite" calcMode="spline" keyTimes="0;1" '
                          f'keySplines=".6 0 .3 1"/>')
    card.text(PAD, 92, "HELLO, WORLD — I’M", "label", fill="amber", kind="up", delay=t0 + .15, size=12)
    card.defs.append(f'<clipPath id="nm"><rect x="0" y="96" width="{n(ox - orad - 30)}" height="{n(size * 1.08)}"/></clipPath>')
    card.add('<g clip-path="url(#nm)">')
    # Offset outline copy for depth, then the gradient-swept fill.
    card.chars["sans"].update(PROFILE["name"])
    card.add(f'<text x="{PAD + 3}" y="{n(96 + size * .86 + 3)}" font-size="{n(size)}" fill="none" '
             f'stroke="{c["rose"]}" stroke-opacity=".55" stroke-width="1"{card.anim("rise", t0 + .28, "t-display")}>{esc(PROFILE["name"])}</text>')
    card.add(f'<text x="{PAD}" y="{n(96 + size * .86)}" class="t-display{"" if STATIC else " rise"}" font-size="{n(size)}" '
             f'fill="{name_g}"{"" if STATIC else f" style=\"animation-delay:{n(t0 + .2)}s\""}>{esc(PROFILE["name"])}</text>')
    card.add("</g>")

    # Terminal line with a typing reveal and a blinking block cursor.
    ty = 96 + size * 1.08 + 38
    prompt = "~ $"
    card.text(PAD, ty, prompt, "monob", fill="lime", kind="fade", delay=t0 + .7, size=14)
    px0 = PAD + measure(prompt + " ", "monob", 14)
    line = PROFILE["line"]
    lw = measure(line, "state")
    card.chars["sans"].update(line)
    type_st = "" if STATIC else f' style="animation-delay:{n(t0 + .9)}s;animation-timing-function:steps({len(line)},end)"'
    card.add(f'<text x="{n(px0)}" y="{n(ty)}" class="t-state{"" if STATIC else " type"}" font-size="{STYLES['state'][1]}" fill="{c["ink"]}"{type_st}>{esc(line)}</text>')
    cur_kf = card.keyframe("cur", f"from{{transform:translateX({n(-lw)}px)}}")
    cur_st = "" if STATIC else (f' style="animation:{cur_kf} 1.6s steps({len(line)},end) {n(t0 + .9)}s both"')
    card.add(f'<g{cur_st}><rect x="{n(px0 + lw + 4)}" y="{n(ty - 16)}" width="10" height="20" fill="{c["amber"]}"'
             f'{card.loop("blink", 1.05, t0 + 2.5, "step-end")}/></g>')

    card.text(PAD, ty + 32, PROFILE["role"], "small", fill="ink2", kind="up", delay=t0 + 1.0, size=13.5)

    # Tag chips
    cx = PAD
    for i, tag in enumerate(PROFILE["tags"]):
        hue = ["amber", "rose", "violet", "cyan"][i % 4]
        cx += card.chip(cx, ty + 50, tag, hue, t0 + 1.15 + i * .08, h=26, size=11.5) + 8

    # Bottom-left HUD readout on the floor.
    hud = [("CONTRIB·1Y", str(st["total"])), ("STREAK", f"{st['current']}d"), ("REPOS", str(len(data["repos"])))]
    hx = PAD
    for i, (k, v) in enumerate(hud):
        card.text(hx, h - 40, k, "label", fill="ink2", kind="up", delay=t0 + 1.4 + i * .08, size=10)
        card.text(hx, h - 18, v, "monob", fill="ink", kind="up", delay=t0 + 1.45 + i * .08, size=17)
        hx += max(measure(k, "label", 10), measure(v, "monob", 17)) + 30
    card.text(W - PAD, h - 18, "build · measure · explain", "mono", fill="ink2", anchor="end",
              kind="fade", delay=t0 + 1.6, size=12)
    return card


# ---------------------------------------------------------------- tiles


def tile(theme: str, spec: dict, t0: float) -> Card:
    h = 116
    card = Card(f"tile-{spec['id']}", TILE_W, h, theme, f"{spec['label']}: {spec['sub']}")
    c = card.c
    hue = spec["hue"]
    aurora(card, [(30, 10, 150, hue, .45)], t0)
    card.add(f'<path d="{rr(20, 20, 40, 40, 12)}" fill="{c[hue]}" fill-opacity=".14" stroke="{c[hue]}" stroke-opacity=".5"'
             f'{card.anim("pop", t0)}/>')
    card.icon(29, 29, 22, ICONS[spec["icon"]], color=hue, delay=t0 + .1)
    card.text(72, 36, spec["kicker"], "label", fill=hue, kind="up", delay=t0 + .05, size=10)
    card.text(72, 56, spec["label"], "title", kind="up", delay=t0 + .1)
    card.arrow(TILE_W - 22 - 16, 22, 16, t0 + .2, color=hue)
    sub_size = min(12.5, 12.5 * (TILE_W - 40) / measure(spec["sub"], "mono", 12.5))
    card.text(20, 90, spec["sub"], "mono", fill="ink2", kind="up", delay=t0 + .18, size=sub_size)
    bar = card.lingrad([(0, hue, 1), (1, hue, 0)])
    card.add(f'<rect x="0" y="{h - 3}" width="{TILE_W}" height="3" fill="{bar}"{card.anim("gx", t0 + .25)}/>')
    # A glint that sweeps the bottom bar now and then.
    card.add(f'<rect x="-60" y="{h - 3}" width="60" height="3" fill="{card.lingrad([(0, "ink", 0), (.5, "ink", .9), (1, "ink", 0)])}"'
             f'{card.loop(card.keyframe("glint", f"0%,70%{{transform:translateX(0)}}100%{{transform:translateX({TILE_W + 60}px)}}"), 4.5, t0 + 1 + TILE_W / 900)}/>')
    return card


# ---------------------------------------------------------------- section headers


def section(theme: str, sid: str, num: str, title: str, note: str, t0: float) -> Card:
    h = 70
    card = Card(f"section-{sid}", W, h, theme, f"{num} — {title}", frame=False)
    c = card.c
    hue = HUES[int(num) % len(HUES)]
    card.text(4, 46, num, "monob", fill=hue, kind="up", delay=t0, size=13)
    card.add(f'<path d="M30 41.5h14" stroke="{c[hue]}" stroke-width="2"{card.anim("gx", t0 + .05)}/>')
    card.text(54, 48, title.upper(), "head", kind="up", delay=t0 + .08, size=24)
    tw = measure(title.upper(), "head", 24)
    nx = W - 4 - measure(note, "mono", 12)
    lx0 = 54 + tw + 22
    card.add(f'<path d="M{n(lx0)} 41.5H{n(nx - 14)}" stroke="{card.lingrad([(0, hue, 1), (.6, "rule2", 1), (1, "rule2", 0)], lx0, 0, nx - 14, 0, units="userSpaceOnUse")}" '
             f'stroke-width="1.5"{card.anim("gx", t0 + .15)}/>')
    card.add(f'<circle cx="{n(lx0 + 3)}" cy="41.5" r="3" fill="{c[hue]}"{card.anim("pop", t0 + .1)}/>')
    if not STATIC:
        card.add(f'<circle class="smil" cx="0" cy="41.5" r="2" fill="{c[hue]}"><animate attributeName="cx" '
                 f'values="{n(lx0)};{n(nx - 14)}" dur="3.6s" begin="{n(t0 + 1)}s" repeatCount="indefinite"/>'
                 f'<animate attributeName="opacity" values="0;1;1;0" keyTimes="0;.1;.8;1" dur="3.6s" '
                 f'begin="{n(t0 + 1)}s" repeatCount="indefinite"/></circle>')
    card.text(W - 4, 46, note, "mono", fill="ink2", anchor="end", kind="fade", delay=t0 + .2, size=12)
    return card


# ---------------------------------------------------------------- toolkit marquee


def toolkit(theme: str, t0: float) -> Card:
    h = 132
    card = Card("toolkit", W, h, theme, "Toolkit: " + ", ".join(sum(TOOLKIT, [])) + ".")
    c = card.c
    aurora(card, [(80, 66, 260, "cyan", .25), (760, 66, 260, "rose", .25)], t0)
    # Luminance mask that fades the rows out at both edges.
    wid = card.id("mk")
    card.defs.append(f'<linearGradient id="{wid}"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
                     f'<stop offset=".09" stop-color="#fff"/><stop offset=".91" stop-color="#fff"/>'
                     f'<stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>')
    card.defs.append(f'<mask id="edge"><rect width="{W}" height="{h}" fill="url(#{wid})"/></mask>')
    card.add('<g mask="url(#edge)">')
    for r, row in enumerate(TOOLKIT):
        y = 24 + r * 46
        # Lay the row out twice so the loop is seamless.
        items = []
        x = 0.0
        for k, name in enumerate(row):
            w = measure(name, "monob", 13) + 40
            items.append((x, w, name, HUES[(k + r * 3) % len(HUES)]))
            x += w + 12
        period = x
        reps = math.ceil((W + period) / period) + 1
        parts = []
        for rep in range(reps):
            for ix, w, name, hue in items:
                X = ix + rep * period
                card.chars["mono"].update(name)
                parts.append(f'<path d="{rr(X, y, w, 34, 17)}" fill="{c[hue]}" fill-opacity=".08" stroke="{c[hue]}" '
                             f'stroke-opacity=".35"/><circle cx="{n(X + 16)}" cy="{n(y + 17)}" r="4" fill="{c[hue]}"/>'
                             f'<text x="{n(X + 28)}" y="{n(y + 21.5)}" class="t-monob" font-size="13" fill="{c["ink"]}">{esc(name)}</text>')
        start = PAD if r == 0 else PAD - period / 2
        kf = card.keyframe(f"mq{r}", f"from{{transform:translateX(0)}}to{{transform:translateX({n(-period if r == 0 else period)}px)}}")
        anim = card.loop(kf, period / 28, 0)
        card.add(f'<g transform="translate({n(start - (0 if r == 0 else period))} 0)"><g{card.anim("fade", t0 + r * .15)}>'
                 f'<g{anim}>{"".join(parts)}</g></g></g>')
    card.add("</g>")
    return card


# ---------------------------------------------------------------- activity


def activity(theme: str, data: dict, st: dict, t0: float) -> Card:
    months = monthly(data["days"])
    busy_day, busy_n = st["busiest"]
    h = 420
    card = Card("activity", W, h, theme,
                f"Activity over the last 12 months: {st['total']} contributions. "
                f"Current streak {plural(st['current'], 'day')}, longest {plural(st['longest'], 'day')}, "
                f"active on {st['active']} of {st['span']} days, {st['last30']} in the last 30 days, "
                f"busiest day {day_label(busy_day)} with {busy_n}.")
    c = card.c
    aurora(card, [(120, 90, 300, "amber", .28), (700, 200, 320, "violet", .26)], t0)

    # -- stat blocks, 2 x 2, with rolling digits -----------------------------
    blocks = [
        ("CONTRIBUTIONS", str(st["total"]), "last 12 months", "amber"),
        ("LAST 30 DAYS", str(st["last30"]), f"{round(100 * st['last30'] / max(st['total'], 1))}% of the year", "rose"),
        ("CURRENT STREAK", str(st["current"]), f"longest {plural(st['longest'], 'day')}", "lime"),
        ("ACTIVE DAYS", str(st["active"]), f"of {st['span']} · peak {busy_n} on {busy_day.day} {MONTHS[busy_day.month - 1]}", "cyan"),
    ]
    bw, bh = 170, 116
    for i, (k, v, sub, hue) in enumerate(blocks):
        bx = PAD + (i % 2) * (bw + 12)
        by = 30 + (i // 2) * (bh + 12)
        dl = t0 + .1 + i * .1
        card.add(f'<g{card.anim("up", dl)}><path d="{rr(bx, by, bw, bh, 12)}" fill="{c["sheet"]}" fill-opacity=".75" '
                 f'stroke="{c["rule2"]}"/><path d="M{bx + 14} {by}h40" stroke="{c[hue]}" stroke-width="2.5"/></g>')
        card.text(bx + 14, by + 26, k, "label", fill=hue, kind="up", delay=dl + .05, size=10)
        wv = odometer(card, bx + 14, by + 74, v, "num", "ink", dl + .15, size=40)
        if hue == "lime" and st["current"]:
            # A small flame for a live streak.
            fx = bx + 14 + wv + 12
            card.add(f'<g{card.anim("pop", dl + 1.2)}><path d="M{fx} {by + 72}c-6 0-9-4-9-8 0-5 4-7 5-12 2 3 3 4 3 7 1-2 2-3 2-5 '
                     f'4 3 8 7 8 11 0 4-3 7-9 7Z" fill="{c["ember"]}" fill-opacity=".9"'
                     f'{card.loop("flicker", 1.3, 0, "ease-in-out")}/></g>')
        subs = wrap(sub, "small", bw - 28, 1, 11.5)
        card.text(bx + 14, by + 98, subs[0], "small", fill="ink2", kind="up", delay=dl + .2, size=11.5)

    # -- monthly bars ----------------------------------------------------
    x0, x1, top, base = PAD + 2 * bw + 12 + 46, W - PAD, 66, 258
    card.text(x0 - 12, 40, "PER MONTH", "label", fill="ink2", kind="up", delay=t0 + .2, size=10)
    peak = max(t for *_, t in months) or 1
    step = next(s for s in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000) if s * 4 >= peak)
    top_v = -(-peak // step) * step
    scale = (base - top) / top_v
    for v in range(0, top_v + 1, step):
        y = base - v * scale
        card.rule(x0, y, x1, "rule2" if v == 0 else "rule", delay=t0 + .2, dash=None if v == 0 else "2 4")
        card.text(x0 - 8, y + 4, str(v), "mono", fill="ink2", anchor="end", kind="fade", delay=t0 + .25, size=10.5)
    slot = (x1 - x0) / len(months)
    bwid = slot * .56
    fill = card.lingrad([(0, "amber", 1), (.55, "rose", 1), (1, "violet", 1)], 0, top, 0, base, units="userSpaceOnUse")
    gf = card.glow_filter(5)
    peak_i = max(range(len(months)), key=lambda i: months[i][2])
    for i, (year, month, total) in enumerate(months):
        cx = x0 + slot * (i + .5)
        dl = t0 + .4 + i * .05
        if total:
            hg = max(total * scale, 3)
            card.add(f'<g filter="{gf}"><path d="{rr(cx - bwid / 2, base - hg, bwid, hg + 4, 4)}" fill="{fill}" '
                     f'clip-path="url(#basecl)"{card.anim("gy", dl)}/></g>')
            if i == peak_i or i == len(months) - 1 or total == max(t for *_, t in months[:-1] or [(0, 0, 0)]):
                card.text(cx, base - hg - 9, str(total), "monob", anchor="middle", kind="fade", delay=dl + .6, size=12)
        else:
            card.add(f'<rect x="{n(cx - 2)}" y="{n(base - 4)}" width="4" height="4" rx="2" fill="{c["rule2"]}"{card.anim("pop", dl)}/>')
        card.text(cx, base + 18, MONTHS[month - 1][0] if slot < 26 else MONTHS[month - 1], "mono", fill="ink2",
                  anchor="middle", kind="fade", delay=t0 + .3, size=10.5)
    card.defs.append(f'<clipPath id="basecl"><rect x="0" y="0" width="{W}" height="{base}"/></clipPath>')

    # -- heartbeat strip: last 120 days ------------------------------------
    days = data["days"][-120:]
    sx0, sx1, sy = PAD, W - PAD, 360
    amp = 52
    pk = max(cnt for _, cnt in days) or 1
    step_x = (sx1 - sx0) / (len(days) - 1)
    pts = []
    for i, (_, cnt) in enumerate(days):
        x = sx0 + i * step_x
        v = (cnt / pk) ** .6 * amp if cnt else 0
        # spike shape: dip, spike, settle
        if v:
            pts += [(x - step_x * .45, sy), (x - step_x * .2, sy + v * .18), (x, sy - v), (x + step_x * .25, sy + v * .1)]
        else:
            pts.append((x, sy))
    d = "M" + "L".join(f"{n(x)} {n(y)}" for x, y in pts)
    card.text(PAD, 300, "PULSE · LAST 120 DAYS", "label", fill="ink2", kind="up", delay=t0 + .6, size=10)
    card.text(W - PAD, 300, f"{days[0][0].day} {MONTHS[days[0][0].month - 1]} → {days[-1][0].day} {MONTHS[days[-1][0].month - 1]}",
              "mono", fill="ink2", anchor="end", kind="fade", delay=t0 + .6, size=10.5)
    card.rule(sx0, sy, sx1, "rule", delay=t0 + .6, dash="1 5")
    pulse = card.lingrad([(0, "cyan", .5), (.6, "violet", 1), (1, "rose", 1)], sx0, 0, sx1, 0, units="userSpaceOnUse")
    card.add(f'<path d="{d}" fill="none" stroke="{pulse}" stroke-width="1.8" stroke-linejoin="round" '
             f'pathLength="1" filter="{gf}"{card.anim("draw", t0 + .7, dur=2.4)}/>')
    if not STATIC:
        card.add(f'<circle class="smil" r="3.5" fill="{c["ink"]}" opacity="0"><animateMotion path="{d}" dur="6s" '
                 f'begin="{n(t0 + 3.1)}s" repeatCount="indefinite" calcMode="linear"/>'
                 f'<set attributeName="opacity" to="1" begin="{n(t0 + 3.1)}s"/></circle>')
    return card


# ---------------------------------------------------------------- project visuals


def vis_scan(card: Card, x, y, w, h, hue, t0):
    """News article being read: lines highlighted as supported / flagged."""
    c = card.c
    rnd = Rand(11)
    px, py, pw, ph = x + 26, y + 22, w - 52, h - 44
    card.add(f'<g{card.anim("up", t0)}><path d="{rr(px, py, pw, ph, 8)}" fill="{c["sheet2"]}" stroke="{c["rule2"]}"/>')
    card.add(f'<rect x="{px + 16}" y="{py + 18}" width="{n(pw * .62)}" height="9" rx="3" fill="{c["ink"]}" fill-opacity=".85"/>'
             f'<rect x="{px + 16}" y="{py + 33}" width="{n(pw * .4)}" height="6" rx="3" fill="{c["ink2"]}" fill-opacity=".6"/></g>')
    ly = py + 56
    marks = {1: "lime", 3: hue, 5: "lime", 7: hue}
    for i in range(9):
        lw = (pw - 32) * (.55 + rnd() * .45)
        dl = t0 + .2 + i * .05
        card.add(f'<rect x="{px + 16}" y="{n(ly)}" width="{n(lw)}" height="5" rx="2.5" fill="{c["ink2"]}" fill-opacity=".45"'
                 f'{card.anim("gx", dl)}/>')
        if i in marks:
            m = c[marks[i]]
            card.add(f'<rect x="{px + 12}" y="{n(ly - 4)}" width="{n(lw + 8)}" height="13" rx="3" fill="{m}" fill-opacity=".18" '
                     f'stroke="{m}" stroke-opacity=".6"{card.anim("gx", t0 + 1 + i * .12)}/>')
            gx = px + pw - 22
            sym = "M-3.5 0l2.5 2.5 5-5" if marks[i] == "lime" else "M-3 -3l6 6M3 -3l-6 6"
            card.add(f'<g transform="translate({n(gx)} {n(ly + 2.5)})"><g{card.anim("pop", t0 + 1.3 + i * .12)}>'
                     f'<circle r="7" fill="{m}"/><path d="{sym}" stroke="{c["ground"]}" stroke-width="2" fill="none" '
                     f'stroke-linecap="round"/></g></g>')
        ly += 17
    # scan beam
    beam = card.lingrad([(0, hue, 0), (.85, hue, .35), (1, hue, 1)], 0, 0, 0, 1)
    kf = card.keyframe("beam", f"from{{transform:translateY(0)}}to{{transform:translateY({n(ph - 30)}px)}}")
    card.defs.append(f'<clipPath id="paper"><path d="{rr(px, py, pw, ph, 8)}"/></clipPath>')
    card.add(f'<g clip-path="url(#paper)"><g{card.loop(kf, 2.8, t0, "ease-in-out", "alternate")}>'
             f'<rect x="{px}" y="{py}" width="{pw}" height="28" fill="{beam}"/>'
             f'<rect x="{px}" y="{py + 27}" width="{pw}" height="1.5" fill="{c[hue]}"/></g></g>')
    # verdict pill
    card.add(f'<g{card.anim("pop", t0 + 1.9)}><path d="{rr(px + pw - 104, py + ph - 14, 116, 28, 14)}" fill="{c[hue]}"/>'
             f'</g>')
    card.text(px + pw - 46, py + ph + 4.5, "EVIDENCE", "label", fill="ground",
              anchor="middle", kind="pop", delay=t0 + 1.9, size=10.5)


def vis_grid(card: Card, x, y, w, h, hue, t0):
    """A dataset table with flagged cells and a lens that sweeps it."""
    c = card.c
    rnd = Rand(23)
    cols, rows = 7, 8
    gx, gy = x + 24, y + 26
    cw, ch = (w - 48) / cols, 15
    card.add(f'<g{card.anim("fade", t0)}><rect x="{n(gx)}" y="{n(gy)}" width="{n(cw * cols)}" height="{ch}" rx="3" '
             f'fill="{c[hue]}" fill-opacity=".22"/></g>')
    flagged = {(2, 3), (5, 1), (4, 6), (6, 4)}
    for r in range(1, rows):
        for k in range(cols):
            cx, cy = gx + k * cw, gy + r * (ch + 3)
            v = .25 + rnd() * .7
            dl = t0 + .1 + (r + k) * .025
            card.add(f'<rect x="{n(cx + 3)}" y="{n(cy + 5)}" width="{n((cw - 6) * v)}" height="5" rx="2.5" '
                     f'fill="{c["ink2"]}" fill-opacity=".45"{card.anim("gx", dl)}/>')
            if (k, r) in flagged:
                card.add(f'<rect x="{n(cx + .5)}" y="{n(cy)}" width="{n(cw - 1)}" height="{ch}" rx="3" fill="{c["rose"]}" '
                         f'fill-opacity=".16" stroke="{c["rose"]}" stroke-opacity=".75"{card.anim("pop", t0 + 1 + k * .08)}/>')
                card.add(f'<circle cx="{n(cx + cw - 4)}" cy="{n(cy + 2)}" r="7" fill="none" stroke="{c["rose"]}" stroke-width="1.2"'
                         f'{card.loop("ping", 2.2, -(k * .5), "ease-out", origin="50% 50%")}/>')
    # histogram with an outlier
    hy = gy + rows * (ch + 3) + 26
    bars = [.25, .45, .7, 1, .85, .55, .3, .15, 0, 0, .22]
    bw = (w - 48) / len(bars)
    for i, b in enumerate(bars):
        if not b:
            continue
        col = c["rose"] if i == len(bars) - 1 else c[hue]
        bh = b * 44
        card.add(f'<rect x="{n(gx + i * bw + 2)}" y="{n(hy + 44 - bh)}" width="{n(bw - 4)}" height="{n(bh)}" rx="2" '
                 f'fill="{col}" fill-opacity="{".95" if i == len(bars) - 1 else ".6"}"{card.anim("gy", t0 + .6 + i * .04)}/>')
    card.rule(gx, hy + 44.5, gx + cw * cols, "rule2", delay=t0 + .5)
    card.text(gx + cw * cols, hy + 4, "outlier", "mono", fill="rose", anchor="end", kind="fade", delay=t0 + 1.4, size=10)
    # lens
    if not STATIC:
        path = (f"M{n(gx + cw * 2.5)} {n(gy + 60)}C{n(gx + cw * 6)} {n(gy + 30)} {n(gx + cw * 6)} {n(gy + 120)} "
                f"{n(gx + cw * 4)} {n(gy + 110)}S{n(gx + cw * .5)} {n(gy + 90)} {n(gx + cw * 2.5)} {n(gy + 60)}Z")
        card.add(f'<g class="smil" opacity=".95"><circle r="19" fill="{c[hue]}" fill-opacity=".1" stroke="{c[hue]}" stroke-width="2"/>'
                 f'<path d="M13.5 13.5l9 9" stroke="{c[hue]}" stroke-width="3.5" stroke-linecap="round"/>'
                 f'<animateMotion path="{path}" dur="7s" begin="{n(t0 + 1)}s" repeatCount="indefinite"/></g>')


def vis_forecast(card: Card, x, y, w, h, hue, t0):
    """Sensor graph on top, actual vs forecast with a widening band below."""
    c = card.c
    rnd = Rand(5)
    # road graph
    road = (f"M{x + 22} {y + 52}C{x + 70} {y + 22} {x + 110} {y + 78} {x + 160} {y + 46}"
            f"S{x + 230} {y + 30} {x + w - 22} {y + 62}")
    road2 = f"M{x + 60} {y + 90}C{x + 100} {y + 60} {x + 140} {y + 50} {x + 160} {y + 46}S{x + 190} {y + 96} {x + w - 50} {y + 100}"
    for d, dl in ((road, 0), (road2, .15)):
        card.add(f'<path d="{d}" fill="none" stroke="{c["rule2"]}" stroke-width="6" stroke-linecap="round"{card.anim("fade", t0 + dl)}/>')
        card.add(f'<path d="{d}" fill="none" stroke="{c["ink2"]}" stroke-opacity=".5" stroke-dasharray="3 5"{card.anim("fade", t0 + dl)}/>')
        if not STATIC:
            for k in range(3):
                col = c[[hue, "lime", "rose"][k]]
                card.add(f'<circle class="smil" r="3" fill="{col}"><animateMotion path="{d}" dur="{n(3.5 + k * .9)}s" '
                         f'begin="{n(t0 + .5 + k * 1.1 + dl)}s" repeatCount="indefinite" '
                         f'{"keyPoints=\"1;0\" keyTimes=\"0;1\" calcMode=\"linear\"" if k == 1 else ""}/></circle>')
    for sx, sy in ((x + 22, y + 52), (x + 160, y + 46), (x + w - 22, y + 62), (x + 60, y + 90), (x + w - 50, y + 100), (x + 98, y + 56)):
        card.add(f'<g{card.anim("pop", t0 + .3)}><circle cx="{n(sx)}" cy="{n(sy)}" r="5.5" fill="{c["ground"]}" stroke="{c[hue]}" '
                 f'stroke-width="2"/></g>')
    # chart
    cx0, cx1, cy0, cy1 = x + 22, x + w - 22, y + 128, y + h - 26
    now = cx0 + (cx1 - cx0) * .62
    N = 40
    vals = []
    v = .5
    for i in range(N):
        v += (rnd() - .5) * .22 + (.5 - v) * .12
        vals.append(min(max(v, .12), .9))
    def px(i): return cx0 + (cx1 - cx0) * i / (N - 1)
    def py(v): return cy1 - (cy1 - cy0) * v
    ni = int((N - 1) * .62)
    actual = "M" + "L".join(f"{n(px(i))} {n(py(vals[i]))}" for i in range(N))
    fc = [vals[ni]]
    for i in range(ni + 1, N):
        fc.append(fc[-1] * .6 + vals[i] * .4 + .015)
    upper = [(px(ni + k), py(min(fc[k] + .02 + k * .012, .98))) for k in range(len(fc))]
    lower = [(px(ni + k), py(max(fc[k] - .02 - k * .012, .02))) for k in range(len(fc))]
    band = "M" + "L".join(f"{n(a)} {n(b)}" for a, b in upper) + "L" + "L".join(f"{n(a)} {n(b)}" for a, b in reversed(lower)) + "Z"
    card.rule(cx0, cy1, cx1, "rule2", delay=t0 + .3)
    card.add(f'<path d="{band}" fill="{c[hue]}" fill-opacity=".18"{card.anim("fade", t0 + 1.4)}/>')
    past = "M" + "L".join(f"{n(px(i))} {n(py(vals[i]))}" for i in range(ni + 1))
    card.add(f'<path d="{past}" fill="none" stroke="{c["ink"]}" stroke-width="1.8" pathLength="1"{card.anim("draw", t0 + .4)}/>')
    card.add(f'<path d="{actual}" fill="none" stroke="{c["ink2"]}" stroke-opacity=".35" stroke-width="1.2" stroke-dasharray="2 3"'
             f'{card.anim("fade", t0 + 1.6)}/>')
    fpath = "M" + "L".join(f"{n(px(ni + k))} {n(py(fc[k]))}" for k in range(len(fc)))
    card.add(f'<path d="{fpath}" fill="none" stroke="{c[hue]}" stroke-width="2.2" pathLength="1"{card.anim("draw", t0 + 1.2)}/>')
    card.add(f'<path d="M{n(now)} {n(cy0 - 6)}V{n(cy1)}" stroke="{c[hue]}" stroke-dasharray="3 3"{card.anim("gy", t0 + 1.1)}/>')
    card.add(f'<circle cx="{n(px(ni))}" cy="{n(py(vals[ni]))}" r="4" fill="{c[hue]}"{card.anim("pop", t0 + 1.2)}/>')
    card.add(f'<circle cx="{n(px(ni))}" cy="{n(py(vals[ni]))}" r="12" fill="none" stroke="{c[hue]}"{card.loop("ping", 2, t0 + 1.4, "ease-out", origin="50% 50%")}/>')
    card.text(now + 6, cy0 + 2, "+60 min", "mono", fill=hue, kind="fade", delay=t0 + 1.5, size=10)
    card.text(now - 6, cy0 + 2, "observed", "mono", fill="ink2", anchor="end", kind="fade", delay=t0 + 1.5, size=10)


def vis_radar(card: Card, x, y, w, h, hue, t0):
    """Campus blocks under a radar sweep, with pings where events land."""
    c = card.c
    cx, cy = x + w / 2, y + h / 2 + 4
    R = min(w, h) / 2 - 18
    card.add(f'<g{card.anim("fade", t0)}>')
    for k in (1, 2, 3):
        card.add(f'<circle cx="{n(cx)}" cy="{n(cy)}" r="{n(R * k / 3)}" fill="none" stroke="{c[hue]}" stroke-opacity="{n(.12 + k * .06)}"/>')
    card.add(f'<path d="M{n(cx - R)} {n(cy)}H{n(cx + R)}M{n(cx)} {n(cy - R)}V{n(cy + R)}" stroke="{c[hue]}" stroke-opacity=".15"/>')
    card.add("</g>")
    # campus blocks
    rnd = Rand(31)
    blocks = []
    for k in range(9):
        a = rnd() * 2 * math.pi
        r = R * (.25 + rnd() * .62)
        bx, by = cx + math.cos(a) * r, cy + math.sin(a) * r
        bw, bh = 12 + rnd() * 16, 10 + rnd() * 12
        blocks.append((bx, by))
        card.add(f'<rect x="{n(bx - bw / 2)}" y="{n(by - bh / 2)}" width="{n(bw)}" height="{n(bh)}" rx="3" fill="{c["sheet2"]}" '
                 f'stroke="{c["ink2"]}" stroke-opacity=".5"{card.anim("pop", t0 + .2 + k * .05)}/>')
    # paths between blocks
    pth = "".join(f"M{n(blocks[k][0])} {n(blocks[k][1])}L{n(blocks[(k + 2) % 9][0])} {n(blocks[(k + 2) % 9][1])}" for k in range(0, 9, 2))
    card.add(f'<path d="{pth}" stroke="{c["ink2"]}" stroke-opacity=".25" stroke-dasharray="2 4"{card.anim("fade", t0 + .5)}/>')
    # sweep
    sweep = card.lingrad([(0, hue, 0), (1, hue, .55)], 0, 1, 1, 0)
    a0 = -math.pi / 2
    a1 = a0 + .9
    wedge = (f"M{n(cx)} {n(cy)}L{n(cx + R * math.cos(a0))} {n(cy + R * math.sin(a0))}"
             f"A{n(R)} {n(R)} 0 0 1 {n(cx + R * math.cos(a1))} {n(cy + R * math.sin(a1))}Z")
    sw = "" if STATIC else f";animation:spin 4s linear {n(t0)}s infinite"
    card.add(f'<g style="transform-origin:{n(cx)}px {n(cy)}px{sw}">'
             f'<path d="{wedge}" fill="{sweep}"/><path d="M{n(cx)} {n(cy)}L{n(cx + R * math.cos(a1))} {n(cy + R * math.sin(a1))}" '
             f'stroke="{c[hue]}" stroke-width="1.5"/></g>')
    # pings on a few blocks
    for k, (bx, by) in enumerate(blocks[::2]):
        hue2 = [hue, "amber", "rose", "cyan", "lime"][k % 5]
        card.add(f'<circle cx="{n(bx)}" cy="{n(by)}" r="16" fill="none" stroke="{c[hue2]}" stroke-width="1.5"'
                 f'{card.loop("ping", 2.4, t0 + .6 + k * .55, "ease-out", origin="50% 50%")}/>')
        card.add(f'<circle cx="{n(bx)}" cy="{n(by)}" r="3" fill="{c[hue2]}"{card.anim("pop", t0 + .8 + k * .1)}/>')
    card.add(f'<circle cx="{n(cx)}" cy="{n(cy)}" r="4" fill="{c[hue]}"/>')
    # notification toasts
    for k, (lbl, hue2) in enumerate((("EVENT", "amber"), ("NOTICE", "cyan"), ("ISSUE", "rose"))):
        tx, ty = x + 14, y + 16 + k * 26
        tw = measure(lbl, "label", 9.5) + 26
        card.add(f'<g{card.anim("up", t0 + 1.2 + k * .2)}><path d="{rr(tx, ty, tw, 20, 10)}" fill="{c["ground"]}" fill-opacity=".85" '
                 f'stroke="{c[hue2]}" stroke-opacity=".6"/><circle cx="{tx + 10}" cy="{ty + 10}" r="3" fill="{c[hue2]}"/></g>')
        card.text(tx + 18, ty + 13.8, lbl, "label", fill="ink", kind="up", delay=t0 + 1.2 + k * .2, size=9.5)


VISUALS = {"scan": vis_scan, "grid": vis_grid, "forecast": vis_forecast, "radar": vis_radar}


# ---------------------------------------------------------------- project cards


def repo_card(theme: str, spec: dict, repo: dict, idx: int, total: int, t0: float) -> Card:
    hue = spec["hue"]
    vx, vw = 18, 300
    tx = vx + vw + 30
    tw = W - tx - PAD
    desc = wrap((spec.get("description") or repo.get("description") or "").strip(), "body", tw, 5)
    stack = spec.get("stack") or ([repo["primaryLanguage"]["name"]] if repo.get("primaryLanguage") else [])
    title = spec.get("title") or repo["name"]
    slug = f"lloydaq/{repo['name']}"

    # layout chips to know the height
    chip_rows, cx, row = [[]], 0.0, 0
    for s in stack:
        w = measure(s, "mono", 11.5) + 34
        if cx + w > tw and chip_rows[-1]:
            chip_rows.append([])
            cx = 0
        chip_rows[-1].append(s)
        cx += w + 8
    desc_y = 128
    chips_y = desc_y + (len(desc) - 1) * 22 + 26
    meta_y = chips_y + len(chip_rows) * 34 + 26
    h = max(meta_y + 40, 300)

    card = Card(f"repo-{spec['repo'].lower()}", W, h, theme,
                f"Project {idx + 1} of {total}: {title} ({slug}). {' '.join(desc)} Stack: {', '.join(stack)}.")
    c = card.c
    aurora(card, [(vx + vw / 2, h / 2, 280, hue, .5), (W - 60, 0, 260, hue, .18)], t0)

    # Visual panel
    vy, vh = 18, h - 36
    card.add(f'<g{card.anim("blur", t0 + .05)}><path d="{rr(vx, vy, vw, vh, 12)}" fill="{c["sheet"]}" fill-opacity=".8" '
             f'stroke="{c[hue]}" stroke-opacity=".35"/></g>')
    pid = card.id("vp")
    card.defs.append(f'<clipPath id="{pid}"><path d="{rr(vx, vy, vw, vh, 12)}"/></clipPath>')
    card.add(f'<g clip-path="url(#{pid})">')
    dot_grid(card, vx, vy, vx + vw, vy + vh, 16, .18, t0)
    VISUALS[spec["visual"]](card, vx, vy, vw, vh, hue, t0 + .3)
    card.add("</g>")
    corner_marks(card, vx - 6, vy - 6, vw + 12, vh + 12, hue, 12, t0 + .2)

    # Ghost index numeral, top right
    num = f"{idx + 1:02d}"
    card.chars["sans"].update(num)
    card.add(f'<text x="{W - 16}" y="{h - 12}" text-anchor="end" class="t-ghost{"" if STATIC else " fade"}" font-size="80" fill="none" '
             f'stroke="{c[hue]}" stroke-opacity=".22" stroke-width="1.2"'
             f'{"" if STATIC else f" style=\"animation-delay:{n(t0 + .3)}s\""}>{num}</text>')

    card.text(tx, 46, f"{num} / {total:02d}  ·  {spec.get('kicker', '')}", "label", fill=hue, kind="up", delay=t0 + .1, size=10.5)
    tsize = min(STYLES["head"][1], STYLES["head"][1] * (tw - 30) / measure(title, "head"))
    card.text(tx, 80, title, "head", kind="up", delay=t0 + .15, size=tsize)
    card.text(tx, 102, slug, "mono", fill="ink2", kind="up", delay=t0 + .2, size=12)
    card.arrow(tx + measure(slug, "mono", 12) + 6, 90, 14, t0 + .3, color=hue)
    for i, line in enumerate(desc):
        card.text(tx, desc_y + i * 22, line, "body", kind="up", delay=t0 + .25 + i * .05)
    for r, rowc in enumerate(chip_rows):
        cx = tx
        for k, s in enumerate(rowc):
            cx += card.chip(cx, chips_y + r * 34, s, HUES[(HUES.index(hue) + k) % len(HUES)],
                            t0 + .45 + (r * 4 + k) * .05, h=26, size=11.5) + 8

    # meta row
    card.rule(tx, meta_y - 14, W - PAD, "rule", delay=t0 + .6)
    mx = tx
    lang = (repo.get("primaryLanguage") or {}).get("name")
    if lang:
        card.add(f'<circle cx="{n(mx + 5)}" cy="{n(meta_y + 2)}" r="5" fill="{c[hue]}"{card.anim("pop", t0 + .7)}/>')
        card.text(mx + 16, meta_y + 6.5, lang, "mono", fill="ink2", kind="up", delay=t0 + .7, size=12)
        mx += 16 + measure(lang, "mono", 12) + 22
    stars = repo.get("stargazerCount", 0)
    card.icon(mx, meta_y - 6, 16, ICONS["star"], color="amber", delay=t0 + .75)
    card.text(mx + 21, meta_y + 6.5, str(stars), "mono", fill="ink2", kind="up", delay=t0 + .75, size=12)
    mx += 21 + measure(str(stars), "mono", 12) + 22
    if repo.get("pushedAt"):
        d = dt.date.fromisoformat(repo["pushedAt"][:10])
        card.icon(mx, meta_y - 6, 16, ICONS["clock"], color="ink2", delay=t0 + .8)
        card.text(mx + 21, meta_y + 6.5, f"pushed {day_label(d)}", "mono", fill="ink2", kind="up", delay=t0 + .8, size=12)
    return card


# ---------------------------------------------------------------- isometric city


def city(theme: str, data: dict, st: dict, t0: float) -> Card:
    days = data["days"]
    total = st["total"]
    h = 420
    card = Card("city", W, h, theme,
                f"Isometric contribution city: {plural(total, 'contribution')} between "
                f"{day_label(days[0][0])} and {day_label(days[-1][0])}; each tower is one day.")
    c = card.c
    aurora(card, [(640, 300, 360, "violet", .35), (180, 120, 300, "cyan", .2), (720, 120, 260, "rose", .25)], t0)

    first = days[0][0]
    start = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    weeks = (days[-1][0] - start).days // 7 + 1
    # Projection: weeks run to the lower right, weekdays to the upper right.
    ux, uy = 12.4, 2.9      # one week
    vx_, vy_ = 7.2, -4.3    # one weekday
    span_x = (weeks + .2) * ux + 7.2 * vx_
    ox = (W - span_x) / 2 + .6 * ux + .6 * vx_
    oy = h - 34 - (weeks - .4) * uy + .6 * vy_ - 10
    peak = max(cnt for _, cnt in days) or 1
    hmax = 150
    ramp = [mix(c["sheet2"], c["rule2"], .5), c["cyan"], c["violet"], c["rose"], c["amber"]]
    nonzero = sorted(cnt for _, cnt in days if cnt)
    cuts = [nonzero[min(len(nonzero) - 1, int(len(nonzero) * q))] for q in (.33, .66, .9)] if nonzero else []

    def level(cnt):
        return 0 if not cnt else 1 + sum(cnt > q for q in cuts)

    def P(wk, dw, z=0.0):
        return ox + wk * ux + dw * vx_, oy + wk * uy + dw * vy_ - z

    # base plate
    a, b, cc, d = P(-.6, -.6), P(weeks - .4, -.6), P(weeks - .4, 6.6), P(-.6, 6.6)
    plate = f"M{n(a[0])} {n(a[1])}L{n(b[0])} {n(b[1])}L{n(cc[0])} {n(cc[1])}L{n(d[0])} {n(d[1])}Z"
    card.add(f'<path d="{plate}" fill="{c["sheet"]}" fill-opacity=".7" stroke="{c["rule2"]}"{card.anim("fade", t0)}/>')
    depth = 10
    side = (f"M{n(a[0])} {n(a[1])}L{n(b[0])} {n(b[1])}L{n(b[0])} {n(b[1] + depth)}L{n(a[0])} {n(a[1] + depth)}Z")
    card.add(f'<path d="{side}" fill="{c["rule2"]}" fill-opacity=".6"{card.anim("fade", t0)}/>')
    side2 = (f"M{n(b[0])} {n(b[1])}L{n(cc[0])} {n(cc[1])}L{n(cc[0])} {n(cc[1] + depth)}L{n(b[0])} {n(b[1] + depth)}Z")
    card.add(f'<path d="{side2}" fill="{c["rule"]}"{card.anim("fade", t0)}/>')

    s = .78  # tower footprint as a fraction of a cell
    cols: dict[int, list[str]] = {}
    tallest = None
    # Painter's order: back (high weekday, low week) to front.
    order = sorted(days, key=lambda t: ((t[0] - start).days // 7 - ((t[0].weekday() + 1) % 7), -((t[0].weekday() + 1) % 7)))
    for dday, cnt in order:
        wk, dw = (dday - start).days // 7, (dday.weekday() + 1) % 7
        lv = level(cnt)
        z = 2 + (cnt / peak) ** .7 * hmax if cnt else 1.5
        col = ramp[lv]
        x0, x1 = wk + (1 - s) / 2 - .5, wk + (1 + s) / 2 - .5
        y0, y1 = dw + (1 - s) / 2 - .5, dw + (1 + s) / 2 - .5
        # corners: front = (x1, y0) is lowest on screen? compute all
        p00, p10, p11, p01 = P(x0, y0), P(x1, y0), P(x1, y1), P(x0, y1)
        top = [P(x0, y0, z), P(x1, y0, z), P(x1, y1, z), P(x0, y1, z)]
        poly = lambda pts: "M" + "L".join(f"{n(px)} {n(py)}" for px, py in pts) + "Z"
        parts = []
        if cnt:
            # visible sides: the y0 face (front-left) and the x1 face (front-right)
            parts.append(f'<path d="{poly([p00, p10, top[1], top[0]])}" fill="{mix(col, "#000000", .38)}"/>')
            parts.append(f'<path d="{poly([p10, p11, top[2], top[1]])}" fill="{mix(col, "#000000", .2)}"/>')
            parts.append(f'<path d="{poly(top)}" fill="{mix(col, "#ffffff", .12)}"/>')
            if tallest is None or cnt > tallest[0]:
                tallest = (cnt, P(wk, dw, z + 6), dday)
        else:
            parts.append(f'<path d="{poly(top)}" fill="{col}" fill-opacity=".9"/>')
        cols.setdefault(wk - dw, []).append("".join(parts))
    for diag in sorted(cols):
        dl = t0 + .25 + (diag + 6) * .022
        card.add(f'<g{card.anim("gy", dl)}>{"".join(cols[diag])}</g>')

    if tallest:
        cnt, (px, py), dday = tallest
        lbl = f"{cnt} · {dday.day} {MONTHS[dday.month - 1]}"
        lw = measure(lbl, "monob", 11) + 20
        bx, by = min(px - lw / 2, W - PAD - lw), py - 40
        card.add(f'<g{card.anim("up", t0 + 1.6)}><path d="M{n(px)} {n(py)}V{n(by + 24)}" stroke="{c["amber"]}" stroke-dasharray="2 2"/>'
                 f'<path d="{rr(bx, by, lw, 24, 12)}" fill="{c["amber"]}"/></g>')
        card.text(bx + lw / 2, by + 16, lbl, "monob", fill="ground", anchor="middle", kind="up", delay=t0 + 1.6, size=11)
        card.add(f'<circle cx="{n(px)}" cy="{n(py + 4)}" r="9" fill="none" stroke="{c["amber"]}"'
                 f'{card.loop("ping", 2, t0 + 2, "ease-out", origin="50% 50%")}/>')

    # month labels along the front edge
    prev = -99
    for dday, _ in days:
        if dday.day == 1:
            wk = (dday - start).days // 7
            if wk - prev < 4:
                continue
            prev = wk
            lx, ly = P(wk, -.6)
            card.text(lx - 4, ly + 26, MONTHS[dday.month - 1], "mono", fill="ink2", kind="fade", delay=t0 + .3, size=10,
                      extra=f' transform="rotate(16 {n(lx - 4)} {n(ly + 26)})"')

    # Overlay stats, top-left
    card.text(PAD, 52, f"{total}", "num", kind="up", delay=t0 + .1, size=44)
    tw = measure(str(total), "num", 44)
    card.text(PAD + tw + 12, 34, "CONTRIBUTIONS", "label", fill="amber", kind="up", delay=t0 + .15, size=10)
    card.text(PAD + tw + 12, 51, "in the last year", "small", fill="ink2", kind="up", delay=t0 + .2, size=12.5)
    # Three more readings under the headline number.
    wd = [0] * 7
    for dday, cnt in days:
        wd[dday.weekday()] += cnt
    names = "Mondays Tuesdays Wednesdays Thursdays Fridays Saturdays Sundays".split()
    weekly: dict[int, int] = {}
    for dday, cnt in days:
        weekly[(dday - start).days // 7] = weekly.get((dday - start).days // 7, 0) + cnt
    active = [cnt for _, cnt in days if cnt]
    reads = [("BEST WEEK", str(max(weekly.values()) if weekly else 0)),
             ("PER ACTIVE DAY", f"{sum(active) / len(active):.1f}" if active else "0"),
             ("BUSIEST ON", names[max(range(7), key=lambda i: wd[i])] if total else "—")]
    rx = PAD
    for i, (k, v) in enumerate(reads):
        card.text(rx, 92, k, "label", fill="ink2", kind="up", delay=t0 + .3 + i * .08, size=9.5)
        card.text(rx, 113, v, "monob", kind="up", delay=t0 + .35 + i * .08, size=16)
        rx += max(measure(k, "label", 9.5), measure(v, "monob", 16)) + 28
    # legend top-right
    lx = W - PAD
    card.text(lx, 50, "more", "mono", fill="ink2", anchor="end", kind="fade", delay=t0 + .2, size=10.5)
    lx -= measure("more", "mono", 10.5) + 8
    card.add(f'<g{card.anim("fade", t0 + .2)}>')
    for col in reversed(ramp):
        lx -= 12
        card.add(f'<rect x="{n(lx)}" y="40" width="12" height="12" rx="3" fill="{col}"/>')
        lx -= 4
    card.add("</g>")
    card.text(lx - 4, 50, "less", "mono", fill="ink2", anchor="end", kind="fade", delay=t0 + .2, size=10.5)
    return card


# ---------------------------------------------------------------- footer


def footer(theme: str, data: dict, t0: float) -> Card:
    h = 200
    card = Card("footer", W, h, theme, "Thanks for stopping by. Cards rendered from live GitHub data, refreshed daily.")
    c = card.c
    aurora(card, [(W / 2, h, 420, "violet", .45), (120, h, 260, "amber", .3), (720, h, 260, "cyan", .3)], t0)
    # Layered sine waves that scroll sideways.
    for k, (hue, amp, wl, yy, dur, op) in enumerate((("violet", 14, 210, 150, 9, .7), ("rose", 10, 160, 158, 6.5, .8),
                                                     ("amber", 7, 120, 166, 5, .9), ("cyan", 5, 90, 172, 4, .6))):
        pts = []
        x = -wl
        while x <= W + wl:
            pts.append(f"{n(x)} {n(yy + amp * math.sin(2 * math.pi * x / wl))}")
            x += 6
        d = "M" + "L".join(pts)
        kf = card.keyframe(f"wv{k}", f"to{{transform:translateX({wl}px)}}")
        card.add(f'<g{card.anim("fade", t0 + k * .1)}><path d="{d}" fill="none" stroke="{c[hue]}" stroke-opacity="{op}" '
                 f'stroke-width="{1.2 + k * .3}"{card.loop(kf, dur, 0, "linear", "normal" if k % 2 else "reverse")}/></g>')
    card.text(W / 2, 66, "Thanks for scrolling.", "head", anchor="middle", kind="up", delay=t0 + .1, size=26)
    card.text(W / 2, 94, "Let’s build something that shows its work.", "state", fill="ink2", anchor="middle",
              kind="up", delay=t0 + .2, size=17)
    last = data["days"][-1][0]
    card.text(W / 2, 124, f"rendered by scripts/render_cards.py · data as of {day_label(last)}", "mono", fill="ink2",
              anchor="middle", kind="fade", delay=t0 + .4, size=10.5, opacity=.8)
    return card


# ---------------------------------------------------------------- main


def preview_page(names: list[str]) -> str:
    def stack(theme: str) -> str:
        out = []
        for name in names:
            width = "32.5%" if name.startswith("tile-") else "100%"
            out.append(f'<img src="{name}-{theme}.svg" width="{width}" alt="">')
        return "".join(out)
    bg = {"dark": "#0d1117", "light": "#ffffff"}
    cols = "".join(f'<div style="background:{bg[t]};padding:32px"><div style="max-width:846px;margin:auto">'
                   f'{stack(t)}</div></div>' for t in THEMES)
    return ("<!doctype html><meta charset=utf-8><title>Card preview</title>"
            "<style>body{margin:0}img{margin:0 0 8px;display:inline-block;vertical-align:top}"
            "img[width='32.5%']{margin-right:.5%}img{margin-right:0}</style>" + cols)


def main() -> int:
    data = load_data()
    missing = [f["repo"] for f in FEATURED if f["repo"] not in data["repos"]]
    if missing:
        print(f"featured repos not in {DATA.name}: {', '.join(missing)}", file=sys.stderr)
        return 1
    st = stats(data["days"])

    if OUT.exists():
        for old in OUT.glob("*.svg"):
            old.unlink()
    OUT.mkdir(exist_ok=True)

    names: list[str] = []
    for theme in THEMES:
        # Start times step down the page so the README plays top to bottom.
        secs = {sid: (num, title, note.format(n=len(FEATURED))) for sid, num, title, note in SECTIONS}
        cards = [hero(theme, data, st, 0.0)]
        cards += [tile(theme, spec, 1.2 + i * .12) for i, spec in enumerate(TILES)]
        cards.append(section(theme, "toolkit", *secs["toolkit"], 1.5))
        cards.append(toolkit(theme, 1.6))
        cards.append(section(theme, "signal", *secs["signal"], 1.7))
        cards.append(activity(theme, data, st, 1.8))
        cards.append(section(theme, "work", *secs["work"], 2.2))
        cards += [repo_card(theme, f, data["repos"][f["repo"]], i, len(FEATURED), 2.3 + i * .35)
                  for i, f in enumerate(FEATURED)]
        cards.append(section(theme, "city", *secs["city"], 3.6))
        cards.append(city(theme, data, st, 3.7))
        cards.append(footer(theme, data, 4.2))
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
