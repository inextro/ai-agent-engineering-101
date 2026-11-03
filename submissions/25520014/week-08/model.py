"""Week 08 model: the posteriors the shop decides with, computed by code from the collector's SQLite archive.

    python model.py            # print the current estimates (what restock and the manager see)

Demand, per catalog category: settled deals arrive as a Poisson process with an unknown rate (deals per hour,
whole market). The rate gets a gamma prior; the gamma is conjugate to the Poisson, so the posterior is two sums:
    alpha = alpha0 + sum of w_i            (w_i = 0.5 ** (age_i / half_life): old sales count less)
    beta  = beta0  + discounted exposure   (the hours watched, discounted the same way)
The prior is the same for all 140 categories (orders pick a category, not a SKU): rate lambda0 = the pooled
discounted rate / 140, worth `prior_hours` of observation (beta0). A category nobody sold in yet keeps lambda0.

Our share of a category's deals: Beta, prior mean 1 / (shops holding stock in that category, market average),
weight 5, updated with our deals against everyone's deals in the categories we sold in. One share for the shop.

Then for a horizon of H hours, with our rate s * lambda, the gamma-Poisson predictive count is negative binomial:
    P(we sell none in H) = (beta / (beta + s H)) ** alpha,   and tail(c, H)[k-1] = P(we sell at least k in H)
which restock.py uses to price the k-th unit of a category (11-03: one rule for whether to hold and how many).

Sale prices, per category (what restock expects a unit to sell for; the fixed 0.85 x list it used before was a
guess): r = price paid / list price of the other shops' settled deals, a category's weighted mean blended with the
market's mean as if the market were PRICE_PRIOR_W more deals, times one factor for our shop: what our deals got over
their category's ratio, blended with 1 the same way. Our 71 deals by 11-03 23 h spread over 23 categories (5 with
5 or more), too few for a ratio per category; the market had 3,480, and ours sold about 1.08 x the market's ratio.

Refund exposure: our goods sold under a warranty in the last warranty window and not broken yet. Each may still
break with its grade's rate over the part of the 6 h not yet passed; the market refunds the full price from our
balance, and a refund we cannot pay takes the shop under the bankruptcy line. restock keeps the line plus the
expected refunds plus `refund_sd` standard deviations of them out of what it spends.

Breaks, per goods grade: a Beta prior worth BREAK_PRIOR_W sales at the rates said in class on 11-03 (A 3%, B 15%,
C 35%: normal, damaged box, refurbished; said in passing, so the ledger corrects it), updated with the `broken` entries matched to settled goods by
buyer and SKU, over the exposure of those sales: goods break within 6 h, so a sale counts by the share of its 6 h
already passed (a sale 3 h ago counts one half).

Prices, per shop (the buyer's model): r = price paid / list price of every settled deal on the ledger, whoever
bought. A shop's ratio is the weighted mean of its deals' r, blended with the market's mean as if the market
were PRICE_PRIOR_W more deals: (sum w r + 5 x market) / (sum w + 5). A shop with 2 deals sits near the market,
one with 50 near its own mean. Old deals count less (the same half-life). The same blend gives
P(we pay at most a price): the share of the shop's deals at or under that ratio. Other buyers haggle differently,
so the shop's numbers come from their deals only, and our own deals give one factor: what we paid over the shop's
ratio, blended with 1 the same way.

Price bandit, per kind (food, goods) and arm (share of list price for the opening ask): Beta(1, 1), updated from
our own negotiations (data/negotiations.jsonl) with a reward in [0, 1]: 0 for no deal, else
(0.5 x margin + 0.3 x price) / (0.8 x list price), the shop score's weights on profit and revenue.
A deal counts only once it is paid: the buyer's `result` on the ledger (market.db) names our task in its receipt
and says `checks.settled`. In the first hour of Round 3 (11-03) 10 of 24 agreed deals were never paid, and the
bandit had counted them as sales. A deal with no result yet waits PAY_WAIT, then counts as no sale.
"""
import json
import math
import random
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import policy

