"""Week 08 restock hook: which categories to hold and how many units, by a newsvendor rule on model.py's posteriors.

    python restock.py --state .market        # the kit runs it every 60 s in Round 3 and reads the last JSON line

Supply arrives within a minute, so the classic newsvendor question (how much to order for the season) becomes
"hold this category or not": a shop with no stock in a category is not in the buyer's offers for that order, and
a held category is refilled right after each sale. For each category, over a horizon of H hours:
    Cu = expected price - expected cost - warranty premium         (what a missed sale costs)
    Co = food: the cost (it rots); goods: storage over H, or the cost when the round ends inside H
    critical ratio cr = Co / (Cu + Co);  P = P(we sell at least one in H) from the gamma-Poisson predictive
Hold when P >= cr + hysteresis, keep holding while P >= cr - hysteresis (so a category does not flip on noise).
Units per held SKU: the base stock that covers the next `review_minutes` with probability cr. Busy categories get a
second and third SKU. SKUs are the ones closest to the category's median price, where an order's 0.5-1.5 x price
band most often falls.

Money rules (policy.py, the manager may move them): a reserve of max(20% of the balance, 1,000,000) is never
spent, one unit's cost stays under 10% of the balance, goods stock at cost under 50%. Bankruptcy only when the
balance is under the line and nothing is left to sell (the kit stops filing by itself once a restock hook exists).
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import httpx

import model
import policy

MARKET = "https://market.ourolabs.ai"
STATE = policy.DATA / "restock_state.json"   # held categories, for the hysteresis
LOG = policy.DATA / "restock.jsonl"
COST_SHARE = {"food": 0.50, "goods": 0.59}     # expected cost / list price over the grade mix (agent.md)
WORST_COST = 0.75                               # grade A at the top of its range: used for the caps
FOOD_LIFE = 0.20                                # expected share of shelf life a fresh supply has before grade C
GOODS_H = 48
MAX_LINES = 10


def read(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def competitors(catalog):
    """category -> how many other shops hold stock in it now, from GET /api/shops."""
    try:
        shops = httpx.get(f"{MARKET}/api/shops", timeout=15).json()["shops"]
    except Exception:
        return {}
    cat = {k: v["category"] for k, v in catalog.items()}
    out = {}
    for s in shops:
        if s.get("name") == model.SHOP_NAME:
            continue
        for c in {cat.get(k) for k, n in (s.get("stock") or {}).items() if n}:
            if c:
                out[c] = out.get(c, 0) + 1
    return out


def decide(inv, cat, demand, est, p, held, now):
    balance = inv.get("balance") or 0
    left = policy.hours_left(now)
    units = [u for u in inv.get("units", []) if u.get("grade") != "expired"]
    have = {}
    for u in units:
        have[u["sku"]] = have.get(u["sku"], 0) + 1
    goods_at_cost = sum(u["cost"] for u in units if u.get("kind") == "goods")
    median = {d["category"]: d["median_price"] for d in demand}
    by_cat = {}
    for sku, x in cat.items():
        by_cat.setdefault(x["category"], []).append((sku, x))

    plans, new_held = [], set()
    for c, skus in by_cat.items():
        kind = skus[0][1]["kind"]
        m = median.get(c) or sorted(x["list_price"] for _, x in skus)[len(skus) // 2]
        unit_cap = p["unit_cap_frac"] * balance
        skus = sorted((s for s in skus if WORST_COST * s[1]["list_price"] <= unit_cap),
                      key=lambda s: abs(math.log(s[1]["list_price"] / m)))
        if not skus:
            continue
        lp = skus[0][1]["list_price"]
        price = 0.85 * lp
        cost = COST_SHARE[kind] * lp
        premium = 0.0
        if kind == "goods" and p["warranty_hours"] > 0:
            rates = [est.break_rate[g] for g in "ABC" if p[f"warranty_{g}"]]
            premium = (sum(rates) / len(rates) if rates else 0) * price
        cu = price - cost - premium
        if kind == "food":
            h = min(FOOD_LIFE * (skus[0][1].get("shelf_hours") or 24), left)
            co = cost
        else:
            h = min(GOODS_H, left)
            co = cost if left < GOODS_H or left < p["liquidate_hours"] else 0.01 * cost * h / 24
        if cu <= 0 or h <= 0:
            continue
        cr = co / (cu + co)
        prob = 1 - est.p_none(c, h)
        keep = c in held
        if not (prob >= cr + p["hysteresis"] or (keep and prob >= cr - p["hysteresis"])):
            continue
        new_held.add(c)
        expected = est.share(c) * est.rate(c) * h
        n_sku = 1 + (expected >= 3) + (expected >= 10)
        q = est.base_stock(c, p["review_minutes"], max(cr, 0.5))
        value = prob * cu - (1 - prob) * co
        for sku, x in skus[:n_sku]:
            need = min(q - have.get(sku, 0), 20)
            if need > 0:
                plans.append({"value": value, "sku": sku, "qty": need, "kind": kind, "category": c,
                              "unit_cost": COST_SHARE[kind] * x["list_price"], "cr": round(cr, 3),
                              "p": round(prob, 3)})

    spend = balance - policy.reserve(p, balance)
    goods_room = p["goods_cap_frac"] * balance - goods_at_cost
    buy = []
    for pl in sorted(plans, key=lambda x: -x["value"]):
        if len(buy) >= MAX_LINES:
            break
        qty = pl["qty"]
        while qty and qty * pl["unit_cost"] > spend:
            qty -= 1
        if pl["kind"] == "goods":
            while qty and qty * pl["unit_cost"] > goods_room:
                qty -= 1
        if not qty:
            continue
        spend -= qty * pl["unit_cost"]
        if pl["kind"] == "goods":
            goods_room -= qty * pl["unit_cost"]
        buy.append(dict(pl, qty=qty))
    return buy, new_held


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=".market")
    a = ap.parse_args()
    md = Path(a.state)
    inv = read(md / "inventory.json", {})
    out = {"buy": []}
    if inv.get("round", 0) < 3:
        print(json.dumps(out))
        return
    stuck = (inv.get("balance") or 0) < inv.get("bankrupt_line", 0) and (inv.get("bankruptcies_left") or 0) > 0
    if stuck and not [u for u in inv.get("units", []) if u.get("grade") != "expired"]:
        print("[restock] below the bankruptcy line with nothing to sell: filing")
        print(json.dumps({"buy": [], "bankrupt": True}))
        return
    now = time.time()
    p = policy.load()
    cat = read(md / "catalog.json", {})
    demand = read(md / "demand.json", [])
    st = read(STATE, {"held": []})
    try:
        est = model.Estimates(pol=p, now=now, competitors=competitors(cat))
    except Exception as e:                       # no archive yet: hold nothing new, keep what we have
        print(f"[restock] estimates unavailable ({type(e).__name__}: {e}); no purchase", file=sys.stderr)
        print(json.dumps(out))
        return
    buy, held = decide(inv, cat, demand, est, p, set(st["held"]), now)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"held": sorted(held), "ts": now}, ensure_ascii=False), encoding="utf-8")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "balance": inv.get("balance"), "held": len(held), "buy": buy,
                            "lambda0": round(est.lambda0, 4), "share": round(est.share_mean, 4)},
                           ensure_ascii=False) + "\n")
    print(f"[restock] {len(held)} categories held, buying {sum(b['qty'] for b in buy)} units in {len(buy)} lines, "
          f"balance {inv.get('balance')}")
    print(json.dumps({"buy": [{"sku": b["sku"], "qty": b["qty"]} for b in buy]}))


if __name__ == "__main__":
    main()
