"""Week 08 clearance: stock bought before the demand fix goes cheaper now, where the market buys it.

Restock bought on a flat demand prior until 11-03 23:55 KST (5b66120) and filled the shelf with categories that hardly
sell. Unsold stock counts as a total loss at the end and costs 1% of its cost a day in storage, so (owner, 11-04):
  - a unit supplied before FIX_TS whose category other shops sell MIN_RATE to MAX_RATE times an hour (real demand,
    competitors to win it from, but not so much that it sells anyway at the usual price) opens at the market's median share of the list price for that category less
    ASK_UNDER, and its floor drops to FLOOR_COST x cost: the next order of that category should find us cheapest;
  - in a category with no such demand nothing changes: no order came yet, a lower price would not bring one.
Since 11-04 16:50 (owner: unsold goods only cost storage, sell them cheaper) any goods unit held STALE_H hours or
more is clearance too, whenever it was bought and however fast its category sells: it opens at the market's median
share of the list price for its SKU over the last day (its category's when the SKU has fewer than MIN_SKU_SETTLES
settles by other shops), less ASK_UNDER, or at NO_DATA_ASK x cost with no settles at all, and grade C less the
policy's c_discount on top (the SKU's median is mostly grade A sales); its floor is STALE_FLOOR x that ask, never
under FLOOR_COST x cost (with FLOOR_COST x cost alone a grade C sofa costing 21만 that the market buys near 40만
could be talked down to 19만). 13-16 h: 30 SKUs held 2 h+ had no sale of ours in 6 h; their floor (cost x 1.08 + warranty) sat at or over
the market's prices (TV G7f82f04e 23.9만 asked, market 20-26만; carbon mat floor 7.9만, buyer offered 7.5만), and
once sold restock does not buy them again (first unit ranked 624-748 of 752 by value per won).
Negotiations that offer a clearance unit are logged with arm "clearance", so the price bandit (model.bandit_stats)
leaves them out.

The table is rebuilt from the collector's ledger at most every REFRESH seconds and written to data/clearance.json
for the managers and the owner.
"""
import json
import time

import model
import policy

FIX_TS = 1793717700       # 11-03 23:55 KST, the demand prior fix went live
MIN_RATE = 1.0            # other shops' settles per hour in the category: below this, no clearance
MAX_RATE = 10.0           # above this the category sells anyway at the usual price: no clearance (owner, 11-04)
ASK_UNDER = 0.05          # open this far under the market's median share of the list price
FLOOR_COST = 0.90         # floor as a share of the unit's cost
STALE_H = 6               # a goods unit held this long is clearance (11-04 16:50)
MIN_SKU_SETTLES = 3       # other shops' settles of the SKU in the last day for its own median; else the category's
NO_DATA_ASK = 1.10        # opening ask as a share of cost when nobody sold the SKU or its category in a day
STALE_FLOOR = 0.90        # a stale unit's floor as a share of its clearance ask (never under FLOOR_COST x cost)
REFRESH = 600
DAY = 86400
PATH = policy.DATA / "clearance.json"

_table, _built = {}, 0.0
_stale = {"sku": {}, "category": {}}


def build(now=None):
    """{category: {"rate": other shops' settles/h, "median": median paid / list}} for categories between MIN_RATE and
    MAX_RATE, and sets _stale: the median paid / list per SKU (MIN_SKU_SETTLES or more) and per category, any rate."""
    global _stale
    now = now or time.time()
    db = model.connect()
    cat = model.catalog(db)
    ours = model.our_urls(db)
    since = max(now - DAY, model.round_start(db) or now)
    hours = max((now - since) / 3600, 0.5)
    n, pct, by_sku = {}, {}, {}
    for _, d in model.entries(db, "settle", since):
        x = cat.get(model.sku_of(d)) or {}
        price = model.amount_of(d)
        if d.get("shop") in ours or not x.get("category") or not price or not x.get("list_price"):
            continue
        c = x["category"]
        n[c] = n.get(c, 0) + 1
        pct.setdefault(c, []).append(price / x["list_price"])
        by_sku.setdefault(model.sku_of(d), []).append(price / x["list_price"])
    med = lambda v: sorted(v)[len(v) // 2]
    _stale = {"sku": {k: round(med(v), 3) for k, v in by_sku.items() if len(v) >= MIN_SKU_SETTLES},
              "category": {c: round(med(v), 3) for c, v in pct.items()}}
    out = {}
    for c, k in n.items():
        if MIN_RATE <= k / hours <= MAX_RATE:
            p = sorted(pct[c])
            out[c] = {"rate": round(k / hours, 2), "median": round(p[len(p) // 2], 3)}
    return out


def table():
    global _table, _built
    now = time.time()
    if now - _built >= REFRESH:
        try:
            _table = build(now)
            PATH.write_text(json.dumps({"ts": now, "fix_ts": FIX_TS, "min_rate": MIN_RATE, "max_rate": MAX_RATE, "ask_under": ASK_UNDER,
                                        "floor_cost": FLOOR_COST, "categories": _table, "stale_h": STALE_H,
                                        "stale_median_sku": _stale["sku"]}, ensure_ascii=False),
                            encoding="utf-8")
        except Exception as e:             # a failed rebuild keeps the last table; the seller must never stop on it
            print(f"[clearance] rebuild failed: {type(e).__name__}: {str(e)[:160]}", flush=True)
        _built = now
    return _table


def stale(u, now=None):
    """A goods unit held STALE_H hours or more."""
    return (u.get("kind", "goods") == "goods" and u.get("grade") != "expired"
            and (now or time.time()) - (u.get("supplied_at") or now or time.time()) >= STALE_H * 3600)


def terms(u, now=None, c_discount=0.0):
    """{"ask": price, "floor": price} for a clearance unit, None for any other."""
    if (u.get("supplied_at") or FIX_TS) < FIX_TS:
        t = table().get(u.get("category"))
        if t:
            return {"ask": u["list_price"] * max(t["median"] - ASK_UNDER, 0.05), "floor": FLOOR_COST * u["cost"]}
    if not stale(u, now):
        return None
    table()
    share = _stale["sku"].get(u.get("sku")) or _stale["category"].get(u.get("category"))
    ask = u["list_price"] * max(share - ASK_UNDER, 0.05) if share else NO_DATA_ASK * u["cost"]
    if u.get("grade") == "C":
        ask *= 1 - c_discount
    return {"ask": ask, "floor": max(FLOOR_COST * (u.get("_pool_cost") or u["cost"]), STALE_FLOOR * ask)}
