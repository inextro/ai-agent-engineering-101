"""Week 08 restock hook: which units to buy, one unit at a time, by their expected profit per won on model.py's posteriors.

    python restock.py --state .market        # the kit runs it every 60 s in Round 3 and reads the last JSON line

Supply opens for 3 minutes every 20 (agent.md, 11-03; the first window at the round's start stays open 10), and
stock bought in a window has to last until the next one. Outside a window this hook prints an empty list, so the kit
does not wait 90 s per line for a window that is not coming. The kit buys at most 10 lines a run (a line is one SKU,
up to 20 units), and about two runs fit in a window.

One rule decides whether to hold a SKU and how many units (11-03 23 h per category, 11-04 11 h per SKU). The k-th
unit of SKU s, counting the units of it we already hold, is worth
    value_k = P_k x Cu - (1 - P_k) x Co,     P_k = P(we sell at least k of s within T)  (model.nb_tail)
    Cu = expected price - expected cost - warranty premium, of s itself  (expected price: model.price_ratio)
    Co = what an unsold unit costs over T: goods 1% of cost a day; food the share of its usable life T uses up
         (all of the cost when T is longer); everything at cost once the round's last liquidate_hours begin
P_k comes from our own sales rate of s: a gamma over the hours we held it in stock, around the market's sales of s
times our share (model.own_sku_posterior), plus the requests for it we missed times our close rate. Buyers order a
SKU, so each SKU is its own demand; until 11-04 11 h a category had one count of units priced at its best seller,
and a second SKU got only what the first left (quilt G5cd4ffbc, 34k a unit, none behind a 15k sheet).
T is the refill cycle: how long until this SKU gets a line again. With H SKUs to keep and L lines a window, a SKU is
refilled about every ceil(H / L) windows, so T = ceil(H / L) x the time between windows; L and that time are
measured from our own runs (data/restock.jsonl). H depends on what the money buys, so T is found by repeating the
allocation a few times.

Every unit with a positive value is ranked by value / cost (the profit a won tied up in it earns per cycle: a unit
that would sell late is worth less, so turnover counts), and units are taken from the top while the money lasts: the
cash above the reserve (the bankruptcy line plus the warranty refunds we may still owe, model.refund_exposure), each
unit counted at the top of its cost range, no unit above unit_cap_frac of the balance. Each SKU takes at most its
demand to us over the refill cycle (sku_demand x the last plan's cycle within CAP_HOURS), and a SKU nobody bought
gets none unless a buyer asked us for it (11-04: buyers settle on few SKUs, 255 of 415 fresh fruit sales one apple
box). The lines worth most go first; this
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
RUN_GAP = 300                                   # runs further apart than this are in different windows
OLD_C_H = 3                                     # a goods unit of grade C held this long is not counted as stock
# 3 h from 16:31, 0 from 19:22 (with A or B held the seller never offered C, and under the 1 h cover a fresh C took
# a place of A or B), 3 again from 11-04 20:27: 72% of goods supply is A and the seller sells A first, so B and C
# pile up behind it; uncounted, every C was bought over again in A and B (자코모 4인 G8ec51935 at 20:11: 13 A, 36 B,
# 32 C, 92M at cost; stock 175M -> 192M from 19:04). Offline, 6 h with grades, paid shares and the C fallback:
# counting C again adds 29-50M to the board in each of 4 cases (A-only buyers 20/40%, C paid 55/35%)
WINDOW_MIN, RUNS_PER_WINDOW = 20, 2             # until three windows are measured (agent.md; 11-03 logs)
WANTED_H = 2                                    # wanted posts this recent are worth one unit (wanted.py)
WINDOW_LEAD = 45                                # print the plan when a window opens within this many seconds
CAP_HOURS = (1.0, 2.0)                          # a SKU's units: its demand to us over the last plan's refill cycle,
                                                # within these hours (owner, 11-04 07:20; before, 1 h of our sales)
# A SKU this fast to us gets a line in every window (about 20 min apart, 16-24 min over 24 h on 11-04), so its stock
# only has to last FAST_COVER_H, not the refill cycle of all SKUs (about 2 h): its cap and its P_k horizon. 11-04 19 h:
# sofa G8ec51935 was bought in each of 24 runs and held 69 units (94M at cost) for 2 h; offline over 6 h a 1 h cover
# held half the stock and sold more of the high-end SKUs we never stocked (gross +5%), while 0.5 h ran out more
# than it gained (owner, 11-04 19:09).
FAST_RATE = 3.0                                 # demand to us per hour (sku_demand)
FAST_COVER_H = 1.0
GRADE_CAP = False                               # a goods SKU's B and C count only up to their share of its cap
GRADE_SHARE_H = 6                               # our goods settles this recent give the grade shares
# 11-04 21:15, offline only, not deployed (off): the seller sells the best grade first, so the cap filled with B and C left no room for A: 자코모 4인
# G8ec51935 held 13 A, 36 B, 32 C against a cap of 49 and its A sales fell 55 -> 24 -> 9 an hour (17-21 h), gross
# 35M -> 20-25M an hour. Buyers of a SKU take B and C about as often as our goods settles do (6 h: A 81%, B 13%, C 5%),
# so a B or C unit beyond that share of the cap is not stock an A buyer can use. The B and C fallbacks sell them.
# Rejected offline (6 h, 3 seeds, live restock.decide with grades): A of 자코모 4인 34 -> 38 an hour, but every
# line bought for A brings 28% B and C, so stock 124-140M -> 222-224M and board +158M -> +62M.


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


def sku_demand(est, sku):
    """Units of one SKU an hour that buyers would pay us for: our paid sales of it (model.own_sku_rate) plus the
    requests for it that found nothing at our shop (data/missed.jsonl) times our close rate. Paid sales alone stop
    when the SKU runs out, so a cap built on them stayed low: 300 of 311 requests we missed in 3 h (11-04 04-07 h)
    were SKUs we stock, mattress G1515d951 54 of them."""
    asked = est.asked_sku.get(sku, 0.0) / est.watched if est.watched > 0 else 0.0
    return est.own_sku_rate(sku) + asked * est.p_close


def cap_hours():
    """The refill cycle of the last plan in hours, within CAP_HOURS: stock bought now has to last until the
    category gets a line again (98 min on 11-04 07 h, when a 1 h cap ran the best sellers out between refills)."""
    cyc = read(STATE, {}).get("cycle_minutes")
    return min(max((cyc or 60) / 60, CAP_HOURS[0]), CAP_HOURS[1])


def old_c(u, cat, now):
    """A goods unit of grade C held OLD_C_H hours or more (food's C is its date, and food is not kept that long)."""
    return (u.get("grade") == "C" and cat.get(u.get("sku"), {}).get("kind", u.get("kind")) != "food"
            and now - (u.get("supplied_at") or now) >= OLD_C_H * 3600)


def grade_shares(est, cat, now):
    """{grade: its share of our goods settles over GRADE_SHARE_H}, B and C at least 0.02 (a SKU's cap keeps one)."""
    c = Counter()
    for ts, d in model.entries(est.db, "settle", now - GRADE_SHARE_H * 3600):
        if d.get("shop") in est.mine and cat.get(d.get("sku"), {}).get("kind") == "goods":
            c[d.get("grade")] += 1
    n = sum(c[g] for g in "ABC")
    return {g: max(c[g] / n if n else 0.0, 0.02) for g in "BC"}


def candidates(inv, cat, demand, est, p, now):
    """What each SKU's units are worth, before the horizon: {sku: facts}. Each SKU is its own demand (buyers order
    a SKU, and those of a quilt are not those of a sheet): its rate is our posterior for it (model.own_sku_posterior,
    in-stock hours) plus the requests for it we missed times our close rate, and its unit pays its own margin.
    Until 11-04 11 h a category had one count of units, priced at its best-selling SKU, so a second SKU only got
    what the first left: quilt G5cd4ffbc (34k a unit, 3.6 an hour) got none behind sheet Ge62449c9 (15k)."""
    balance = inv.get("balance") or 0
    units = [u for u in inv.get("units", []) if u.get("grade") != "expired"]
    have_sku = Counter(u.get("sku") for u in units)
    # 11-04: the seller offers the best grade held, so A and B sell and C stays; counted as stock, old C kept the
    # A and B a buyer asked for from being bought (sheet Ge62449c9: 16 C, 3 B, 0 A; 13-16 h, 15 talks that opened
    # on C were cancelled by buyers who take only A or B). A goods C unit held OLD_C_H hours is still on sale but
    # does not count toward a SKU's units.
    count_sku = Counter(u.get("sku") for u in units if not old_c(u, cat, now))
    by_grade = {}
    for u in units:
        if not old_c(u, cat, now):
            by_grade.setdefault(u.get("sku"), Counter())[u.get("grade")] += 1
    share = grade_shares(est, cat, now) if GRADE_CAP else None
    median = {d["category"]: d["median_price"] for d in demand}
    by_cat = {}
    for sku, x in cat.items():
        by_cat.setdefault(x["category"], []).append((sku, x))
    skip = policy.skipped(policy.load_playbook(), now)   # the manager's playbook: leave these alone for now
    unit_cap = p["unit_cap_frac"] * balance
    cap_h = cap_hours()
    warranty_rate = 0.0
    if p["warranty_hours"] > 0:
        rates = [est.break_rate[g] for g in "ABC" if p[f"warranty_{g}"]]
        warranty_rate = sum(rates) / len(rates) if rates else 0.0
    out = {}
    for s, x in cat.items():
        c, lp, kind = x["category"], x["list_price"], x["kind"]
        if c in skip or WORST_COST * lp > unit_cap:
            continue
        asked = s in est.asked_recent                   # a request we missed in the last 2 h (model.ASKED_RECENT_H)
        if est.sku_rate(s) <= 0 and not asked:
            continue                                    # nobody buys it (11-04: 255 of 415 fruit sales one box)
        if not asked and not have_sku.get(s) and est.unproven(s):
            continue                                    # new to the market: wait for its first sales
        rate = sku_demand(est, s)
        fast = rate >= FAST_RATE
        cap = math.ceil(rate * (min(cap_h, FAST_COVER_H) if fast else cap_h) - 1e-9)
        if asked:
            cap = max(cap, 1)                           # a buyer asked us for it
        have = count_sku.get(s, 0)
        if share and kind == "goods":
            g = by_grade.get(s, Counter())
            have = g["A"] + sum(min(g[k], math.ceil(cap * share[k] - 1e-9)) for k in "BC")
        room = min(cap - have, MAX_QTY)
        if room <= 0:
            continue
        price, cost = est.price_ratio(c) * lp, COST_SHARE[kind] * lp
        cu = price - cost - (warranty_rate * price if kind == "goods" else 0.0)
        if cu <= 0:
            continue
        a, b = est.own_sku_posterior(s)
        a += (rate - est.own_sku_rate(s)) * b           # the missed requests, at the same weight
        out[s] = {"kind": kind, "category": c, "lp": lp, "cost": cost, "worst": WORST_COST * lp, "cu": cu,
                  "life": FOOD_LIFE * (x.get("shelf_hours") or 24) if kind == "food" else None,
                  "have": have, "room": room, "a": a, "b": b}
        if fast:
            out[s]["h"] = FAST_COVER_H                  # ranked: P_k over this, not the refill cycle
    return out, units, have_sku, by_cat, median, skip


def ranked(cands, p, hours, left):
    """Every unit with a positive value over a refill cycle of `hours`: [(value per won, value, sku, k)]. The k-th
    unit of a SKU is worth P(it sells by the next refill) x its margin - P(not) x what holding it costs, per won of
    its cost: the profit a won tied up in it earns per cycle, so a unit that sells late (slow turnover) is worth less
    than one that sells at once."""
    h = min(hours, left)
    out = []
    for s, x in cands.items():
        if left < p["liquidate_hours"]:
            co = x["cost"]                                       # it may not sell before the round ends
        elif x["kind"] == "food":
            co = x["cost"] * min(1.0, h / max(x["life"], 0.1))   # it ages; past its usable life it is lost
        else:
            co = 0.01 * x["cost"] * h / 24                       # storage
        tail = model.nb_tail(x["a"], x["b"], min(h, x.get("h", h)), kmax=x["have"] + x["room"])
        for k in range(x["have"] + 1, x["have"] + x["room"] + 1):
            pk = tail[k - 1]
            value = pk * x["cu"] - (1 - pk) * co
            if value <= 0:                                       # P_k only falls with k: the rest are worse
                break
            out.append((value / x["cost"], value, s, k))
    out.sort(key=lambda u: -u[0])
    return out


def allocate(units, cands, room):
    """Take units from the top while `room` (cash, at the top cost) lasts: {sku: new units}."""
    target = Counter()
    for _, _, s, _ in units:
        if cands[s]["worst"] <= room:
            room -= cands[s]["worst"]
            target[s] += 1
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
    cands, units, have_sku, by_cat, median, skip = candidates(inv, cat, demand, est, p, now)
    sold = {k for k in have_sku if est.sku_rate(k) > 0 or est.asked_sku.get(k, 0.0) > 0}
    cycle = every_min                                       # refill cycle: start at one window, then settle
    # P_k looks at most CAP_HOURS[1] ahead: with a line per SKU the mean cycle over all SKUs kept passed 3.5 h
    # (11-04 11 h, 200 SKUs over 20 lines a window), but the lines go to the best SKUs first, so those are refilled
    # every window or two; over 3.7 h slow units looked sure to sell and the plan held 37% more units
    horizon = lambda cyc: min(cyc, 60 * CAP_HOURS[1]) / 60
    for _ in range(5):
        order = ranked(cands, p, horizon(cycle), left)
        target = allocate(order, cands, room)
        keep = len(set(target) | sold)                      # a line is one SKU: SKUs to keep refilled
        nxt = math.ceil(max(keep, 1) / lines_w) * every_min
        if nxt == cycle:
            break
        cycle = nxt
    else:                                                   # did not settle: rank once more on the last cycle
        order = ranked(cands, p, horizon(cycle), left)
        target = allocate(order, cands, room)
    # this run: money chose the target (value per won); lines are what this run is short of, so the lines worth
    # most go first
    value = Counter()
    for _, v, s, k in order:
        if k - cands[s]["have"] <= target[s]:
            value[s] += v
    buy, spent = {}, 0.0
    for s in sorted(target, key=lambda s: -value[s]):
        if len(buy) >= MAX_LINES:
            break
        x = cands[s]
        qty = min(target[s], int((room - spent) // x["worst"]))
        if qty <= 0:
            continue
        buy[s] = {"sku": s, "qty": qty, "kind": x["kind"], "category": x["category"], "worst_cost": x["worst"],
                  "k_first": x["have"] + 1, "value": round(value[s] * qty / target[s])}
        spent += qty * x["worst"]
    if left >= p["liquidate_hours"]:
        for w in asked(units, by_cat, median, skip, p, balance, cat, demand, now):
            if len(buy) < MAX_LINES and w["sku"] not in buy and spent + w["worst_cost"] <= room:
                buy[w["sku"]] = w
                spent += w["worst_cost"]
    by_c = Counter()                                        # the manager reads the target per category
    for k, n in have_sku.items():
        if k in sold:
            by_c[cat.get(k, {}).get("category")] += n
    for s, n in target.items():
        by_c[cands[s]["category"]] += n
    by_c.pop(None, None)
    plan.update(cycle_minutes=round(cycle, 1), target=dict(by_c), target_skus=len(set(target) | sold),
                stocked_categories=len({u.get("category") for u in units if u.get("sku") in sold}),
                price_ours_factor=round(est.price_ours, 3), price_market_ratio=round(est.price_market, 3),
                asked_of_us_per_h={c.split(">")[-1]: round(r, 2) for c, r in sorted(
                    est.asked_rate.items(), key=lambda t: -t[1])[:10]}, close_rate=round(est.p_close, 3),
                cap_hours=round(cap_hours(), 2),
                sku_demand_per_h={k: round(v, 2) for k, v in sorted(
                    ((k, sku_demand(est, k)) for k in have_sku), key=lambda t: -t[1])[:10]})
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
                            "lambda0": round(est.lambda0, 4), "prior_h": round(est.prior_weight_h, 4), "share": round(est.share_mean, 4)},
                           ensure_ascii=False) + "\n")
    print(f"[restock] {len(plan['target'])} categories in the plan (cycle {plan.get('cycle_minutes')} min), buying "
          f"{sum(b['qty'] for b in buy)} units in {len(buy)} lines, balance {inv.get('balance')}, reserve {plan['reserve']}")
    print(json.dumps({"buy": [{"sku": b["sku"], "qty": b["qty"]} for b in buy]}))


if __name__ == "__main__":
    main()