DB = policy.DATA / "market.db"
NEGOTIATIONS = policy.DATA / "negotiations.jsonl"
SHOP_NAME = "inextro"           # our join name; settle entries name the shop by URL, the shops table maps it
BREAK_WINDOW = 6 * 3600
BREAK_PRIOR = {"A": 0.03, "B": 0.15, "C": 0.35}
BREAK_PRIOR_W = 10
LAMBDA_FLOOR = 0.01             # deals per hour per category before any data
SHARE_WEIGHT = 5


def connect(path=DB):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def catalog(db):
    return {r["sku"]: dict(r) for r in db.execute("SELECT * FROM catalog")}


def our_urls(db):
    return {r[0] for r in db.execute("SELECT url FROM shops WHERE name=?", (SHOP_NAME,))}


def round_start(db):
    row = db.execute("SELECT MIN(ts) FROM ledger WHERE round >= 3").fetchone()
    return row[0]


def entries(db, kind, since):
    """(ts, data) of Round 3 ledger entries of one kind since a time."""
    for r in db.execute("SELECT ts, data FROM ledger WHERE round >= 3 AND kind=? AND ts >= ? ORDER BY seq",
                        (kind, since)):
        yield r["ts"], json.loads(r["data"])


def sku_of(d):
    """settle entries name the SKU as `item` (agent.md); accept `sku` too in case the market names it so."""
    return d.get("item") if isinstance(d.get("item"), str) else d.get("sku")


def settled(d):
    """A settle entry that counts. Fields not confirmed before Round 3 started are read defensively."""
    return d.get("verified", True) is not False and sku_of(d)


