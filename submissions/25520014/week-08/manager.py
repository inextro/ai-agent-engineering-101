"""Week 08 manager: the slow loop. Every hour opus reads a summary of the market and of our shop and moves the policy.

    python manager.py --dir .              # runs forever, one decision an hour
    python manager.py --dir . --once       # one decision now (testing)

A parameter-level meta-harness: the code that decides (restock.py, seller.py) runs on data/policy.json, and this
loop rewrites that file from the record of how the code did. It never touches the posteriors (model.py computes
them from data); it moves what data alone cannot settle: half-life and prior weight when demand shifts, money
rules, margin, concessions, C discount, warranty by grade, the liquidation start. policy.clip keeps every value in
its range and within a quarter of the range per decision, so one bad answer cannot wreck the shop.

Model: opus at medium effort; when it fails, sonnet at medium. Each decision is a line in data/manager.jsonl
(summary size, the answer, what changed after clipping, the reason).

Forum: one shop post an hour and one whenever the policy changes, built by code from the policy and the ledger
(no claims the shop does not keep), written to .market/outbox.jsonl for the kit to send. Shop posts do not count
for the shop's score; they tell buyers what the shop guarantees, with ledger refs to check it.
"""
import argparse
import json
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

import harness
import model
import policy

EVERY = 3600
MODELS = (("opus", "medium"), ("sonnet", "medium"))
LOG = policy.DATA / "manager.jsonl"


def log(msg):
    print(f"{time.strftime('%m-%d %H:%M:%S')} [manager] {msg}", flush=True)


def jsonl(path, since):
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("ts", 0) >= since:
                out.append(rec)
    except OSError:
        pass
    return out


def summary(md, now):
    """What opus sees: numbers by code, plus the newest forum posts and our reviews as text."""
    p = policy.load()
    est = model.Estimates(pol=p, now=now)
    db = est.db
    inv = json.loads((md / "inventory.json").read_text(encoding="utf-8")) if (md / "inventory.json").exists() else {}
    units = inv.get("units", [])
    hour = now - 3600
    negs = jsonl(model.NEGOTIATIONS, now - 6 * 3600)
    opened = [r for r in negs if r["outcome"] == "open"]
    deals = [r for r in negs if r["outcome"] == "deal"]
    mine = model.our_urls(db)
    our_settles = [(ts, d) for ts, d in est.sales if d.get("shop") in mine]
    reviews = [(d.get("stars"), d.get("review")) for ts, d in our_settles[-15:] if d.get("review") or d.get("stars")]
    honest = [d.get("claim_honest") for _, d in our_settles if "claim_honest" in d]
    posts = db.execute("SELECT id, ts, author, role, text FROM forum_posts ORDER BY id DESC LIMIT 30").fetchall()
    votes = {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT post, likes, dislikes FROM forum_votes v WHERE polled_at = "
        "(SELECT MAX(polled_at) FROM forum_votes WHERE post = v.post)")}
    restocks = jsonl(policy.DATA / "restock.jsonl", hour)
    missed = jsonl(policy.DATA / "missed.jsonl", hour)
    bandit = model.bandit_stats(now=now)
    hot = sorted(est.categories, key=est.rate, reverse=True)[:12]
    s = {
        "time": time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(now)),
        "hours_left_in_round": round(policy.hours_left(now), 1),
        "balance": inv.get("balance"), "start": inv.get("start"), "injected": inv.get("injected"),
        "profit_so_far": (inv.get("balance") or 0) - (inv.get("start") or 0) - (inv.get("injected") or 0),
        "stock": {"units": len(units), "food_units": sum(u.get("kind") == "food" for u in units),
                  "goods_units": sum(u.get("kind") == "goods" for u in units),
                  "at_cost": round(sum(u.get("cost", 0) for u in units)),
                  "grades": dict(Counter(u.get("grade") for u in units)),
                  "food_expiring_6h": sum(1 for u in units if u.get("expires_at") and u["expires_at"] - now < 6 * 3600)},
        "held_categories": len(json.loads((policy.DATA / "restock_state.json").read_text())["held"])
                           if (policy.DATA / "restock_state.json").exists() else None,
        "restock_last_hour": {"runs": len(restocks), "units_bought": sum(b["qty"] for r in restocks for b in r["buy"])},
        "negotiations_6h": {"opened": len(opened), "deals": len(deals),
                            "deal_price_over_list": round(sum(d["price"] / d["list_price"] for d in deals) / len(deals), 3)
                            if deals else None,
                            "deal_margin_over_cost": round(sum(d["price"] / d["cost"] - 1 for d in deals) / len(deals), 3)
                            if deals else None,
                            "steps_at_close": dict(Counter(d.get("step") for d in deals))},
        "price_bandit_mean_reward": {f"{k}@{a}": round(v[0] / sum(v), 3) for (k, a), v in bandit.items()},
        "requests_for_items_not_in_stock_last_hour": len(missed),
        "market": {"settles_in_window": len(est.sales), "lambda0_per_category_per_hour": round(est.lambda0, 3),
                   "our_share": round(est.share_mean, 4), "our_settles": len(our_settles),
                   "hottest_categories": [(c, round(est.rate(c), 2)) for c in hot]},
        "break_rate_by_grade": {g: round(r, 3) for g, r in est.break_rate.items()},
        "break_counts_by_grade(breaks, exposure)": est.break_counts,
        "our_claims_honest": f"{sum(1 for h in honest if h)}/{len(honest)}",
        "our_recent_reviews": reviews,
        "forum_newest": [{"id": r[0], "author": r[2], "role": r[3], "text": (r[4] or "")[:300],
                          "likes_dislikes": votes.get(r[0])} for r in posts],
    }
    return p, s


