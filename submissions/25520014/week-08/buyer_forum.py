"""Week 08 buyer forum: our buyer posts what it bought, as facts anyone can check on the ledger.

    python buyer_forum.py --dir .            # runs forever, checks every minute
    python buyer_forum.py --dir . --once     # one pass (testing)

Buyer posts are a third of the buyer's score (votes from other owners' agents with 5+ deals), so each post should
be worth reading to another buyer: which shop sold what at what share of the list price, whether the grade was as
claimed, the warranty. Only facts the market signed (data/purchases.jsonl, written by review.py from deal.json),
with the ledger entries in refs so a reader can check them. Never how our buyer chooses shops or haggles, never our
own shop (we cannot buy there; naming it reads as an ad), no advice, no attacks.

One post every post_every_h hours (buyer_policy), only when there are purchases not posted yet; a false grade claim
goes first. haiku writes it from the facts (the buyer manager's forum guidance in front); a post with a number the
facts do not give, our shop's name, or over MAX_CHARS is retried once, then the template goes out instead.
"""
import argparse
import json
import re
import time
from pathlib import Path

import buyer_policy
import harness
import model
import policy
from forum import NAMES, queue_post, read, stray_numbers

STATE = policy.DATA / "buyer_forum_state.json"
LOG = policy.DATA / "buyer_forum.jsonl"
PURCHASES = policy.DATA / "purchases.jsonl"
MODEL, EFFORT = "haiku", "medium"
MAX_ITEMS = 3
MAX_CHARS = 280           # the forum shows about 300 characters


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


def one_pass(md, st, now):
    p = buyer_policy.load()
    if now - st["last_post"] < p["post_every_h"] * 3600:
        return
    rows = []
    try:
        rows = [json.loads(x) for x in PURCHASES.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        pass
    new = [r for r in rows if r.get("ledger") not in st["posted"] and r.get("ts", 0) > st["since"]]
    if not new:
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
        st = {"last_post": 0, "posted": [], "since": now, **read(STATE, {})}     # purchases before the first run stay
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
