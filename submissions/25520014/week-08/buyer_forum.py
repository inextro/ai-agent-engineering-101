"""Week 08 buyer forum: our buyer posts what it bought, as facts anyone can check on the ledger.

    python buyer_forum.py --dir .            # runs forever, checks every 5 s
    python buyer_forum.py --dir . --once     # one pass (testing)

Buyer posts are 0.3 (votes from other owners' agents with 5+ deals) + 0.1 (how many we wrote) of the buyer's score, so each post should
be worth reading to another buyer: which shop sold what at what share of the list price, whether the grade was as
claimed, the warranty. Only facts the market signed (data/purchases.jsonl, written by review.py from deal.json),
with the ledger entries in refs so a reader can check them. Never how our buyer chooses shops or haggles, never our
own shop (we cannot buy there; naming it reads as an ad), no advice, no attacks.

One post every POST_EVERY_S (the count of posts is scored, so as often as the market's 5 min limit allows; fixed
on 11-04 for the rest of the round, the buyer manager cannot change it), the kinds taken in turn, skipping a kind with nothing new (11-03: many posts, of every kind, no repeats;
be the buyer other agents read for facts they cannot get alone). A wanted post under WANTED_JUMP_S old goes first.
  1. wanted: a reply to a wanted post (wanted.py, last WANTED_H hours, newest first): which shops hold that category
     now (GET /api/shops), and what it sold for on the ledger; nothing when no shop holds it and nothing sold
  2. arrivals: what other shops took in at the supply windows since our last such post, by category. OFF since
     11-04: both posts got a dislike (it lays out other shops' buying). A post's score is the Wilson lower bound of
     its likes (z 1.96) and the buyer's forum number the mean over voted posts, so one dislike on 5 likes costs 0.13
  3. purchases: our purchases not posted yet, a false grade claim first (code template since 11-04)
  4. trend: the busiest category's settles and median share of the list price, last TREND_H hours against the rest
     of the day
  5. compare: one category's shops side by side over the last day, cheapest median share of the list price first
  6. broken: goods that broke in the last day, by grade, hours after the sale, warranty and refunds
  7. shop check (at most every DIGEST_EVERY): per shop over the last day, settles, how many grade claims were true,
     the warranty given and the median share of the list price
  8. summary (at most every SUMMARY_EVERY): the market's last hour, settles, buyers, shops, busiest categories, grades
Every number comes from the public ledger or the market's API; never our own shop's name.
No repeats: a category (stock reply or price trend) waits TOPIC_H hours, and a text already posted in the last 300
posts is dropped. Replies, trends and the shop check are code templates (every number is computed, refs cite the ledger); purchases go through
haiku as before. haiku writes it from the facts (the buyer manager's forum guidance in front); a post with a number the
facts do not give, our shop's name, or over MAX_CHARS is retried once, then the template goes out instead.
"""
import argparse
import json
import math
import re
import time
from pathlib import Path

import httpx

import buyer_policy
import harness
import model
import policy
import wanted
from forum import NAMES, queue_post, read, stray_numbers

STATE = policy.DATA / "buyer_forum_state.json"
LOG = policy.DATA / "buyer_forum.jsonl"
PURCHASES = policy.DATA / "purchases.jsonl"
MODEL, EFFORT = "haiku", "medium"
MAX_ITEMS = 2             # posts are frequent now (their count is scored), so each carries fewer purchases
MAX_CHARS = 280           # the forum shows about 300 characters
MARKET = "https://market.ourolabs.ai"
WANTED_H = 0.5            # wanted posts older than this get no reply (the order behind them has likely expired)
WANTED_JUMP_S = 600       # a wanted post this fresh goes ahead of the turn (unless the last post was a reply too)
POST_EVERY_S = 305        # the market sends one buyer post per 5 min; 5 s spare so the kit never holds one back
DIGEST_EVERY = 3600
SUMMARY_EVERY = 3600
TOPIC_H = 2               # a category (stock reply or price trend) is not posted again within this many hours
TREND_H = 6               # the price trend compares the last TREND_H hours with the rest of the day
DAY = 86400


def log(msg):
    print(f"{time.strftime('%m-%d %H:%M:%S')} [buyer-forum] {msg}", flush=True)


def shop_names():
    try:
        return {r["url"]: r["name"] for r in model.connect().execute("SELECT url, name FROM shops")}
    except Exception:
        return {}


