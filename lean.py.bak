"""Political-lean estimator for news text: transparent, rule-based, English only.

It rates the LANGUAGE of one article (loaded vocabulary and attribution habits), not the author or the outlet.
Output is a percentage split across five bins that always adds up to 100:
    far_left | left | centre | right | far_right
Weak evidence is pulled towards "centre", and every estimate lists the phrases that drove it.
It is a rough guide (rule-based models of this kind score roughly 50-60% on standard tests): editors can override it.

Use on its own:   python lean.py story.txt      (or pipe text in)
Extend vocabulary: set LEAN_LEXICON to a JSON file such as
    {"left": {"phrase": 1}, "right": {"phrase": 3}, "political": ["term"], "balance": ["phrase"]}
Weights: 1 = lean-associated framing, 3 = strongly ideological or partisan vocabulary.
"""
from __future__ import annotations

import html
import json
import math
import re
import sys
from collections import Counter

BINS = ("far_left", "left", "centre", "right", "far_right")
LABELS = {"far_left": "Far left", "left": "Left", "centre": "Centre", "right": "Right", "far_right": "Far right"}
COLORS = {"far_left": "#1d4ed8", "left": "#60a5fa", "centre": "#9ca3af", "right": "#f87171", "far_right": "#b91c1c"}

LEFT = {
    "inequality": 1, "social justice": 1, "workers' rights": 1, "labour rights": 1, "labor rights": 1, "minimum wage": 1,
    "universal healthcare": 1, "public healthcare": 1, "wealth tax": 1, "progressive tax": 1, "climate justice": 1,
    "marginalised": 1, "marginalized": 1, "systemic": 1, "trade union": 1, "collective bargaining": 1, "affordable housing": 1,
    "social welfare": 1, "safety net": 1, "public ownership": 1, "corporate greed": 1, "tax the rich": 1, "gender equality": 1,
    "reproductive rights": 1, "austerity": 1, "affirmative action": 1, "gun control": 1, "refugee rights": 1, "civil liberties": 1,
    "class struggle": 3, "ruling class": 3, "capitalist exploitation": 3, "means of production": 3, "bourgeois": 3,
    "proletariat": 3, "imperialist": 3, "neoliberal": 3, "oligarch": 3, "wage slavery": 3, "anti-capitalist": 3,
}
RIGHT = {
    "free market": 1, "deregulation": 1, "tax cuts": 1, "lower taxes": 1, "private enterprise": 1, "law and order": 1,
    "traditional values": 1, "family values": 1, "national security": 1, "border security": 1, "border control": 1,
    "illegal immigration": 1, "illegal immigrants": 1, "fiscal discipline": 1, "small government": 1, "red tape": 1,
    "patriotic": 1, "national sovereignty": 1, "religious values": 1, "tough on crime": 1, "welfare dependency": 1,
    "job creators": 1, "school choice": 1, "gun rights": 1, "political correctness": 1, "woke": 1, "big government": 1,
    "globalist": 3, "great replacement": 3, "mass immigration": 3, "traitors": 3, "deep state": 3, "open borders": 3,
    "radical left": 3, "marxist agenda": 3, "enemy of the people": 3,
}
BALANCE = (  # attribution to several sides is a sign of even-handed reporting
    "on the other hand", "both sides", "critics said", "critics say", "supporters said", "supporters say", "opponents said",
    "according to the government", "the opposition said", "the ministry said", "analysts said", "experts said",
    "bipartisan", "cross-party", "consensus", "compromise", "declined to comment", "did not respond",
)
POLITICAL = (
    "parliament", "election", "vote", "voter", "ballot", "government", "minister", "prime minister", "president", "opposition",
    "party", "policy", "cabinet", "coalition", "legislation", "reform", "protest", "campaign", "manifesto", "regime",
    "senate", "congress", "lawmaker", "constitution", "political", "politician", "referendum", "sanctions", "ruling party",
    "caretaker", "mp", "mps",
)


def to_percent(values: dict) -> dict:
    """Round to whole percentages that add up to exactly 100 (largest remainder)."""
    total = sum(values.values()) or 1
    raw = {k: v * 100 / total for k, v in values.items()}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[:100 - sum(out.values())]:
        out[k] += 1
    return out


def _rx(terms) -> re.Pattern:
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")s?(?!\w)")