class Estimates:
    def __init__(self, db=None, pol=None, now=None, competitors=None, cat=None):
        """competitors: {category: number of other shops holding stock in it}, from GET /api/shops. cat: the kit's
        catalog.json, used when the collector has not stored the catalog yet (the first minutes of a run)."""
        self.db = db or connect()
        self.p = pol or policy.load()
        self.now = now or time.time()
        self.cat = catalog(self.db) or cat or {}
        self.categories = sorted({x["category"] for x in self.cat.values()})
        self.competitors = competitors or {}
        self._demand()
        self._share()
        self._breaks()
        self._prices()

    # --- demand: gamma-Poisson per category with exponential forgetting

    def _demand(self):
        hl = self.p["half_life_h"] * 3600
        start = round_start(self.db) or self.now
        horizon = min(self.now - start, 6 * hl)               # older sales weigh under 1/64: skip them
        watched = hl / math.log(2) * (1 - 0.5 ** (horizon / hl)) / 3600 if horizon > 0 else 0.0
        mine = self.mine = our_urls(self.db)
        self.w = defaultdict(float)                           # category -> discounted deals (everyone)
        self.w_ours = defaultdict(float)
        self.sales = []                                       # (ts, data) for the manager's summary
        for ts, d in entries(self.db, "settle", self.now - horizon):
            if not settled(d):
                continue
            c = self.cat.get(sku_of(d), {}).get("category")
            if not c:
                continue
            wt = 0.5 ** ((self.now - ts) / hl)
            self.w[c] += wt
            if d.get("shop") in mine:
                self.w_ours[c] += wt
            self.sales.append((ts, d))
        total = sum(self.w.values())
        self.lambda0 = max(total / max(watched, 1e-9) / len(self.categories), LAMBDA_FLOOR) if watched else LAMBDA_FLOOR
        b0 = self.p["prior_hours"]
        self.alpha = {c: self.lambda0 * b0 + self.w[c] for c in self.categories}
        self.beta = b0 + watched

    def rate(self, c):
        """Posterior mean deals per hour in category c, whole market."""
        return self.alpha[c] / self.beta

    # --- our share of a category's deals

    def _share(self):
        held = [n for n in self.competitors.values() if n is not None]
        mean = 1 / (1 + (sum(held) / len(held) if held else 3))
        a, b = SHARE_WEIGHT * mean, SHARE_WEIGHT * (1 - mean)
        for c, w_ours in self.w_ours.items():
            a += w_ours
            b += self.w[c] - w_ours
        self.share_ab = (a, b)
        self.share_mean = a / (a + b)

    def share(self, c):
        """Our expected share in c: the shop-wide share, scaled down where more shops than usual hold stock."""
        n = self.competitors.get(c)
        if n is None:
            return self.share_mean
        held = [x for x in self.competitors.values() if x is not None]
        usual = 1 + (sum(held) / len(held) if held else 3)
        return min(1.0, self.share_mean * usual / (1 + n))

    def p_none(self, c, hours):
        """P(we sell nothing in c within `hours`): negative binomial at zero."""
        s = self.share(c)
        return (self.beta / (self.beta + s * hours)) ** self.alpha[c]

    def tail(self, c, hours, kmax=40):
        """[P(we sell at least k in c within `hours`) for k = 1..kmax]: the negative binomial predictive."""
        r, q = self.alpha[c], self.beta / (self.beta + self.share(c) * hours)
        pmf = q ** r
        cdf, out = pmf, []
        for k in range(kmax):
            out.append(max(0.0, 1 - cdf))
            pmf *= (k + r) / (k + 1) * (1 - q)
            cdf += pmf
        return out

    # --- sale prices: the market's ratio per category, times our shop's factor

    def _prices(self):
        hl = self.p["half_life_h"] * 3600
        rows = []                                             # (category, paid / list, weight, ours)
        for ts, d in self.sales:
            x, a = self.cat.get(sku_of(d)), amount_of(d)
            if x and a and x.get("list_price"):
                rows.append((x["category"], a / x["list_price"], 0.5 ** ((self.now - ts) / hl), d.get("shop") in self.mine))
        others = [(c, r, w) for c, r, w, o in rows if not o]
        self.price_market = (sum(r * w for _, r, w in others) + PRICE_R0) / (sum(w for *_, w in others) + 1)
        sw, srw = defaultdict(float), defaultdict(float)
        for c, r, w in others:
            sw[c] += w
            srw[c] += r * w
        self.price_cat = {c: (srw[c] + PRICE_PRIOR_W * self.price_market) / (sw[c] + PRICE_PRIOR_W) for c in sw}
        ours = [(c, r, w) for c, r, w, o in rows if o]
        self.price_ours_n = len(ours)
        self.price_ours = ((sum(w * r / self.cat_ratio(c) for c, r, w in ours) + PRICE_PRIOR_W)
                           / (sum(w for *_, w in ours) + PRICE_PRIOR_W))

    def cat_ratio(self, c):
        """The market's price paid / list in category c (its mean when nobody sold there yet)."""
        return self.price_cat.get(c, self.price_market)

    def price_ratio(self, c):
        """What we expect a unit of category c to sell for, as a share of its list price (never above list)."""
        return min(1.0, self.cat_ratio(c) * self.price_ours)

    # --- refunds we may still owe: our goods under warranty, not broken yet

    def refund_exposure(self, hours):
        """(expected refunds, their standard deviation) over our goods sold in the last `hours` with a warranty."""
        sold = [(ts, d) for ts, d in entries(self.db, "settle", self.now - hours * 3600)
                if settled(d) and d.get("shop") in self.mine and (d.get("warranty_hours") or 0) > 0
                and self.cat.get(sku_of(d), {}).get("kind") == "goods"]
        broke = {(d.get("buyer"), sku_of(d)) for _, d in entries(self.db, "broken", self.now - hours * 3600)}
        mean = var = 0.0
        for ts, d in sold:
            a = amount_of(d) or 0
            if (d.get("buyer"), sku_of(d)) in broke:          # already refunded (or will be at once)
                continue
            p, e = self.break_rate.get(d.get("grade"), max(self.break_rate.values())), min(1.0, (self.now - ts) / BREAK_WINDOW)
            p = p * (1 - e) / (1 - p * e) if p * e < 1 else 0.0   # it has not broken in the part already passed
            mean += a * p
            var += a * a * p * (1 - p)
        return mean, math.sqrt(var)

    # --- break rates per goods grade

    def _breaks(self):
        since = self.now - 7 * 86400
        sold = defaultdict(list)                              # (buyer, sku) -> [(ts, grade)]
        n = defaultdict(float)
        for ts, d in entries(self.db, "settle", since):
            sku = sku_of(d)
            if not settled(d) or self.cat.get(sku, {}).get("kind") != "goods":
                continue
            g = d.get("grade") or "?"
            sold[(d.get("buyer"), sku)].append((ts, g))
            n[g] += min(1.0, (self.now - ts) / BREAK_WINDOW)   # exposure: a 3-hour-old sale has had half its window
        k = defaultdict(int)
        for ts, d in entries(self.db, "broken", since):
            cands = [g for t, g in sold.get((d.get("buyer"), sku_of(d)), []) if 0 <= ts - t <= BREAK_WINDOW]
            if cands:
                k[cands[-1]] += 1
        self.break_counts = {g: (k[g], round(n[g], 1)) for g in "ABC"}
        self.break_rate = {g: (BREAK_PRIOR_W * BREAK_PRIOR[g] + k[g]) / (BREAK_PRIOR_W + max(n[g], k[g]))
                           for g in "ABC"}