def facts(rows, names):
    out = []
    for r in rows:
        f = {"shop": names.get(r.get("shop"), r.get("shop")), "item": r.get("name") or r.get("sku")}
        if r.get("price") and r.get("list"):
            f["pct_of_list"] = round(100 * r["price"] / r["list"])
        if r.get("grade"):
            f["grade"] = r["grade"]
        if r.get("claim_honest") is not None:
            f["grade_as_claimed"] = bool(r["claim_honest"])
        if r.get("warranty_hours"):
            f["warranty_hours"] = r["warranty_hours"]
        out.append(f)
    return out


def template(fs):
    parts = []
    for f in fs:
        s = f"{f['shop']}에서 {f['item']}"
        if "pct_of_list" in f:
            s += f", 정가의 {f['pct_of_list']}%"
        if f.get("grade_as_claimed") is False:
            s += ", 등급이 말한 것과 달랐음"
        elif f.get("grade"):
            s += f", {f['grade']}등급 말한 대로" if f.get("grade_as_claimed") else f", {f['grade']}등급"
        if f.get("warranty_hours"):
            s += f", 보증 {f['warranty_hours']}시간"
        parts.append(s)
    text = "[구매 후기] 영수증은 refs에: " + "; ".join(parts) + "."
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 1] + "…"


PROMPT = """{guide}You write a short forum post on an agent marketplace, as a buyer, about purchases you just made.
Other buyers read it to choose shops. The facts (from the market's signed receipts):
{facts}

Write one post in Korean starting with "[구매 후기] ", at most {chars} characters, plain text. Say per purchase: the item, the shop, the share of
the list price paid, and whether the grade was as claimed (say it plainly when it was not). Use only these facts and
only their numbers. Do not explain how you pick shops or negotiate, give no advice, do not praise or attack beyond the
facts, no hashtags. Reply with the post only."""


def write(fs):
    tpl = template(fs)
    g = buyer_policy.load_playbook().get("forum")
    prompt = PROMPT.format(guide=f"Buyer manager's guidance (follow it where it fits; the rules below come first): "
                                 f"{g}\n" if g else "", facts=json.dumps(fs, ensure_ascii=False), chars=MAX_CHARS)
    sources = [json.dumps(fs, ensure_ascii=False)]
    for attempt in (1, 2):
        try:
            text = " ".join(harness.call(prompt, timeout=60, model=MODEL, effort=EFFORT).split()).strip('"')
        except Exception as e:
            log(f"haiku failed ({type(e).__name__}), template")
            return tpl, "template"
        problem = ("too long" if len(text) > MAX_CHARS else
                   "names our own shop" if any(n in text.lower() for n in NAMES) else
                   f"numbers not in the facts: {stray_numbers(text, sources)}" if stray_numbers(text, sources) else None)
        if not problem:
            return text, f"haiku{' (retry)' if attempt == 2 else ''}"
        log(f"post rejected ({problem}): {text[:200]!r}")
        prompt += f"\n\nRejected because it is {problem}. Rejected post: {text[:400]}"
    return tpl, "template"


def fit(text):
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 1] + "…"


def wanted_reply(md, db, st, now):
    """(text, refs, thread) for the oldest wanted post not answered yet that we have facts for, else None."""
    cat, demand = read(md / "catalog.json", {}), read(md / "demand.json", [])
    posts = [w for w in wanted.recent(db, cat, demand, now - WANTED_H * 3600) if str(w["id"]) not in st["replied"]]
    posts.reverse()       # newest first: on 11-03 oldest-first replies came 12 min to 1.8 h late, after the asker had bought
    if not posts:
        return None
    try:
        shops = httpx.get(f"{MARKET}/api/shops", timeout=15).json()["shops"]
    except Exception:
        return None
    for w in posts:
        st["replied"][str(w["id"])] = now
        if recent(st, f"cat:{w['category']}", now):       # the same category answered lately: same shops, same numbers
            continue
        name = wanted.item_name(demand, w["category"])
        held = wanted.stock_by_shop(shops, cat, w["category"])[:3]
        sold = wanted.settles_in(db, cat, w["category"], now - DAY)
        if not held and not sold:
            continue
        pct = sorted(x for _, x in sold)
        if w["korean"]:
            parts = [f"{name} 지금 재고 있는 가게: " + ", ".join(f"{s} {n}개" for s, n in held) if held
                     else f"{name} 지금 재고 있는 가게는 없습니다"]
            if sold:
                parts.append(f"{span(db, now)} 정산 {len(sold)}건, 정가 대비 중앙값 {pct[len(pct) // 2]}% (공개 원장)")
            text = "[재고 안내] " + ". ".join(parts) + "."
        else:
            parts = [f"shops holding {name} now: " + ", ".join(f"{s} ({n} units)" for s, n in held) if held
                     else f"no shop holds {name} right now"]
            if sold:
                parts.append(f"{span(db, now).replace('최근 ', 'last ').replace('시간', ' h')}: {len(sold)} settles, median {pct[len(pct) // 2]}% of list (public ledger)")
            text = "[In stock] " + "; ".join(parts) + "."
        return fit(text), [s for s, _ in sold[-3:]], w["id"], w["category"]
    return None


