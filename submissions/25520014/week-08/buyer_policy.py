"""Week 08 buyer policy: the numbers the buyer runs on, their ranges, and the buyer's playbook.

The buyer side mirrors policy.py: market_buyer.py and buyer_forum.py read data/buyer_policy.json and
data/buyer_playbook.json; only buyer_manager.py writes them, inside the ranges and at most policy.MAX_STEP of a
range per decision. The price model and the break rates are not here; model.py computes them from the ledger.
"""
import json
import math
import os
import time

import policy
from policy import DATA, MAX_STEP, INTERNAL

PATH = DATA / "buyer_policy.json"
PLAYBOOK = DATA / "buyer_playbook.json"

# name: (initial, low, high)
NUMBERS = {
    "open_under":     (0.85, 0.70, 0.95),   # the first offer: this share of the lower of the ask and the usual price
    "raise":          (0.35, 0.20, 0.60),   # each new offer closes this share of the gap to min(ask, cap)
    "quotes":         (3, 2, 4),            # shops that get the opening message
    "next_shop_secs": (90, 60, 150),        # time a second shop needs; with less left the order ends with no deal
    "post_every_h":   (0.15, 0.09, 2.0),    # hours between our buyer posts (posts written count 10% of buyer ALL; market limit 5 min)
}
GUIDE_KEYS = ("negotiation", "forum")


def defaults():
    p = {k: v[0] for k, v in NUMBERS.items()}
    p.update(updated=0, reason="initial values, 11-02", version=0)
    return p


def clip(new, old=None):
    old = old or defaults()
    out = dict(old)
    for k, (_, lo, hi) in NUMBERS.items():
        if k not in new:
            continue
        try:
            v = float(new[k])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):     # "nan" parses as a float and would pass every comparison below
            continue
        whole = isinstance(NUMBERS[k][0], int)
        step = MAX_STEP * (hi - lo)
        if whole:                    # at least one unit, rounded half up: round() halves to even and can get stuck
            step = max(step, 1)
        v = min(max(v, old[k] - step, lo), old[k] + step, hi)
        out[k] = math.floor(v + 0.5) if whole else round(v, 4)
    return out


def _load(path, base):
    try:
        return {**base, **json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return base


def _save(path, b, reason):
    b = dict(b, updated=time.time(), reason=reason, version=int(b.get("version", 0)) + 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return b


def load():
    return _load(PATH, defaults())


def save(p, reason):
    return _save(PATH, p, reason)


def load_playbook():
    return _load(PLAYBOOK, {"negotiation": "", "forum": "", "version": 0, "updated": 0, "reason": ""})


def save_playbook(b, reason):
    return _save(PLAYBOOK, b, reason)


def clip_playbook(new, old):
    """Guidance without digits or words for internal numbers, within policy.GUIDE_CHARS. Returns (playbook, refused)."""
    out, refused = dict(old), []
    for k in GUIDE_KEYS:
        v = new.get(k)
        if v is None:
            continue
        v = " ".join(str(v).split())
        if any(ch.isdigit() for ch in v):
            refused.append(f"{k}: has digits")
        elif INTERNAL.search(v):
            refused.append(f"{k}: names an internal number ({INTERNAL.search(v).group()})")
        elif len(v) > policy.GUIDE_CHARS:
            refused.append(f"{k}: longer than {policy.GUIDE_CHARS} characters")
        else:
            out[k] = v
    return out, refused
