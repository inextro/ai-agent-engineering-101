"""Week 08 restock hook: which categories to hold and how many units, by a newsvendor rule on model.py's posteriors.

    python restock.py --state .market        # the kit runs it every 60 s in Round 3 and reads the last JSON line

Supply opens for 3 minutes every 20 (agent.md, 11-03; the first window at the round's start stays open 10), and
stock bought in a window has to last until the next one. Outside a window this hook prints an empty list, so the kit
does not wait 90 s per line for a window that is not coming; the kit buys at most 10 lines a run, so about 20 lines
per window. Within that, the classic newsvendor question (how much to order for the season) becomes
"hold this category or not": a shop with no stock in a category is not in the buyer's offers for that order, and
a held category is refilled at each window. For each category, over a horizon of H hours:
    Cu = expected price - expected cost - warranty premium         (what a missed sale costs)
    Co = food: the cost (it rots); goods: storage over H, or the cost when the round ends inside H
    critical ratio cr = Co / (Cu + Co);  P = P(we sell at least one in H) from the gamma-Poisson predictive
Hold when P >= cr + hysteresis, keep holding while P >= cr - hysteresis (so a category does not flip on noise).
Units per held SKU: the base stock that covers the next `review_minutes` with probability cr. Busy categories get a
second and third SKU. SKUs are the ones closest to the category's median price, where an order's 0.5-1.5 x price
band most often falls. While the ledger holds fewer than SCOUT_SETTLES settles (the round's start), the shop also
scouts: one unit in every category it has none of, up to scout_frac of the balance. A category a forum post asked for in the last two hours (wanted.py) gets one
unit when we hold none, after the newsvendor's lines and before scouting. Categories the manager's playbook skips (at most 24 h each) are not bought; their stock stays
on sale.

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
import wanted

MARKET = "https://market.ourolabs.ai"
STATE = policy.DATA / "restock_state.json"   # held categories, for the hysteresis
LOG = policy.DATA / "restock.jsonl"
COST_SHARE = {"food": 0.50, "goods": 0.59}     # expected cost / list price over the grade mix (agent.md)
WORST_COST = 0.75                               # grade A at the top of its range: used for the caps
FOOD_LIFE = 0.20                                # expected share of shelf life a fresh supply has before grade C
GOODS_H = 48
MAX_LINES = 10
SCOUT_SETTLES = 300                             # market settles in the window below which the shop scouts
SCOUT_MIN_SHELF = 72                            # food that keeps less than this many hours is not scouted
WANTED_H = 2                                    # wanted posts this recent are worth one unit (wanted.py)
WINDOW_LEAD = 45                                # print the plan when a window opens within this many seconds


def window_shut(inv, now):
    """True when supply is closed and will not open before the kit's next run (inventory.json `restock`)."""
    r = inv.get("restock") or {}
    if not r or r.get("open", True):
        return False
    at = r.get("opens_at") or 0
    if at > 1e12:                                # milliseconds
        at /= 1000
    return at - now > WINDOW_LEAD


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