PROMPT = """You manage the policy of a shop in an agent marketplace (Round 3, 14 days). The shop's code sells
units it bought from the market, negotiates with buyer agents, and restocks every minute by a newsvendor rule on a
gamma-Poisson demand estimate. You do not trade; you set the policy numbers the code runs on, once an hour.

Score (percentiles among shops): 0.5 x profit (balance - 5,000,000 - bankruptcy money; unsold stock counts as a
loss) + 0.3 x revenue + 0.2 x stars. Food rots (thrown away at cost); goods pay 1% of cost a day in storage and a
hidden share breaks within 6 h of the sale (a warranty refunds the full price from our balance). Buyers see only
shops that hold stock in the ordered category. Our claims are always true and certificates always shown.

Policy now, with the allowed range of each number (a change moves at most a quarter of the range):
{policy}

The record (code-computed):
{summary}

Decide what to change, if anything. Change a value only for a reason in the record; no change is a fine answer.
Answer with one JSON object only:
{{"changes": {{"<name>": <value>, ...}}, "reason": "<two sentences: what in the record, what you changed>"}}"""


def describe(p):
    lines = [f"  {k} = {p[k]}  (range {lo} .. {hi})" for k, (_, lo, hi) in policy.NUMBERS.items()]
    lines += [f"  {k} = {p[k]}  (true/false)" for k in policy.FLAGS]
    return "\n".join(lines)


def decide(md, now):
    p, s = summary(md, now)
    prompt = PROMPT.format(policy=describe(p), summary=json.dumps(s, ensure_ascii=False, indent=1))
    answer, used = None, None
    for mdl, effort in MODELS:
        t0 = time.time()
        try:
            reply = harness.call(prompt, timeout=600, model=mdl, effort=effort)
        except Exception as e:
            log(f"{mdl} failed after {time.time() - t0:.0f}s: {type(e).__name__} {str(e)[:150]}")
            continue
        m = re.search(r"\{.*\}", reply, re.S)
        try:
            answer, used = json.loads(m.group()), f"{mdl} {effort} {time.time() - t0:.0f}s"
            break
        except (AttributeError, ValueError):
            log(f"{mdl} answer had no JSON: {reply[:200]!r}")
    if answer is None:
        log("no decision this hour; the policy stays")
        return p, False
    new = policy.clip(answer.get("changes") or {}, p)
    changed = {k: (p[k], new[k]) for k in list(policy.NUMBERS) + list(policy.FLAGS) if new[k] != p[k]}
    reason = str(answer.get("reason", ""))[:600]
    if changed:
        new = policy.save(new, reason)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "model": used, "asked": answer.get("changes"), "changed": changed,
                            "reason": reason, "prompt_chars": len(prompt)}, ensure_ascii=False) + "\n")
    log(f"{used}: {'changed ' + json.dumps(changed, ensure_ascii=False) if changed else 'no change'} | {reason}")
    return new, bool(changed)


def shop_post(md, p, est_db):
    """The shop's forum post: what it guarantees now, with the newest of our settles as refs to check it."""
    mine = model.our_urls(est_db)
    refs = [r[0] for r in est_db.execute(
        "SELECT seq, data FROM ledger WHERE round >= 3 AND kind='settle' ORDER BY seq DESC LIMIT 2000")
        if json.loads(r[1]).get("shop") in mine][:3]
    grades = [g for g in "ABC" if p[f"warranty_{g}"]]
    warranty = (f"a {int(p['warranty_hours'])}-hour warranty on goods of grade {'/'.join(grades)}"
                if grades and p["warranty_hours"] > 0 else "no warranty on goods for now")
    text = (f"[{model.SHOP_NAME}] Certificates always shown, grades stated truthfully (check claim_honest on our "
            f"settles), {warranty}. Food and goods across many categories, restocked every minute. Ask for the item "
            "by SKU and send your offer as a number.")
    with open(md / "outbox.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"post": text, "refs": refs, "as": "shop"}, ensure_ascii=False) + "\n")
    log(f"forum post queued (refs {refs})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    md = Path(a.dir) / ".market"
    while True:
        now, inv = time.time(), {}
        try:
            inv = json.loads((md / "inventory.json").read_text(encoding="utf-8"))
            if inv.get("round", 0) >= 3:
                p, changed = decide(md, now)
                shop_post(md, p, model.connect())
            else:
                log(f"round {inv.get('round')}: waiting for Round 3")
        except (OSError, ValueError, sqlite3.Error) as e:
            log(f"skipped: {type(e).__name__}: {str(e)[:200]}")
        if a.once:
            return
        time.sleep(max(60, EVERY - (time.time() - now)) if inv.get("round", 0) >= 3 else 120)


if __name__ == "__main__":
    main()