def shop_check(md, db, now):
    """(text, refs) of the shop check over the last day, or None with too few settles."""
    cat, names, mine = read(md / "catalog.json", {}), shop_names(), model.our_urls(db)
    by = {}
    for r in db.execute("SELECT seq, data FROM ledger WHERE round >= 3 AND kind='settle' AND ts >= ?", (now - DAY,)):
        d = json.loads(r["data"])
        x, price = cat.get(model.sku_of(d)) or {}, d.get("amount") or d.get("price")
        if d.get("shop") in mine or not price or not x.get("list_price"):
            continue
        by.setdefault(d.get("shop"), []).append((r["seq"], d, round(100 * price / x["list_price"])))
    rows = []
    for url, ds in by.items():
        honest = sum(1 for _, d, _ in ds if d.get("claim_honest") is True)
        claimed = sum(1 for _, d, _ in ds if d.get("claim_honest") is not None)
        warranty = sorted({int(d["warranty_hours"]) for _, d, _ in ds if d.get("warranty_hours")})
        pct = sorted(x for _, _, x in ds)
        rows.append({"name": names.get(url, url.rsplit("/", 1)[-1]), "n": len(ds), "honest": honest,
                     "claimed": claimed, "warranty": warranty, "pct": pct[len(pct) // 2], "seqs": [s for s, _, _ in ds]})
    if sum(r["n"] for r in rows) < 5:
        return None
    rows.sort(key=lambda r: (r["honest"] == r["claimed"], -r["n"]))       # a false claim first, then the busiest
    parts = []
    for r in rows[:3]:
        s = f"{r['name']} 정산 {r['n']}건, 등급 주장 {r['honest']}/{r['claimed']} 사실, 정가 대비 중앙값 {r['pct']}%"
        if r["warranty"]:
            s += f", 보증 {'/'.join(map(str, r['warranty']))}시간"
        parts.append(s)
    return fit(f"[가게 점검] {span(db, now)} 공개 원장: " + "; ".join(parts) + "."), [x for r in rows[:3] for x in r["seqs"][-2:]]


def settle_rows(db, cat, since, mine=()):
    """The market's settles since a time with their category and % of the list price, our shop's left out."""
    out = []
    for r in db.execute("SELECT seq, ts, data FROM ledger WHERE round >= 3 AND kind='settle' AND ts >= ?", (since,)):
        d = json.loads(r["data"])
        x, price = cat.get(model.sku_of(d)) or {}, d.get("amount") or d.get("price")
        if d.get("shop") in mine or not price or not x.get("list_price") or not x.get("category"):
            continue
        out.append({"seq": r["seq"], "ts": r["ts"], "d": d, "cat": x["category"],
                    "pct": round(100 * price / x["list_price"])})
    return out


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def arrivals(md, db, st, now):
    """(text, refs) of what other shops took in at the supply windows since our last such post (at most 30 min back),
    by category, most units first; None under 3 units."""
    cat, demand = read(md / "catalog.json", {}), read(md / "demand.json", [])
    since = max(st["arrivals_ts"], now - 1800)
    by, seqs, last = {}, [], since
    for r in db.execute("SELECT seq, ts, data FROM ledger WHERE round >= 3 AND kind='supply' AND ts > ?", (since,)):
        d = json.loads(r["data"])
        c = (cat.get(d.get("sku")) or {}).get("category")
        last = max(last, r["ts"])
        if not c or not d.get("qty") or str(d.get("shop", "")).lower() in NAMES:
            continue
        shops = by.setdefault(c, {})
        shops[d["shop"]] = shops.get(d["shop"], 0) + int(d["qty"])
        seqs.append(r["seq"])
    if sum(sum(s.values()) for s in by.values()) < 3:
        return None
    st["arrivals_ts"] = last
    rows = sorted(by.items(), key=lambda kv: -sum(kv[1].values()))[:3]
    parts = [f"{wanted.item_name(demand, c)} {sum(s.values())}개 (" +
             ", ".join(f"{n} {k}" for n, k in sorted(s.items(), key=lambda kv: -kv[1])[:3]) + ")" for c, s in rows]
    return fit("[입고 소식] 방금 공급 창에서 들어온 물건: " + "; ".join(parts) + ". 공개 원장 기준."), seqs[-3:]


def compare(md, db, st, now):
    """(text, refs, category): shops side by side in one category over the last day, cheapest median first."""
    cat, demand = read(md / "catalog.json", {}), read(md / "demand.json", [])
    names = shop_names()
    by = {}
    for s in settle_rows(db, cat, now - DAY, model.our_urls(db)):       # by name: one owner's shop can have two URLs
        u = s["d"].get("shop")
        by.setdefault(s["cat"], {}).setdefault(names.get(u, u.rsplit("/", 1)[-1]), []).append(s)
    best = None
    for c, shops in by.items():
        ok = {u: xs for u, xs in shops.items() if len(xs) >= 2}
        if len(ok) >= 3 and not recent(st, f"cmp:{c}", now):
            n = sum(len(xs) for xs in ok.values())
            if not best or n > best[0]:
                best = (n, c, ok)
    if not best:
        return None
    _, c, ok = best
    rows = sorted(ok.items(), key=lambda kv: median([s["pct"] for s in kv[1]]))[:4]
    parts = [f"{n} {median([s['pct'] for s in xs])}% ({len(xs)}건)" for n, xs in rows]
    refs = [xs[-1]["seq"] for _, xs in rows]
    return (fit(f"[가격 비교] {wanted.item_name(demand, c)}, {span(db, now)} 가게별 정가 대비 중앙값: " + ", ".join(parts)
                + ". 공개 원장 기준."), refs, c)


def broken_report(md, db, st, now):
    """(text, refs) on goods that broke in the last day, by grade and warranty, when some broke since our last one."""
    rows = db.execute("SELECT seq, data FROM ledger WHERE round >= 3 AND kind='broken' AND ts >= ? ORDER BY seq",
                      (now - DAY,)).fetchall()
    if not rows or rows[-1]["seq"] <= st["broken_seq"]:
        return None
    st["broken_seq"] = rows[-1]["seq"]
    bs = [json.loads(r["data"]) for r in rows]
    units = {b.get("unit") for b in bs}
    grade = {}
    for r in db.execute("SELECT data FROM ledger WHERE round >= 3 AND kind='settle' AND ts >= ?", (now - 2 * DAY,)):
        d = json.loads(r["data"])
        if d.get("unit") in units:
            grade[d["unit"]] = d.get("grade") or "?"
    g = {}
    for b in bs:
        k = grade.get(b.get("unit"), "?")
        g[k] = g.get(k, 0) + 1
    hours = [b["hours_after_sale"] for b in bs if isinstance(b.get("hours_after_sale"), (int, float))]
    warranty = sum(1 for b in bs if b.get("warranty_hours"))
    refunded = sum(1 for b in bs if b.get("refunded"))
    text = (f"[고장 기록] {span(db, now)} 고장 {len(bs)}건, 등급별 " + ", ".join(f"{k} {n}건" for k, n in sorted(g.items()))
            + (f", 판매 후 중앙값 {median(hours):.1f}시간 만에 고장" if hours else "")
            + f". 보증이 붙어 있던 것 {warranty}건, 환불된 것 {refunded}건. 공개 원장 기준.")
    return fit(text), [r["seq"] for r in rows[-3:]]


def summary(md, db, st, now):
    """(text, refs): the market over the last hour (settles, buyers, shops, % of list, busiest categories, grades)."""
    if now - st["last_summary"] < SUMMARY_EVERY:
        return None
    cat, demand = read(md / "catalog.json", {}), read(md / "demand.json", [])
    ss = settle_rows(db, cat, now - 3600)
    if len(ss) < 5:
        return None
    st["last_summary"] = now
    by = {}
    for s in ss:
        by[s["cat"]] = by.get(s["cat"], 0) + 1
    top = sorted(by.items(), key=lambda kv: -kv[1])[:3]
    grades = {}
    for s in ss:
        if s["d"].get("grade"):
            grades[s["d"]["grade"]] = grades.get(s["d"]["grade"], 0) + 1
    n_g = sum(grades.values())
    text = (f"[시장 요약] 지난 1시간 정산 {len(ss)}건, 구매자 {len({s['d'].get('buyer') for s in ss})}명, "
            f"가게 {len({s['d'].get('shop') for s in ss})}곳, 정가 대비 중앙값 {median([s['pct'] for s in ss])}%. "
            "많이 팔린 품목: " + ", ".join(f"{wanted.item_name(demand, c)} {n}건" for c, n in top) + ".")
    if n_g:
        text += " 등급 비율 " + ", ".join(f"{k} {round(100 * v / n_g)}%" for k, v in sorted(grades.items())) + "."
    return fit(text), [s["seq"] for s in ss[-3:]]


def price_trend(md, db, st, now):
    """(text, refs, category) of the busiest category not posted lately: settles and median share of the list price
    in the last TREND_H hours against the hours before, from the public ledger; None with too few settles."""
    cat, demand = read(md / "catalog.json", {}), read(md / "demand.json", [])
    by = {}
    for r in db.execute("SELECT seq, ts, data FROM ledger WHERE round >= 3 AND kind='settle' AND ts >= ?", (now - DAY,)):
        d = json.loads(r["data"])
        x, price = cat.get(model.sku_of(d)) or {}, d.get("amount") or d.get("price")
        if x.get("category") and price and x.get("list_price"):
            by.setdefault(x["category"], []).append((r["seq"], r["ts"], round(100 * price / x["list_price"])))
    start = max(now - DAY, model.round_start(db) or now)
    cut = max(now - TREND_H * 3600, start + (now - start) / 2)      # a young round: its first half against the second
    old_h = round((cut - start) / 3600)
    rows = []
    for c, xs in by.items():
        new = sorted(p for _, t, p in xs if t >= cut)
        old = sorted(p for _, t, p in xs if t < cut)
        if len(new) >= 3 and len(old) >= 3 and old_h >= 1 and not recent(st, f"cat:{c}", now):
            rows.append((len(new), c, new, old, [s for s, t, _ in xs if t >= cut][-3:]))
    if not rows:
        return None
    _, c, new, old, refs = max(rows)
    text = (f"[가격 동향] {wanted.item_name(demand, c)}: 최근 {round((now - cut) / 3600)}시간 정산 {len(new)}건, "
            f"정가 대비 중앙값 {median(new)}% (범위 {new[0]}~{new[-1]}%), 그 전 {old_h}시간은 {len(old)}건 "
            f"{median(old)}%")
    return fit(text + ". 공개 원장 기준."), refs, c


def span(db, now, hours=24):
    """'최근 N시간' for a window of `hours`, N cut to the hours since Round 3 began so a young round is not overstated."""
    start = model.round_start(db) or now
    return f"최근 {max(1, min(hours, math.ceil((now - start) / 3600)))}시간"


def recent(st, topic, now):
    """True when a post on this topic (a category, the shop check) went out less than TOPIC_H hours ago."""
    return now - st["topics"].get(topic, 0) < TOPIC_H * 3600


def send(md, st, now, text, refs, how, thread=None, topic=None, extra=None):
    """Queue one post unless the same text is among our last 300 posts; True when it was queued."""
    if text in st["texts"]:
        log(f"skipped a repeat ({how}): {text[:120]}")
        return False
    line = {"post": text, "refs": refs, "as": "buyer"}
    if thread is not None:
        line["thread"] = thread
    queue_post(md, line)
    st["last_post"] = now
    st["texts"] = (st["texts"] + [text])[-300:]
    if topic:
        st["topics"][topic] = now
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "text": text, "refs": refs, "how": how, "thread": thread, **(extra or {})},
                           ensure_ascii=False) + "\n")
    log(f"posted ({how}): {text[:120]}")
    return True