class LeanModel:
    VERSION = "rules-1"

    def __init__(self, extra_path: str = ""):
        self._left, self._right = dict(LEFT), dict(RIGHT)
        pol, bal = list(POLITICAL), list(BALANCE)
        if extra_path:
            try:
                with open(extra_path, encoding="utf-8") as f:
                    extra = json.load(f)
                self._left.update({k.lower(): float(v) for k, v in extra.get("left", {}).items()})
                self._right.update({k.lower(): float(v) for k, v in extra.get("right", {}).items()})
                pol += [x.lower() for x in extra.get("political", [])]
                bal += [x.lower() for x in extra.get("balance", [])]
            except (OSError, ValueError):
                pass                                              # a bad extra file never breaks the app
        self._lrx, self._rrx, self._prx, self._brx = _rx(self._left), _rx(self._right), _rx(pol), _rx(bal)

    def estimate(self, text: str, section: str | None = None) -> dict:
        t = text.lower().replace("\u2019", "'")
        words = max(1, len(re.findall(r"\w+", t)))
        cues, left, right, hits = [], 0.0, 0.0, 0
        for side, rx, table in (("left", self._lrx, self._left), ("right", self._rrx, self._right)):
            for term, c in Counter(m.group(1) for m in rx.finditer(t)).items():
                score = table[term] * min(c, 3)                   # repeating a phrase cannot dominate
                hits += min(c, 3)
                cues.append({"term": term, "side": side, "count": c, "weight": table[term], "score": score})
                if side == "left":
                    left += score
                else:
                    right += score
        topic = sum(min(c, 3) for c in Counter(m.group(1) for m in self._prx.finditer(t)).values())
        balance = min(len({m.group(1) for m in self._brx.finditer(t)}), 4) / 4
        political = (topic >= 4 and topic * 1000 / words >= 3) or len(cues) >= 3
        evidence = left + right
        # direction comes from which side's vocabulary dominates; how far out it sits comes from how strong that vocabulary is
        strength = min(1.0, 0.45 + 0.28 * (evidence / hits - 1)) if hits else 0.0   # mild phrases ~0.45, partisan ones ~1.0
        x = (right - left) / (evidence + 1) * strength * (1 - 0.4 * balance)         # -1 far left ... +1 far right
        model = [math.exp(-((x - c) ** 2) / (2 * 0.4 ** 2)) for c in (-1, -0.5, 0, 0.5, 1)]
        norm = sum(model)
        neutral = (0.03, 0.12, 0.70, 0.12, 0.03)                  # what "no loaded language" looks like
        e = (evidence / (evidence + 3)) * (1 - 0.35 * balance)    # how much to trust the language evidence
        pct = to_percent({b: e * m / norm + (1 - e) * n for b, m, n in zip(BINS, model, neutral)})
        conf = "High" if evidence >= 10 and words >= 150 else "Medium" if evidence >= 4 and words >= 100 else "Low"
        top = sorted(cues, key=lambda c: -c["score"])[:8]
        return {"pct": pct, "political": bool(political), "confidence": conf, "model": self.VERSION,
                "cues": [{k: c[k] for k in ("term", "side", "count", "weight")} for c in top]}


def bar_html(pct: dict) -> str:
    segs = "".join(
        f"<div style='width:{max(pct.get(b, 0), 0)}%;background:{COLORS[b]};color:#fff;font-size:.78rem;font-weight:600;"
        f"text-align:center;padding:.4rem 0;overflow:hidden'>{pct.get(b, 0) if pct.get(b, 0) >= 7 else ''}{'%' if pct.get(b, 0) >= 7 else ''}</div>"
        for b in BINS)
    legend = "".join(f"<span style='margin-right:.9rem;white-space:nowrap'><span style='display:inline-block;width:.7rem;height:.7rem;"
                     f"border-radius:2px;background:{COLORS[b]};margin-right:.3rem'></span>{LABELS[b]} {pct.get(b, 0)}%</span>" for b in BINS)
    return (f"<div style='display:flex;border-radius:6px;overflow:hidden'>{segs}</div>"
            f"<div style='font-size:.82rem;margin-top:.4rem;opacity:.85'>{legend}</div>")


def _shade(hex_color: str, t: float) -> tuple:
    rgb = tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return tuple(round(b + (c - b) * t) for b, c in zip((248, 250, 252), rgb))


def heatmap_html(rows: list) -> str:
    """rows: [(label, number_of_stories, {bin: percent})] -> a shaded table; darker cell = larger share."""
    th = "".join(f"<th style='padding:.4rem .6rem;text-align:center'>{LABELS[b]}</th>" for b in BINS)
    body = ""
    for label, n, pct in rows:
        cells = ""
        for b in BINS:
            v, t = pct.get(b, 0), min(1.0, pct.get(b, 0) / 60)
            bg, fg = "rgb(%d,%d,%d)" % _shade(COLORS[b], t), "#fff" if t > 0.6 else "#111"
            cells += f"<td style='background:{bg};color:{fg};text-align:center;padding:.45rem .6rem;font-weight:600'>{v}%</td>"
        body += (f"<tr><td style='padding:.45rem .6rem;white-space:nowrap'>{html.escape(str(label))}</td>{cells}"
                 f"<td style='text-align:center;padding:.45rem .6rem;opacity:.7'>{n}</td></tr>")
    return (f"<div style='overflow-x:auto'><table style='border-collapse:separate;border-spacing:3px;width:100%;font-size:.9rem'>"
            f"<tr><th style='text-align:left;padding:.4rem .6rem'></th>{th}<th style='padding:.4rem .6rem'>Stories</th></tr>{body}</table></div>")


if __name__ == "__main__":
    import os
    text = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    r = LeanModel(os.getenv("LEAN_LEXICON", "")).estimate(text)
    print("Political story:", "yes" if r["political"] else "no (estimate shown for reference)", "| confidence:", r["confidence"])
    for b in BINS:
        print(f"  {LABELS[b]:<9} {r['pct'][b]:>3}%  {'#' * (r['pct'][b] // 2)}")
    for c in r["cues"]:
        print(f"  cue [{c['side']}, weight {c['weight']}] {c['term']!r} x{c['count']}")
