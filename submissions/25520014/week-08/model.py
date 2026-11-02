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
    P(we sell none in H) = (beta / (beta + s H)) ** alpha
which is what the newsvendor rule in restock.py compares with its critical ratio.

Breaks, per goods grade: Beta(1, 9) prior (10%), updated with the `broken` entries matched to settled goods by
buyer and SKU, over the exposure of those sales: goods break within 6 h, so a sale counts by the share of its 6 h
already passed (a sale 3 h ago counts one half).

Prices, per shop (the buyer's model): y = log(price paid / list price) of every settled deal on the ledger, whoever
bought. A hierarchical normal: y = m_kind + d_shop + b_us + noise. m_kind is the market's mean (food, goods); the
shop effect d_shop has a N(0, tau^2) prior and its posterior mean is shrunk toward 0 by the shop's (discounted)
deal count W: d = sum(w (y - m)) / (W + sigma^2 / tau^2). sigma^2 (spread within a shop) and tau^2 (spread
between shops) come from the data by moments, pulled toward SIGMA0 and TAU0 while there is little. b_us is our
buyer against the market, shrunk the same way from our own deals. Old deals count less (the same half-life).

Price bandit, per kind (food, goods) and arm (share of list price for the opening ask): Beta(1, 1), updated from
our own negotiations (data/negotiations.jsonl) with a reward in [0, 1]: 0 for no deal, else
(0.5 x margin + 0.3 x price) / (0.8 x list price), the shop score's weights on profit and revenue.
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

    # --- demand: gamma-Poisson per category with exponential forgetting

    def _demand(self):
        hl = self.p["half_life_h"] * 3600
        start = round_start(self.db) or self.now
        horizon = min(self.now - start, 6 * hl)               # older sales weigh under 1/64: skip them
        watched = hl / math.log(2) * (1 - 0.5 ** (horizon / hl)) / 3600 if horizon > 0 else 0.0
        mine = our_urls(self.db)
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

    def base_stock(self, c, minutes, cr):
        """Smallest stock q with P(our deals in the next `minutes` <= q) >= cr; at least 1 when held."""
        r, p = self.alpha[c], self.beta / (self.beta + self.share(c) * minutes / 60)
        k, pmf = 0, p ** r
        cdf = pmf
        while cdf < cr and k < 20:
            pmf *= (k + r) / (k + 1) * (1 - p)
            k += 1
            cdf += pmf
        return max(1, k)

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
        self.break_rate = {g: (1 + k[g]) / (10 + max(n[g], k[g])) for g in "ABC"}


# --- prices per shop, from everyone's settled deals (the buyer's model)

PRICE_SIGMA0 = 0.15             # log-ratio spread within a shop before data
PRICE_TAU0 = 0.15               # spread of shop effects before data
PRICE_M0 = math.log(0.85)       # market mean log ratio before any deal
PRICE_PRIOR_N = 10              # how many deals' worth the two spreads above weigh


def amount_of(d):
    """agent.md's example settle names the price `amount`; accept `price` too."""
    a = d.get("amount", d.get("price"))
    return a if isinstance(a, (int, float)) and a > 0 else None


class Prices:
    def __init__(self, db=None, cat=None, now=None, half_life_h=24, us=SHOP_NAME):
        """us: our buyer's name on the ledger (settle `buyer`)."""
        self.db = db or connect()
        self.now = now or time.time()
        self.cat = catalog(self.db) or cat or {}
        hl = half_life_h * 3600
        rows = []                                             # (shop, kind, y, w, ours)
        for ts, d in entries(self.db, "settle", self.now - 6 * hl):
            x, a = self.cat.get(sku_of(d)), amount_of(d)
            if not settled(d) or not x or not a or not x.get("list_price") or not d.get("shop"):
                continue
            rows.append((d["shop"], x["kind"], math.log(a / x["list_price"]), 0.5 ** ((self.now - ts) / hl),
                         d.get("buyer") == us))
        self.n = len(rows)
        self.m = {}
        for k in ("food", "goods"):
            ws = [(y, w) for _, kk, y, w, _ in rows if kk == k]
            W = sum(w for _, w in ws)
            self.m[k] = (sum(y * w for y, w in ws) + PRICE_M0) / (W + 1)
        by = defaultdict(list)                                # shop -> [(residual, w, ours)]
        for s, k, y, w, ours in rows:
            by[s].append((y - self.m[k], w, ours))
        W = {s: sum(w for _, w, _ in r) for s, r in by.items()}
        mean = {s: sum(e * w for e, w, _ in r) / W[s] for s, r in by.items()}
        within = sum(w * (e - mean[s]) ** 2 for s, r in by.items() for e, w, _ in r)
        self.sigma2 = (within + PRICE_PRIOR_N * PRICE_SIGMA0 ** 2) / (sum(W.values()) + PRICE_PRIOR_N)
        # tau^2 by moments: the spread of shop means minus what their own noise explains
        big = [s for s in by if W[s] >= 3]
        raw = (sum((mean[s] ** 2 - self.sigma2 / W[s]) for s in big) / len(big)) if big else 0.0
        self.tau2 = max((max(raw, 0.0) * len(big) + PRICE_PRIOR_N * PRICE_TAU0 ** 2) / (len(big) + PRICE_PRIOR_N),
                        0.02 ** 2)
        k = self.sigma2 / self.tau2
        self.var = {s: 1 / (W[s] / self.sigma2 + 1 / self.tau2) for s in by}
        self.W = W
        # shop effects and our buyer's effect, fitted in turn (our deals would otherwise pull the shop effect
        # toward our own price and hide part of b)
        self.Wu = sum(w for r in by.values() for _, w, ours in r if ours)
        self.b = 0.0
        for _ in range(5):
            self.d = {s: sum((e - self.b * ours) * w for e, w, ours in r) / (W[s] + k) for s, r in by.items()}
            self.b = sum((e - self.d[s]) * w for s, r in by.items() for e, w, ours in r if ours) / (self.Wu + k)

    def predict(self, shop, kind):
        """(mean, sd) of log(price / list) we would pay at `shop`; an unseen shop gets the market mean."""
        mu = self.m.get(kind, PRICE_M0) + self.d.get(shop, 0.0) + self.b
        return mu, math.sqrt(self.sigma2 + self.var.get(shop, self.tau2))

    def ratio(self, shop, kind):
        """Median price / list price we would pay at `shop`."""
        return math.exp(self.predict(shop, kind)[0])

    def p_within(self, shop, kind, price, list_price):
        """P(the price we end at, at `shop`, is at most `price`), e.g. the budget."""
        if price <= 0:
            return 0.0
        mu, sd = self.predict(shop, kind)
        return 0.5 * (1 + math.erf((math.log(price / list_price) - mu) / (sd * math.sqrt(2))))

    def draw(self, shop, kind):
        """A Thompson draw of the shop's median ratio: the shop effect from its posterior, not a single deal."""
        return math.exp(self.m.get(kind, PRICE_M0) + self.b
                        + random.gauss(self.d.get(shop, 0.0), math.sqrt(self.var.get(shop, self.tau2))))


# --- price bandit: Thompson sampling over opening-ask arms, from our own negotiations

def reward(rec):
    if not rec.get("price"):
        return 0.0
    margin = max(rec["price"] - rec["cost"], 0)
    return min(1.0, (0.5 * margin + 0.3 * rec["price"]) / (0.8 * rec["list_price"]))


def bandit_stats(path=NEGOTIATIONS, since=0, now=None):
    """Beta counts per (kind, arm). A negotiation is its `open` line plus a `deal` line, or no deal when it was
    cancelled or has been open for 10 minutes (buyers often just stop answering)."""
    now = now or time.time()
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
        r = reward(end) if end and end.get("outcome") == "deal" else 0.0
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
    print(f"prices: {pr.n} settles, market median ratio "
          f"{ {k: round(math.exp(v), 3) for k, v in pr.m.items()} }, sigma {math.sqrt(pr.sigma2):.3f}, "
          f"tau {math.sqrt(pr.tau2):.3f}, our buyer x{math.exp(pr.b):.3f} ({pr.Wu:.1f} deals)")
    for s in sorted(pr.d, key=pr.d.get)[:5]:
        print(f"  x{math.exp(pr.d[s]):.3f}  ({pr.W[s]:.1f} deals)  {s}")
    s = bandit_stats()
    for k in ("food", "goods"):
        print(k, {a: round(s[(k, a)][0] / sum(s[(k, a)]), 2) for a in policy.ARMS})
