"""Week 08 restock hook: which units to buy, one unit at a time, by their expected profit per won on model.py's posteriors.

    python restock.py --state .market        # the kit runs it every 60 s in Round 3 and reads the last JSON line

Supply opens for 3 minutes every 20 (agent.md, 11-03; the first window at the round's start stays open 10), and
stock bought in a window has to last until the next one. Outside a window this hook prints an empty list, so the kit
does not wait 90 s per line for a window that is not coming. The kit buys at most 10 lines a run (a line is one SKU,
up to 20 units), and about two runs fit in a window.

One rule decides both whether to hold a category and how many units (11-03 23 h; before, a hold test over 48 h and a
base stock over 25 minutes disagreed, every line bought one unit, and 101 of 138 held categories sat empty). The k-th
unit of category c, counting the units we already hold there, is worth
    value_k = P_k x Cu - (1 - P_k) x Co,     P_k = P(we sell at least k in c within T)  (model.tail)
    Cu = expected price - expected cost - warranty premium    (expected price: model.price_ratio, from the ledger)
    Co = what an unsold unit costs over T: goods 1% of cost a day; food the share of its usable life T uses up
         (all of the cost when T is longer); everything at cost once the round's last liquidate_hours begin
T is the refill cycle: how long until this category gets a line again. With H categories to keep and L lines a
window, a category is refilled about every ceil(H / L) windows, so T = ceil(H / L) x the time between windows; L and
that time are measured from our own runs (data/restock.jsonl). H depends on what the money buys, so T is found by
repeating the allocation a few times.

Every unit with a positive value, in all categories, is ranked by value / cost, and units are taken from the top
while the money lasts: the cash above the reserve (the bankruptcy line plus the warranty refunds we may still owe,
model.refund_exposure), each unit counted at the top of its cost range, no unit above unit_cap_frac of the balance.
A category whose first unit does not make the cut is not held; a busy one gets several units on one line, and a
second and third SKU (orders accept SKUs priced 0.5-1.5 x a reference, so one SKU does not fit every order). This
run buys the best of those within the kit's 10 lines. A category a forum post asked for in the last two hours
(wanted.py) gets one unit when we hold none and a line is left. Categories the manager's playbook skips (at most
24 h each) are not bought; their stock stays on sale. Bankruptcy only when the balance is under the line and nothing
is left to sell (the kit stops filing by itself once a restock hook exists).
"""
import argparse
import json
import math
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

import model
import policy
import wanted

MARKET = "https://market.ourolabs.ai"
STATE = policy.DATA / "restock_state.json"   # the plan (target per category, cycle), for the manager
LOG = policy.DATA / "restock.jsonl"
COST_SHARE = {"food": 0.50, "goods": 0.59}     # expected cost / list price over the grade mix (agent.md)
WORST_COST = 0.75                               # grade A at the top of its range: used for the caps
FOOD_LIFE = 0.20                                # expected share of shelf life a fresh supply has before grade C
MAX_LINES = 10                                  # the kit's lines per run
MAX_QTY = 20                                    # the kit's units per line
UNITS_MAX = 40                                  # units of one category the plan looks at
RUN_GAP = 300                                   # runs further apart than this are in different windows
WINDOW_MIN, RUNS_PER_WINDOW = 20, 2             # until three windows are measured (agent.md; 11-03 logs)
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


def cadence(now, path=LOG):
    """(lines per window, minutes between windows), measured from our runs of the last 3 hours: runs closer than
    RUN_GAP are one window. Defaults until three full windows are in the log."""
    ts = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                t = json.loads(line).get("ts", 0)
                if t > now - 3 * 3600:
                    ts.append(t)
    except (OSError, ValueError):
        pass
    groups = []
    for t in sorted(ts):
        if groups and t - groups[-1][-1] <= RUN_GAP:
            groups[-1].append(t)
        else:
            groups.append([t])
    full = groups[:-1]                           # the newest window may still be running
    if len(full) < 3:
        return MAX_LINES * RUNS_PER_WINDOW, WINDOW_MIN
    runs = statistics.median(len(g) for g in full[-6:])
    gap = statistics.median(b[0] - a[0] for a, b in zip(full[-7:], full[-6:])) / 60
    return MAX_LINES * max(1, round(runs)), min(max(gap, 10), 60)