# --- prices per shop, from everyone's settled deals (the buyer's model)

PRICE_PRIOR_W = 5               # a shop's own deals outweigh the market's mean after 5
PRICE_R0 = 0.85                 # the market's ratio before any deal


def amount_of(d):
    """agent.md's example settle names the price `amount`; accept `price` too."""
    a = d.get("amount", d.get("price"))
    return a if isinstance(a, (int, float)) and a > 0 else None


class Prices:
    def __init__(self, db=None, cat=None, now=None, half_life_h=24, us=SHOP_NAME):
        """us: our buyer's name on the ledger (settle `buyer`). Shops are named as in shops.json (`name`)."""
        self.db = db or connect()
        self.now = now or time.time()
        self.cat = catalog(self.db) or cat or {}
        hl = half_life_h * 3600
        # shops by name: a shop's URL changes when its kit restarts (a new tunnel), settle entries carry the URL
        names = {r["url"]: r["name"] for r in self.db.execute("SELECT url, name FROM shops")}
        self.deals = defaultdict(list)                        # shop name -> [(r, w)], other buyers' deals
        ours = []                                             # (shop, r, w)
        for ts, d in entries(self.db, "settle", self.now - 6 * hl):
            x, a = self.cat.get(sku_of(d)), amount_of(d)
            if not settled(d) or not x or not a or not x.get("list_price") or not d.get("shop"):
                continue
            r, w = a / x["list_price"], 0.5 ** ((self.now - ts) / hl)
            shop = names.get(d["shop"], d["shop"])
            if d.get("buyer") == us:
                ours.append((shop, r, w))
            else:
                self.deals[shop].append((r, w))
        every = [rw for v in self.deals.values() for rw in v]
        self.n = len(every) + len(ours)
        self.market = (sum(r * w for r, w in every) + PRICE_R0) / (sum(w for _, w in every) + 1)
        self.W = {s: sum(w for _, w in v) for s, v in self.deals.items()}
        self.shop = {s: (sum(r * w for r, w in v) + PRICE_PRIOR_W * self.market) / (self.W[s] + PRICE_PRIOR_W)
                     for s, v in self.deals.items()}
        self.Wu = sum(w for _, _, w in ours)
        self.us = (sum(r / self.ratio_market(s) * w for s, r, w in ours) + PRICE_PRIOR_W) / (self.Wu + PRICE_PRIOR_W)

    def ratio_market(self, shop):
        """The shop's ratio from other buyers' deals (the market's mean for a shop nobody bought from)."""
        return self.shop.get(shop, self.market)

    def ratio(self, shop):
        """The price / list price we expect to pay at `shop`."""
        return self.ratio_market(shop) * self.us

    def p_within(self, shop, price, list_price):
        """P(we end at or under `price` at `shop`): the share of its deals at or under that ratio (for us), blended
        with the market's share the same way."""
        cut = price / list_price / self.us
        every = [rw for v in self.deals.values() for rw in v]
        tot = sum(w for _, w in every)
        # the market's share, with one deal's worth of a flat prior over 50-100% of list (an empty ledger at the
        # start of the round must not rule out every order whose budget is under the usual price)
        flat = min(max((cut - 0.5) / 0.5, 0.0), 1.0)
        market = (sum(w for r, w in every if r <= cut) + flat) / (tot + 1)
        v = self.deals.get(shop, [])
        return (sum(w for r, w in v if r <= cut) + PRICE_PRIOR_W * market) / (self.W.get(shop, 0) + PRICE_PRIOR_W)