def asked(units, by_cat, median, skip, p, balance, cat, demand, now):
    """One unit of each category a wanted post asked for in the last WANTED_H hours that we hold none of: the SKU
    the post named, else the one nearest the median price. After the newsvendor's lines, before scouting."""
    try:
        posts = wanted.recent(model.connect(), cat, demand, now - WANTED_H * 3600)
    except Exception:
        return []
    stocked = {u.get("category") for u in units}
    out, seen = [], set()
    for w in posts:
        c = w["category"]
        if c in stocked or c in skip or c in seen or c not in by_cat:
            continue
        seen.add(c)
        skus = [s for s in by_cat[c] if WORST_COST * s[1]["list_price"] <= p["unit_cap_frac"] * balance]
        named = [s for s in skus if s[0] in w["skus"]]
        if not skus:
            continue
        m = median.get(c) or sorted(x["list_price"] for _, x in skus)[len(skus) // 2]
        sku, x = named[0] if named else min(skus, key=lambda s: abs(math.log(s[1]["list_price"] / m)))
        out.append({"value": -0.5, "sku": sku, "qty": 1, "kind": x["kind"], "category": c, "asked": w["id"],
                    "unit_cost": COST_SHARE[x["kind"]] * x["list_price"], "worst_cost": WORST_COST * x["list_price"]})
    return out


def scout(units, by_cat, median, skip, p, balance):
    """While the ledger is too thin to say what sells (the start of the round), hold one unit in every category
    we have none of: a shop with no stock in a category is not shown for its orders, and a sale of our own is the
    best evidence there is. The SKU nearest the category's median price (most likely inside an order's 0.5-1.5 x
    band), cheapest categories first, until the stock at cost reaches scout_frac of the balance. They come after
    the newsvendor's lines and fill MAX_LINES a minute, so the shop covers most categories in a quarter of an hour."""
    stocked = {u.get("category") for u in units}
    room = p["scout_frac"] * balance - sum(u["cost"] for u in units)
    out = []
    for c, skus in by_cat.items():
        if c in stocked or c in skip:
            continue
        m = median.get(c) or sorted(x["list_price"] for _, x in skus)[len(skus) // 2]
        fit = [s for s in skus if WORST_COST * s[1]["list_price"] <= p["unit_cap_frac"] * balance
               and (s[1]["kind"] != "food" or (s[1].get("shelf_hours") or 0) >= SCOUT_MIN_SHELF)]
        if not fit:
            continue
        sku, x = min(fit, key=lambda s: abs(math.log(s[1]["list_price"] / m)))
        out.append({"value": -1, "sku": sku, "qty": 1, "kind": x["kind"], "category": c, "scout": True,
                    "unit_cost": COST_SHARE[x["kind"]] * x["list_price"], "worst_cost": WORST_COST * x["list_price"]})
    out.sort(key=lambda pl: pl["unit_cost"])
    keep = []
    for pl in out:
        if pl["worst_cost"] > room:
            break
        room -= pl["worst_cost"]
        keep.append(dict(pl, value=-1 - pl["unit_cost"] / 1e9))   # after every newsvendor line, cheapest first
    return keep


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
    skip = policy.skipped(policy.load_playbook(), now)   # the manager's playbook: leave these alone for now
    thin = len(est.sales) < SCOUT_SETTLES
    for c, skus in by_cat.items():
        if c in skip or left < policy.LAST_HOURS_BELOW_COST:    # the last hours sell below cost: buy nothing
            continue
        if thin and not est.w[c]:   # thin ledger: an unseen category is only scouted (the pooled prior over a few
            continue                # minutes made every category look like average demand and held them all)
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
                              "unit_cost": COST_SHARE[kind] * x["list_price"],
                              "worst_cost": WORST_COST * x["list_price"], "cr": round(cr, 3),
                              "p": round(prob, 3)})

    if thin and left >= p["liquidate_hours"]:
        plans += scout(units, by_cat, median, skip, p, balance)
    if left >= p["liquidate_hours"]:
        plans += asked(units, by_cat, median, skip, p, balance, cat, demand, now)

    spend = balance - policy.reserve(p, balance)
    goods_room = p["goods_cap_frac"] * balance - goods_at_cost
    buy = []
    for pl in sorted(plans, key=lambda x: (x.get("scout", False), -x["value"])):   # scouting after every refill
        if len(buy) >= MAX_LINES:
            break
        qty, worst = pl["qty"], pl["worst_cost"]   # the caps hold even when every unit comes in at the top cost
        while qty and qty * worst > spend:
            qty -= 1
        if pl["kind"] == "goods":
            while qty and qty * worst > goods_room:
                qty -= 1
        if not qty:
            continue
        spend -= qty * worst
        if pl["kind"] == "goods":
            goods_room -= qty * worst
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
    if window_shut(inv, now):
        print(json.dumps(out))
        return
    p = policy.load()
    cat = read(md / "catalog.json", {})
    demand = read(md / "demand.json", [])
    st = read(STATE, {"held": []})
    try:
        est = model.Estimates(pol=p, now=now, competitors=competitors(cat), cat=cat)
    except Exception as e:                       # no archive (collector down): scout only, so the shop is not empty
        print(f"[restock] estimates unavailable ({type(e).__name__}: {e}); scouting only", file=sys.stderr)
        by_cat = {}
        for sku, x in cat.items():
            by_cat.setdefault(x["category"], []).append((sku, x))
        units = [u for u in inv.get("units", []) if u.get("grade") != "expired"]
        plans = scout(units, by_cat, {d["category"]: d["median_price"] for d in demand},
                      policy.skipped(policy.load_playbook(), now), p, inv.get("balance") or 0)
        print(json.dumps({"buy": [{"sku": b["sku"], "qty": 1} for b in plans[:MAX_LINES]]}))
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