def asked(units, by_cat, median, skip, p, balance, cat, demand, now):
    """One unit of each category a wanted post asked for in the last WANTED_H hours that we hold none of: the SKU
    the post named, else the one nearest the median price. After the ranked units, when a line is left."""
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
        out.append({"sku": sku, "qty": 1, "kind": x["kind"], "category": c, "asked": w["id"],
                    "worst_cost": WORST_COST * x["list_price"]})
    return out


def categories(inv, cat, demand, est, p, now):
    """What each category's units are worth, before the horizon: {category: facts}, and the stock per SKU."""
    balance = inv.get("balance") or 0
    units = [u for u in inv.get("units", []) if u.get("grade") != "expired"]
    have_cat, have_sku = Counter(u.get("category") for u in units), Counter(u.get("sku") for u in units)
    median = {d["category"]: d["median_price"] for d in demand}
    by_cat = {}
    for sku, x in cat.items():
        by_cat.setdefault(x["category"], []).append((sku, x))
    skip = policy.skipped(policy.load_playbook(), now)   # the manager's playbook: leave these alone for now
    unit_cap = p["unit_cap_frac"] * balance
    out = {}
    for c, skus in by_cat.items():
        if c in skip:
            continue
        kind = skus[0][1]["kind"]
        m = median.get(c) or sorted(x["list_price"] for _, x in skus)[len(skus) // 2]
        fit = sorted((s for s in skus if WORST_COST * s[1]["list_price"] <= unit_cap),
                     key=lambda s: abs(math.log(s[1]["list_price"] / m)))   # nearest the median price first
        if not fit:
            continue
        lp = fit[0][1]["list_price"]
        price, cost = est.price_ratio(c) * lp, COST_SHARE[kind] * lp
        premium = 0.0
        if kind == "goods" and p["warranty_hours"] > 0:
            rates = [est.break_rate[g] for g in "ABC" if p[f"warranty_{g}"]]
            premium = (sum(rates) / len(rates) if rates else 0) * price
        cu = price - cost - premium
        if cu <= 0:
            continue
        life = FOOD_LIFE * (fit[0][1].get("shelf_hours") or 24) if kind == "food" else None
        out[c] = {"kind": kind, "skus": fit, "lp": lp, "cost": cost, "worst": WORST_COST * lp, "cu": cu,
                  "life": life, "have": have_cat.get(c, 0)}
    return out, units, have_sku, by_cat, median, skip


def ranked(cands, est, p, hours, left):
    """Every unit with a positive value over a refill cycle of `hours`: [(value per won, value, category, k)]."""
    h = min(hours, left)
    out = []
    for c, x in cands.items():
        if left < p["liquidate_hours"]:
            co = x["cost"]                                       # it may not sell before the round ends
        elif x["kind"] == "food":
            co = x["cost"] * min(1.0, h / max(x["life"], 0.1))   # it ages; past its usable life it is lost
        else:
            co = 0.01 * x["cost"] * h / 24                       # storage
        tail = est.tail(c, h, kmax=x["have"] + UNITS_MAX)
        for k in range(x["have"] + 1, len(tail) + 1):
            pk = tail[k - 1]
            value = pk * x["cu"] - (1 - pk) * co
            if value <= 0:                                       # P_k only falls with k: the rest are worse
                break
            out.append((value / x["cost"], value, c, k))
    out.sort(key=lambda u: -u[0])
    return out


def allocate(units, cands, room):
    """Take units from the top while `room` (cash, at the top cost) lasts: {category: new units}."""
    target = Counter()
    for _, _, c, _ in units:
        if cands[c]["worst"] <= room:
            room -= cands[c]["worst"]
            target[c] += 1
    return target


def decide(inv, cat, demand, est, p, now, lines=None, every=None):
    """(the lines to buy now, the plan) for one run."""
    balance = inv.get("balance") or 0
    left = policy.hours_left(now)
    lines_w, every_min = (lines, every) if lines else cadence(now)
    refunds = est.refund_exposure(p["warranty_hours"] or 6)
    reserve = policy.reserve(p, inv.get("bankrupt_line"), *refunds)
    room = balance - reserve
    plan = {"lines_per_window": lines_w, "window_minutes": round(every_min, 1), "reserve": round(reserve),
            "refunds_expected": round(refunds[0]), "refunds_sd": round(refunds[1]), "cash_to_spend": round(room)}
    if left < policy.LAST_HOURS_BELOW_COST or room <= 0:   # the last hours sell below cost: buy nothing
        return [], dict(plan, target={}, cycle_minutes=None)
    cands, units, have_sku, by_cat, median, skip = categories(inv, cat, demand, est, p, now)
    cycle = every_min                                       # refill cycle: start at one window, then settle
    for _ in range(5):
        order = ranked(cands, est, p, cycle / 60, left)
        target = allocate(order, cands, room)
        keep = len(set(target) | {c for c, x in cands.items() if x["have"]})
        nxt = math.ceil(max(keep, 1) / lines_w) * every_min
        if nxt == cycle:
            break
        cycle = nxt
    else:                                                   # did not settle: rank once more on the last cycle
        order = ranked(cands, est, p, cycle / 60, left)
        target = allocate(order, cands, room)
    # this run: money chose the target (value per won); lines are what this run is short of, so the target's units
    # go into lines, one SKU each (a category's units spread over its first SKUs), and the lines worth most go first
    value = {(c, k): v for _, v, c, k in order}
    lines = {}
    for c, n in target.items():
        x = cands[c]
        n_sku = min(len(x["skus"]), 1 + (n >= 3) + (n >= 10))
        for i in range(n):
            k = x["have"] + i + 1
            sku = x["skus"][i % n_sku][0]
            line = lines.setdefault(sku, {"sku": sku, "qty": 0, "kind": x["kind"], "category": c,
                                          "worst_cost": x["worst"], "k_first": k, "value": 0.0})
            if line["qty"] < MAX_QTY:
                line["qty"] += 1
                line["value"] += value[(c, k)]
    buy, spent = {}, 0.0
    for line in sorted(lines.values(), key=lambda l: -l["value"]):
        if len(buy) >= MAX_LINES:
            break
        qty = min(line["qty"], int((room - spent) // line["worst_cost"]))
        if qty <= 0:
            continue
        buy[line["sku"]] = dict(line, qty=qty, value=round(line["value"] * qty / line["qty"]))
        spent += qty * line["worst_cost"]
    if left >= p["liquidate_hours"]:
        for w in asked(units, by_cat, median, skip, p, balance, cat, demand, now):
            if len(buy) < MAX_LINES and w["sku"] not in buy and spent + w["worst_cost"] <= room:
                buy[w["sku"]] = w
                spent += w["worst_cost"]
    plan.update(cycle_minutes=round(cycle, 1), target={c: x["have"] + target[c] for c, x in cands.items()
                                             if x["have"] + target[c] > 0},
                stocked_categories=sum(1 for x in cands.values() if x["have"]),
                price_ours_factor=round(est.price_ours, 3), price_market_ratio=round(est.price_market, 3))
    return list(buy.values()), plan


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
    try:
        est = model.Estimates(pol=p, now=now, competitors=competitors(cat), cat=cat)
    except Exception as e:                       # no archive (collector down): buy nothing, the stock stays on sale
        print(f"[restock] estimates unavailable ({type(e).__name__}: {e}); buying nothing", file=sys.stderr)
        print(json.dumps(out))
        return
    buy, plan = decide(inv, cat, demand, est, p, now)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"held": sorted(plan["target"]), "ts": now, **plan}, ensure_ascii=False),
                     encoding="utf-8")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "balance": inv.get("balance"), "held": len(plan["target"]), "buy": buy,
                            "plan": {k: v for k, v in plan.items() if k != "target"},
                            "lambda0": round(est.lambda0, 4), "share": round(est.share_mean, 4)},
                           ensure_ascii=False) + "\n")
    print(f"[restock] {len(plan['target'])} categories in the plan (cycle {plan.get('cycle_minutes')} min), buying "
          f"{sum(b['qty'] for b in buy)} units in {len(buy)} lines, balance {inv.get('balance')}, reserve {plan['reserve']}")
    print(json.dumps({"buy": [{"sku": b["sku"], "qty": b["qty"]} for b in buy]}))


if __name__ == "__main__":
    main()