# --- price bandit: Thompson sampling over opening-ask arms, from our own negotiations

def reward(rec):
    if not rec.get("price"):
        return 0.0
    margin = max(rec["price"] - rec["cost"], 0)
    return min(1.0, (0.5 * margin + 0.3 * rec["price"]) / (0.8 * rec["list_price"]))


PAY_WAIT = 900   # seconds a deal may wait for the buyer's result before it counts as unpaid


def paid_tasks(db_path=DB):
    """{task: settled} from the buyers' results on the ledger; None when market.db cannot be read."""
    try:
        db = connect(db_path)
        rows = db.execute("SELECT json_extract(data, '$.receipt.task'), json_extract(data, '$.checks.settled') "
                          "FROM ledger WHERE round >= 3 AND kind = 'result' AND data LIKE '%\"task\"%'").fetchall()
        db.close()
    except sqlite3.Error:
        return None
    out = {}
    for task, ok in rows:
        if task:
            out[task] = out.get(task, False) or bool(ok)
    return out


def bandit_stats(path=NEGOTIATIONS, since=0, now=None, paid=...):
    """Beta counts per (kind, arm). A negotiation is its `open` line plus a `deal` line, or no deal when it was
    cancelled or has been open for 10 minutes (buyers often just stop answering). A deal is a sale only when the
    buyer paid (paid_tasks); unknown for PAY_WAIT, then no sale. Without market.db every deal counts, as before."""
    now = now or time.time()
    if paid is ...:
        paid = paid_tasks()
    stats = {(k, a): [1.0, 1.0] for k in ("food", "goods") for a in policy.ARMS}
    opened, closed = {}, {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("ts", 0) < since or not rec.get("task"):
            continue
        if rec.get("outcome") == "open":
            opened[rec["task"]] = rec
        else:
            closed[rec["task"]] = rec
    for task, rec in opened.items():
        end = closed.get(task)
        if end is None and now - rec["ts"] < 600:
            continue
        key = (rec.get("kind"), rec.get("arm"))
        if key not in stats:
            continue
        r = 0.0
        if end and end.get("outcome") == "deal":
            if paid is not None and task not in paid and now - end["ts"] < PAY_WAIT:
                continue
            r = reward(end) if paid is None or paid.get(task) else 0.0
        stats[key][0] += r
        stats[key][1] += 1 - r
    return stats


def pick_arm(kind, stats=None):
    stats = stats or bandit_stats()
    draws = {a: random.betavariate(*stats[(kind, a)]) for a in policy.ARMS}
    return max(draws, key=draws.get)


if __name__ == "__main__":
    e = Estimates()
    print(f"lambda0 {e.lambda0:.3f}/h per category, beta {e.beta:.2f} h, share {e.share_mean:.3f}, "
          f"{len(e.sales)} settles in the window")
    top = sorted(e.categories, key=e.rate, reverse=True)[:10]
    for c in top:
        print(f"  {e.rate(c):7.2f}/h  ours {e.w_ours[c]:.1f}  {c}")
    print("break rate by grade:", {g: round(r, 3) for g, r in e.break_rate.items()}, e.break_counts)
    pr = Prices(e.db, cat=e.cat, now=e.now, half_life_h=e.p["half_life_h"])
    print(f"prices: {pr.n} settles, market ratio {pr.market:.3f}, our buyer x{pr.us:.3f} ({pr.Wu:.1f} deals)")
    for s in sorted(pr.shop, key=pr.shop.get)[:5]:
        print(f"  {pr.shop[s]:.3f}  ({pr.W[s]:.1f} deals)  {s}")
    s = bandit_stats()
    for k in ("food", "goods"):
        print(k, {a: round(s[(k, a)][0] / sum(s[(k, a)]), 2) for a in policy.ARMS})
