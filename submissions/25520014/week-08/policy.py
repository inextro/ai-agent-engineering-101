"""Week 08 policy: the numbers the shop runs on, their allowed ranges, and the file they live in.

The restock hook, the seller and the forum posts read data/policy.json; only the manager writes it. Every value
has a range: the manager (opus, hourly) may move a value inside its range and at most MAX_STEP of the range per
decision, and whatever it writes is clipped here, so a bad answer cannot push the shop outside the ranges agreed
on 11-02. Posterior updates (demand, share, break rates, the price bandit) are not here; code computes them.
"""
import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", HERE / "data"))   # tests point this at a scratch folder
PATH = DATA / "policy.json"

ROUND_END = 1794927540          # 2026-11-17 23:59 KST (14:59 UTC), when Round 3 closes
MAX_STEP = 0.25                 # share of a value's range one manager decision may move it

# name: (initial, low, high). Booleans have no range and are flipped freely.
NUMBERS = {
    "reserve_frac":      (0.20, 0.15, 0.30),   # cash kept out of restock: max(frac x balance, RESERVE_FLOOR)
    "unit_cap_frac":     (0.10, 0.05, 0.20),   # one unit's cost at most this share of the balance
    "goods_cap_frac":    (0.50, 0.30, 0.70),   # goods stock at cost at most this share of the balance
    "min_margin":        (0.10, 0.05, 0.20),   # floor = cost x (1 + min_margin) + warranty premium
    "max_concessions":   (4, 2, 6),            # counters before the final price
    "c_discount":        (0.10, 0.00, 0.25),   # extra cut off the opening ask for grade C (food near its date, refurbished goods)
    "liquidate_hours":   (24, 12, 48),         # from this many hours before the end the floor falls to cost
    "half_life_h":       (24, 6, 72),          # demand memory: weight of a sale halves every this many hours
    "prior_hours":       (2, 0.5, 8),          # gamma prior worth this many hours of observation (beta)
    "hysteresis":        (0.05, 0.02, 0.10),   # hold a category from cr + h, drop it below cr - h
    "review_minutes":    (2, 1, 10),           # base stock covers demand over this many minutes
    "warranty_hours":    (6, 0, 6),            # goods break within 6 h of the sale, so 6 covers all of it
}
FLAGS = {
    "warranty_A": True, "warranty_B": True, "warranty_C": True,   # offer the warranty on goods of that grade
}
RESERVE_FLOOR = 1_000_000       # never below this, whatever reserve_frac says (bankruptcy line is 500,000)
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
        step = MAX_STEP * (hi - lo)
        v = min(max(v, old[k] - step, lo), old[k] + step, hi)
        out[k] = round(v) if isinstance(NUMBERS[k][0], int) else round(v, 4)
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


def reserve(p, balance):
    return max(p["reserve_frac"] * balance, RESERVE_FLOOR)