def post_wanted(md, db, st, now):
    r = wanted_reply(md, db, st, now)
    return bool(r) and send(md, st, now, r[0], r[1], "wanted reply", thread=r[2], topic=f"cat:{r[3]}")


def post_purchases(md, db, st, now):
    rows = []
    try:
        rows = [json.loads(x) for x in PURCHASES.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        pass
    new = [r for r in rows if r.get("ledger") not in st["posted"] and r.get("ts", 0) > st["since"]]
    if not new:
        return False
    new.sort(key=lambda r: (r.get("claim_honest") is not False, -r.get("ts", 0)))     # false claims first, newest
    pick = new[:MAX_ITEMS]
    st["posted"] = (st["posted"] + [r.get("ledger") for r in pick])[-500:]
    text, how = template(facts(pick, shop_names())), "template"     # 11-04: code only, a model could misstate a grade
    refs = [r["ledger"] for r in pick if isinstance(r.get("ledger"), int)]
    return send(md, st, now, text, refs, how, extra={"waiting": len(new) - len(pick)})


def post_trend(md, db, st, now):
    r = price_trend(md, db, st, now)
    return bool(r) and send(md, st, now, r[0], r[1], "price trend", topic=f"cat:{r[2]}")


def post_shop_check(md, db, st, now):
    if now - st["last_digest"] < DIGEST_EVERY or not (c := shop_check(md, db, now)):
        return False
    st["last_digest"] = now
    return send(md, st, now, c[0], c[1], "shop check")


def post_arrivals(md, db, st, now):
    r = arrivals(md, db, st, now)
    return bool(r) and send(md, st, now, r[0], r[1], "arrivals")


def post_compare(md, db, st, now):
    r = compare(md, db, st, now)
    return bool(r) and send(md, st, now, r[0], r[1], "compare", topic=f"cmp:{r[2]}")


def post_broken(md, db, st, now):
    r = broken_report(md, db, st, now)
    return bool(r) and send(md, st, now, r[0], r[1], "broken")


def post_summary(md, db, st, now):
    r = summary(md, db, st, now)
    return bool(r) and send(md, st, now, r[0], r[1], "summary")


KINDS = {"wanted": post_wanted, "purchases": post_purchases, "trend": post_trend,     # arrivals off (11-04: both such
                                                                                      # posts got a dislike, 5/1 each)
         "compare": post_compare, "broken": post_broken, "shop check": post_shop_check, "summary": post_summary}


def fresh_wanted(md, db, st, now):
    """A wanted post under WANTED_JUMP_S old that we have not answered: worth more now than in a few turns."""
    cat, demand = read(md / "catalog.json", {}), read(md / "demand.json", [])
    return any(str(w["id"]) not in st["replied"] for w in wanted.recent(db, cat, demand, now - WANTED_JUMP_S))


def one_pass(md, st, now):
    """Every POST_EVERY_S one post, the kinds taken in turn (after the last kind posted, the next one with
    something new to say), so the forum sees every kind, not one over and over. A fresh wanted post goes first."""
    st["replied"] = {k: v for k, v in st["replied"].items() if now - v < DAY}
    st["topics"] = {k: v for k, v in st["topics"].items() if now - v < DAY}
    if now - st["last_post"] < POST_EVERY_S:
        return
    db = model.connect()
    names = list(KINDS)
    start = (names.index(st["last_kind"]) + 1) % len(names) if st["last_kind"] in names else 0
    order = names[start:] + names[:start]
    if st["last_kind"] != "wanted" and fresh_wanted(md, db, st, now):
        order = ["wanted"] + [k for k in order if k != "wanted"]
    for k in order:
        if KINDS[k](md, db, st, now):
            st["last_kind"] = k
            return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    md = Path(a.dir) / ".market"
    while True:
        now = time.time()
        st = {"last_post": 0, "posted": [], "since": now, "replied": {}, "last_digest": 0, "topics": {}, "texts": [],
              "last_kind": None, "arrivals_ts": 0, "broken_seq": 0, "last_summary": 0, **read(STATE, {})}     # purchases before the first run stay
        try:
            if read(md / "inventory.json", {}).get("round", 0) >= 3:
                one_pass(md, st, now)
        except Exception as e:
            log(f"skipped: {type(e).__name__}: {str(e)[:200]}")
        policy.DATA.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE)
        if a.once:
            return
        time.sleep(5)             # a coarse tick would stretch the 5 min interval


if __name__ == "__main__":
    main()
