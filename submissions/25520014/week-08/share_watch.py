"""Week 08 share watch: tell the owner within minutes when the shop falls behind the market (owner, 11-05 01 h).

On 11-04 our share of the market's revenue fell from 33% (14 h) to 6% (00 h) while the market held, and it took
hours of hand checks to see. The 0 h check compared us with the first-placed shop and called it a market slump.
Every 10 minutes this reads the ledger (market.db, read-only), our talks (data/negotiations.jsonl) and our stock
(.market/inventory.json) and appends what it finds to data/alerts.md (the same file rule_watch writes, newest last,
KST), with the numbers in data/share_watch.json. Signals, each against the 3 hours before the last hour:
  1. share: our share of the market's revenue (all, and goods listed at 1M+) under 0.75 of it while the market held;
  2. talks: buyers opening talks with us under 0.75 of it while the market's deals held;
  3. price: on a SKU where other shops sold grade A 5+ times in the hour, our median A floor is over their
     median paid share of list + 0.02 (on 11-04 the only signal that came before the revenue fell: 다우닝 and
     지누스 at 17 h, 자코모 at 22 h; session 56's audit);
  4. top SKUs: of the market's top 10 SKUs by revenue in the hour, those where our share is under 5%, with our stock,
     talks and deals on each (out of stock, or held and losing), added when 1 or 2 rings.
(Our opening ask sits over the market's paid price by design, so the price signal uses the floor: with the ask it
rang on 5 SKUs in every half hour of 11-04.)
It changes nothing the shop reads; the owner decides. A signal repeats at most once an hour.
"""
import argparse
import collections
import json
import sqlite3
import statistics as st
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MD, DATA = HERE / ".market", HERE / "data"
DB, NEG = DATA / "market.db", DATA / "negotiations.jsonl"
STATE, ALERTS = DATA / "share_watch.json", DATA / "alerts.md"
OURS = "inextro"
EVERY_S, COOLDOWN_S = 600, 3600
HIGH = 1_000_000            # list price of the high-end band
DROP, HELD = 0.75, 0.8      # ours under DROP x baseline while the market stays at HELD x its baseline or more
# (replay of 11-04: 0.5 first rang at 20:49 for 1M+ and 23:19 for all; 0.75 rings at 18:19, when 30% became 22%)
MIN_SHARE = 0.05            # baseline share under this: nothing to lose
PRICE_GAP, PRICE_MIN = 0.02, 5
TOP_N, TOP_SHARE = 10, 0.05


def kst(t):
    return time.strftime("%m-%d %H:%M", time.gmtime(t + 9 * 3600))


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def settles(db, t0, t1):
    names = dict(db.execute("select url, name from shops"))
    lp = dict(db.execute("select sku, list_price from catalog"))
    pn = dict(db.execute("select sku, name from catalog"))
    out = []
    for ts, d in db.execute("select ts, data from ledger where kind = 'settle' and ts >= ? and ts < ?", (t0, t1)):
        d = json.loads(d)
        sku = d.get("sku") or d.get("item")
        out.append({"ts": ts, "shop": names.get(d.get("shop"), "?"), "sku": sku, "grade": d.get("grade"),
                    "amount": d.get("amount") or 0, "list": lp.get(sku) or 0})
    return out, pn


def opens(t0, t1):
    out = []
    try:
        with NEG.open(encoding="utf-8") as f:
            for line in f:
                if '"open"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("outcome") == "open" and t0 <= d.get("ts", 0) < t1:
                    out.append(d)
    except OSError:
        pass
    return out


def held_units():
    inv = load(MD / "inventory.json", {})
    c = collections.Counter()
    for u in inv.get("units") or []:
        c[u.get("sku")] += 1
    return c


def measure(now, db, live=True):
    s, names = settles(db, now - 4 * 3600, now)
    last = [x for x in s if x["ts"] >= now - 3600]
    base = [x for x in s if x["ts"] < now - 3600]

    def rev(rows, band=False):
        rows = [x for x in rows if not band or x["list"] >= HIGH]
        m = sum(x["amount"] for x in rows)
        o = sum(x["amount"] for x in rows if x["shop"] == OURS)
        return m, o

    m = {}
    for key, band in (("all", False), ("high", True)):
        m1, o1 = rev(last, band)
        mb, ob = rev(base, band)
        m[key] = {"market_1h": m1, "ours_1h": o1, "share_1h": o1 / m1 if m1 else 0.0,
                  "market_base_h": mb / 3, "share_base": ob / mb if mb else 0.0}
    t_last = opens(now - 3600, now)
    t_base = opens(now - 4 * 3600, now - 3600)
    m["talks"] = {"ours_1h": len(t_last), "ours_base_h": len(t_base) / 3,
                  "market_deals_1h": len(last), "market_deals_base_h": len(base) / 3}

    # 3. price: our A asks / floors against the other shops' A paid share of list
    mk = collections.defaultdict(list)
    for x in last:
        if x["shop"] != OURS and x["grade"] == "A" and x["list"]:
            mk[x["sku"]].append(x["amount"] / x["list"])
    ours = collections.defaultdict(lambda: ([], []))
    for d in t_last:
        if d.get("grade") == "A" and d.get("list_price"):
            ours[d["sku"]][0].append(d["ask"] / d["list_price"])
            ours[d["sku"]][1].append(d["floor"] / d["list_price"])
    price = []
    for sku, (asks, floors) in ours.items():
        if len(mk.get(sku, [])) < PRICE_MIN:
            continue
        mmed, a, fl = st.median(mk[sku]), st.median(asks), st.median(floors)
        if fl > mmed + PRICE_GAP:
            price.append({"sku": sku, "name": (names.get(sku) or "")[:24], "market_a": round(mmed, 3),
                          "our_ask": round(a, 3), "our_floor": round(fl, 3), "market_n": len(mk[sku])})
    m["price"] = sorted(price, key=lambda p: p["market_a"] - p["our_floor"])

    # 4. the market's top SKUs where we are under TOP_SHARE
    by = collections.defaultdict(lambda: [0, 0, 0])
    for x in last:
        r = by[x["sku"]]
        r[0] += x["amount"]
        if x["shop"] == OURS:
            r[1] += x["amount"]
            r[2] += 1
    talks = collections.Counter(d.get("sku") for d in t_last)
    stock = held_units() if live else {}
    top = []
    for sku, (mr, orv, od) in sorted(by.items(), key=lambda kv: -kv[1][0])[:TOP_N]:
        if mr and orv / mr < TOP_SHARE:
            top.append({"sku": sku, "name": (names.get(sku) or "")[:24], "market_1h": mr, "share": round(orv / mr, 3),
                        "stock": stock.get(sku) if live else None, "talks": talks.get(sku, 0), "deals": od})
    m["top"] = top
    return m


