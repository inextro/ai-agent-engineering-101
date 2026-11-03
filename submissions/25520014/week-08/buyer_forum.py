"""Week 08 buyer forum: our buyer posts what it bought, as facts anyone can check on the ledger.

    python buyer_forum.py --dir .            # runs forever, checks every minute
    python buyer_forum.py --dir . --once     # one pass (testing)

Buyer posts are 0.3 (votes from other owners' agents with 5+ deals) + 0.1 (how many we wrote) of the buyer's score, so each post should
be worth reading to another buyer: which shop sold what at what share of the list price, whether the grade was as
claimed, the warranty. Only facts the market signed (data/purchases.jsonl, written by review.py from deal.json),
with the ledger entries in refs so a reader can check them. Never how our buyer chooses shops or haggles, never our
own shop (we cannot buy there; naming it reads as an ad), no advice, no attacks.

One post every post_every_h hours (buyer_policy), the first of these that has something to say (11-03 policy: be
the buyer other agents read for facts they cannot get alone):
  1. a reply to a wanted post (wanted.py, last WANTED_H hours): which shops hold that category now (GET /api/shops),
     and what it sold for on the ledger; never our own shop, nothing when no shop holds it and nothing sold
  2. our purchases not posted yet, a false grade claim first
  3. a shop check (at most every DIGEST_EVERY): per shop over the last day, settles, how many grade claims were true,
     the warranty given and the median share of the list price, from the public ledger
Replies and the shop check are code templates (every number is computed, refs cite the ledger); purchases go through
haiku as before. haiku writes it from the facts (the buyer manager's forum guidance in front); a post with a number the
facts do not give, our shop's name, or over MAX_CHARS is retried once, then the template goes out instead.
"""
import argparse
import json
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
WANTED_H = 2              # wanted posts older than this get no reply (the order behind them has likely expired)
DIGEST_EVERY = 3600
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
        s = f"{f['item']} at {f['shop']}"
        if "pct_of_list" in f:
            s += f": {f['pct_of_list']}% of list"
        if f.get("grade_as_claimed") is False:
            s += ", grade NOT as claimed"
        elif f.get("grade"):
            s += f", grade {f['grade']} as claimed" if f.get("grade_as_claimed") else f", grade {f['grade']}"
        if f.get("warranty_hours"):
            s += f", {f['warranty_hours']}h warranty"
        parts.append(s)
    text = "Recent buys (receipts in refs): " + "; ".join(parts) + "."
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 1] + "…"


PROMPT = """{guide}You write a short forum post on an agent marketplace, as a buyer, about purchases you just made.
Other buyers read it to choose shops. The facts (from the market's signed receipts):
{facts}

Write one post in English, at most {chars} characters, plain text. Say per purchase: the item, the shop, the share of
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
    if not posts:
        return None
    try:
        shops = httpx.get(f"{MARKET}/api/shops", timeout=15).json()["shops"]
    except Exception:
        return None
    for w in posts:
        st["replied"][str(w["id"])] = now
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
                parts.append(f"최근 24시간 정산 {len(sold)}건, 정가 대비 중앙값 {pct[len(pct) // 2]}% (공개 원장)")
            text = "[재고 안내] " + ". ".join(parts) + "."
        else:
            parts = [f"shops holding {name} now: " + ", ".join(f"{s} ({n} units)" for s, n in held) if held
                     else f"no shop holds {name} right now"]
            if sold:
                parts.append(f"last 24 h: {len(sold)} settles, median {pct[len(pct) // 2]}% of list (public ledger)")
            text = "[In stock] " + "; ".join(parts) + "."
        return fit(text), [s for s, _ in sold[-3:]], w["id"]
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
    return fit("[가게 점검] 최근 24시간 공개 원장: " + "; ".join(parts) + "."), [x for r in rows[:3] for x in r["seqs"][-2:]]


def send(md, st, now, text, refs, how, thread=None):
    line = {"post": text, "refs": refs, "as": "buyer"}
    if thread is not None:
        line["thread"] = thread
    queue_post(md, line)
    st["last_post"] = now
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "text": text, "refs": refs, "how": how, "thread": thread},
                           ensure_ascii=False) + "\n")
    log(f"posted ({how}): {text[:120]}")


def one_pass(md, st, now):
    p = buyer_policy.load()
    if now - st["last_post"] < p["post_every_h"] * 3600:
        return
    db = model.connect()
    r = wanted_reply(md, db, st, now)
    st["replied"] = {k: v for k, v in st["replied"].items() if now - v < DAY}
    if r:
        send(md, st, now, r[0], r[1], "wanted reply", thread=r[2])
        return
    rows = []
    try:
        rows = [json.loads(x) for x in PURCHASES.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        pass
    new = [r for r in rows if r.get("ledger") not in st["posted"] and r.get("ts", 0) > st["since"]]
    if not new:
        if now - st["last_digest"] >= DIGEST_EVERY and (c := shop_check(md, db, now)):
            st["last_digest"] = now
            send(md, st, now, c[0], c[1], "shop check")
        return
    new.sort(key=lambda r: (r.get("claim_honest") is not False, -r.get("ts", 0)))     # false claims first, newest
    pick = new[:MAX_ITEMS]
    fs = facts(pick, shop_names())
    text, how = write(fs)
    refs = [r["ledger"] for r in pick if isinstance(r.get("ledger"), int)]
    queue_post(md, {"post": text, "refs": refs, "as": "buyer"})
    st["last_post"] = now
    st["posted"] = (st["posted"] + [r.get("ledger") for r in pick])[-500:]
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "text": text, "refs": refs, "how": how, "waiting": len(new) - len(pick)},
                           ensure_ascii=False) + "\n")
    log(f"posted ({how}, {len(pick)} purchases, {len(new) - len(pick)} left): {text[:120]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    md = Path(a.dir) / ".market"
    while True:
        now = time.time()
        st = {"last_post": 0, "posted": [], "since": now, "replied": {}, "last_digest": 0, **read(STATE, {})}     # purchases before the first run stay
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
        time.sleep(60)


if __name__ == "__main__":
    main()
