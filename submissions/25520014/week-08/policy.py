"""Week 08 policy: the numbers the shop runs on, their allowed ranges, and the file they live in.

The restock hook, the seller and the forum posts read data/policy.json; only the manager writes it. Every value
has a range: the manager (opus, hourly) may move a value inside its range and at most MAX_STEP of the range per
decision, and whatever it writes is clipped here, so a bad answer cannot push the shop outside the ranges agreed
on 11-02. Posterior updates (demand, share, break rates, the price bandit) are not here; code computes them.
"""
import json
import math
import os
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", HERE / "data"))   # tests point this at a scratch folder
PATH = DATA / "policy.json"

ROUND_END = 1794927540          # 2026-11-17 23:59 KST (14:59 UTC), when Round 3 closes
MAX_STEP = 0.25                 # share of a value's range one manager decision may move it

# name: (initial, low, high). Booleans have no range and are flipped freely.
NUMBERS = {
    "refund_sd":         (2.0, 1.0, 3.0),      # cash kept out of restock: bankruptcy line + expected refunds + this many sd
    "unit_cap_frac":     (0.10, 0.05, 0.20),   # one unit's cost at most this share of the balance
    "min_margin":        (0.10, 0.05, 0.20),   # floor = cost x (1 + min_margin) + warranty premium
    "max_concessions":   (4, 2, 6),            # counters before the final price
    "c_discount":        (0.10, 0.00, 0.25),   # extra cut off the opening ask for grade C (food near its date, refurbished goods)
    "liquidate_hours":   (24, 12, 48),         # from this many hours before the end the floor falls to cost
    "half_life_h":       (24, 6, 72),          # demand memory: weight of a sale halves every this many hours
    "prior_hours":       (2.0, 0.5, 8),          # gamma prior worth this many hours of observation (beta)
    "warranty_hours":    (6, 0, 6),            # goods break within 6 h of the sale, so 6 covers all of it
}
# 11-03 23 h: reserve_frac, goods_cap_frac, review_minutes, scout_frac and hysteresis are gone. restock prices each
# unit by the demand posterior over the refill cycle it measures, and spends unit by unit, best value per won first,
# so a separate goods cap, a fixed coverage time, scouting and a hold band only overrode that ranking. The reserve
# is the refunds we may still owe, not a share of the balance. Old keys left in policy.json are ignored.
FLAGS = {
    "warranty_A": True, "warranty_B": True, "warranty_C": True,   # offer the warranty on goods of that grade
}
BANKRUPT_LINE = 500_000         # agent.md; inventory.json's bankrupt_line wins when present
ARMS = (0.70, 0.80, 0.90, 1.00) # opening ask as a share of the list price; the bandit picks one per negotiation
LAST_HOURS_BELOW_COST = 6       # in the last hours the floor may go to half the cost: unsold stock is a total loss


def defaults():
    p = {k: v[0] for k, v in NUMBERS.items()}
    p.update(FLAGS)
    p.update(updated=0, reason="initial values, 11-02", version=0)
    return p


def clip(new, old=None):
    """Keep known keys, inside their ranges and within MAX_STEP of the old value."""
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
    for k in FLAGS:
        if isinstance(new.get(k), bool):
            out[k] = new[k]
    return out


def load():
    try:
        p = json.loads(PATH.read_text(encoding="utf-8"))
        return {**defaults(), **p}
    except (OSError, ValueError):
        return defaults()


def save(p, reason):
    p = dict(p, updated=time.time(), reason=reason, version=int(p.get("version", 0)) + 1)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, PATH)
    return p


def hours_left(now=None):
    return max(0.0, (ROUND_END - (now or time.time())) / 3600)


def reserve(p, line, refunds_mean, refunds_sd):
    """Cash restock never spends: below the line the shop is bankrupt, and a warranty refund is paid from the
    balance whether or not the cash is there."""
    return (line or BANKRUPT_LINE) + refunds_mean + p["refund_sd"] * refunds_sd


# The playbook: what the manager may change that is not a number. Text guidance read by the seller's haiku
# (negotiation) and the forum's opus (forum), and categories restock leaves alone for a while. The code still
# decides every price, unit and receipt; guidance only steers how the models speak and choose among the code's
# options. No digits in guidance (it reaches haiku, which must not learn internal numbers).
PLAYBOOK = DATA / "playbook.json"
GUIDE_KEYS = ("negotiation", "forum")
GUIDE_CHARS = 600
# Words that would carry internal numbers without digits ("floor is cost plus a tenth"): guidance with them is refused.
INTERNAL = re.compile(r"\b(?:costs?|floors?|margins?|percent(?:age)?s?|per cent|half|halves|thirds?|quarters?|"
                      r"tenths?|fifths?|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundreds?|"
                      r"thousands?|millions?|dozens?)\b|%|원가|최저가|마진|퍼센트|할인율|만원|천원|반값|절반", re.I)
SKIP_MAX, SKIP_HOURS = 10, 24


def load_playbook():
    try:
        b = json.loads(PLAYBOOK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        b = {}
    return {"negotiation": "", "forum": "", "restock_skip": {}, "version": 0, "updated": 0, "reason": "", **b}


def skipped(b, now=None):
    """Categories restock leaves alone now (each skip lasts at most SKIP_HOURS)."""
    now = now or time.time()
    return {c for c, until in (b.get("restock_skip") or {}).items() if until > now}


def clip_playbook(new, old, categories, now=None):
    """Apply the manager's playbook edits that pass: guidance without digits and within GUIDE_CHARS, skips of known
    categories, at most SKIP_MAX, each for at most SKIP_HOURS. Returns (playbook, what was refused)."""
    now = now or time.time()
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
        elif len(v) > GUIDE_CHARS:
            refused.append(f"{k}: longer than {GUIDE_CHARS} characters")
        else:
            out[k] = v
    skip = new.get("restock_skip")
    if isinstance(skip, dict):
        keep = {c: t for c, t in (old.get("restock_skip") or {}).items() if t > now}
        for c, hours in skip.items():
            if c not in categories:
                refused.append(f"restock_skip: unknown category {c!r}")
                continue
            try:
                h = float(hours)
            except (TypeError, ValueError):
                refused.append(f"restock_skip: {c!r} hours not a number")
                continue
            if h <= 0:
                keep.pop(c, None)
            else:
                keep[c] = now + min(h, SKIP_HOURS) * 3600
        if len(keep) > SKIP_MAX:
            refused.append(f"restock_skip: more than {SKIP_MAX}, kept the {SKIP_MAX} ending last")
            keep = dict(sorted(keep.items(), key=lambda x: -x[1])[:SKIP_MAX])
        out["restock_skip"] = {c: round(t) for c, t in keep.items()}
    return out, refused


def save_playbook(b, reason):
    b = dict(b, updated=time.time(), reason=reason, version=int(b.get("version", 0)) + 1)
    PLAYBOOK.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLAYBOOK.with_suffix(".tmp")
    tmp.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, PLAYBOOK)
    return b