def signals(m):
    out = []
    for key, label in (("all", "revenue"), ("high", "1M+ revenue")):
        x = m[key]
        if (x["share_base"] >= MIN_SHARE and x["share_1h"] < DROP * x["share_base"]
                and x["market_1h"] >= HELD * x["market_base_h"]):
            out.append((f"share_{key}", f"our share of the market's {label} {x['share_1h']:.1%} in the last hour against "
                        f"{x['share_base']:.1%} the 3 h before; the market {x['market_1h'] / 1e6:.0f}M against "
                        f"{x['market_base_h'] / 1e6:.0f}M/h"))
    t = m["talks"]
    if (t["ours_base_h"] >= 20 and t["ours_1h"] < DROP * t["ours_base_h"]
            and t["market_deals_1h"] >= HELD * t["market_deals_base_h"]):
        out.append(("talks", f"buyers opened {t['ours_1h']} talks with us in the last hour against {t['ours_base_h']:.0f}/h "
                    f"the 3 h before; market deals {t['market_deals_1h']} against {t['market_deals_base_h']:.0f}/h"))
    for p in m["price"][:3]:
        out.append((f"price_{p['sku']}", f"price: {p['sku']} {p['name']} other shops sold A at {p['market_a']} of list "
                    f"({p['market_n']} sales), our A ask {p['our_ask']}, floor {p['our_floor']}"))
    if m["top"] and out and not all(k.startswith("price_") for k, _ in out):
        show = "; ".join(f"{x['sku']} {x['name']} {x['market_1h'] / 1e6:.0f}M ours {x['share']:.0%} "
                         f"stock {x['stock']} talks {x['talks']} deals {x['deals']}" for x in m["top"][:5])
        out.append(("top", f"top SKUs under {TOP_SHARE:.0%} ours: {show}"))
    return out


def shortage_note(now):
    """model.shortage() as restock sees it (session 56, a9c5f03), recorded only; None when unavailable."""
    try:
        import model
        r = model.Estimates().shortage()
        return r if isinstance(r, (dict, list)) else str(r)
    except Exception as e:
        return f"unavailable ({type(e).__name__})"


def run(now=None, write=True, live=True):
    now = now or time.time()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        m = measure(now, db, live)
    finally:
        db.close()
    sig = signals(m)
    if not write:
        return m, sig
    state = load(STATE, {})
    sent = state.get("sent", {})
    fresh = [(k, text) for k, text in sig if now - sent.get(k, 0) >= COOLDOWN_S]
    for k, _ in fresh:
        sent[k] = now
    short = shortage_note(now)
    STATE.write_text(json.dumps({"ts": now, "kst": kst(now), "metrics": m, "signals": [k for k, _ in sig],
                                 "shortage": short, "sent": sent}, ensure_ascii=False, default=str),
                     encoding="utf-8")
    if fresh:
        with ALERTS.open("a", encoding="utf-8") as f:
            for _, text in fresh:
                f.write(f"- {kst(now)} share: {text}\n")
    return m, sig


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help=f"run every {EVERY_S} s")
    ap.add_argument("--replay", type=float, default=0, help="hours back: print the signals every 30 min, write nothing")
    a = ap.parse_args()
    if a.replay:
        end = time.time()
        t = end - a.replay * 3600
        while t <= end:
            m, sig = run(t, write=False, live=False)
            x = m["all"]
            print(f"{kst(t)} share {x['share_1h']:.1%} (base {x['share_base']:.1%}) high {m['high']['share_1h']:.1%} "
                  f"talks {m['talks']['ours_1h']} | {', '.join(k for k, _ in sig) or '-'}", flush=True)
            t += 1800
    elif a.loop:
        while True:
            try:
                run()
            except Exception as e:
                print(f"{kst(time.time())} share_watch failed: {type(e).__name__}: {str(e)[:160]}", flush=True)
            time.sleep(EVERY_S)
    else:
        m, sig = run()
        print(json.dumps({"signals": sig, "all": m["all"], "high": m["high"], "talks": m["talks"]}, ensure_ascii=False,
                         indent=1))
