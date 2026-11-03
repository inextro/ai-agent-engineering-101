"""Week 08 clearance: stock bought before the demand fix goes cheaper now, where the market buys it.

Restock bought on a flat demand prior until 11-03 23:55 KST (5b66120) and filled the shelf with categories that hardly
sell. Unsold stock counts as a total loss at the end and costs 1% of its cost a day in storage, so (owner, 11-04):
  - a unit supplied before FIX_TS whose category other shops sell MIN_RATE to MAX_RATE times an hour (real demand,
    competitors to win it from, but not so much that it sells anyway at the usual price) opens at the market's median share of the list price for that category less
    ASK_UNDER, and its floor drops to FLOOR_COST x cost: the next order of that category should find us cheapest;
  - in a category with no such demand nothing changes: no order came yet, a lower price would not bring one.
Stock bought after the fix is judged later from its own record, not here. Negotiations that offer a clearance unit
are logged with arm "clearance", so the price bandit (model.bandit_stats) leaves them out.

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
REFRESH = 600
DAY = 86400
PATH = policy.DATA / "clearance.json"

_table, _built = {}, 0.0


def build(now=None):
    """{category: {"rate": other shops' settles/h, "median": median paid / list}} for categories between MIN_RATE and MAX_RATE."""
    now = now or time.time()
    db = model.connect()
    cat = model.catalog(db)
    ours = model.our_urls(db)
    since = max(now - DAY, model.round_start(db) or now)
    hours = max((now - since) / 3600, 0.5)
    n, pct = {}, {}
    for _, d in model.entries(db, "settle", since):
        x = cat.get(model.sku_of(d)) or {}
        price = model.amount_of(d)
        if d.get("shop") in ours or not x.get("category") or not price or not x.get("list_price"):
            continue
        c = x["category"]
        n[c] = n.get(c, 0) + 1
        pct.setdefault(c, []).append(price / x["list_price"])
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
                                        "floor_cost": FLOOR_COST, "categories": _table}, ensure_ascii=False),
                            encoding="utf-8")
        except Exception as e:             # a failed rebuild keeps the last table; the seller must never stop on it
            print(f"[clearance] rebuild failed: {type(e).__name__}: {str(e)[:160]}", flush=True)
        _built = now
    return _table


def terms(u):
    """{"ask": price, "floor": price} for a clearance unit, None for any other."""
    if (u.get("supplied_at") or FIX_TS) >= FIX_TS:
        return None
    t = table().get(u.get("category"))
    if not t:
        return None
    return {"ask": u["list_price"] * max(t["median"] - ASK_UNDER, 0.05), "floor": FLOOR_COST * u["cost"]}
