"""Week 08 buyer manager: the buyer's slow loop. Every hour opus reviews the buyer's last hours and sets what comes next.

    python buyer_manager.py --dir .              # runs forever, one decision an hour
    python buyer_manager.py --dir . --once       # one decision now (testing)

The seller has manager.py; the buyer has this one, apart, so each reviews one side. Same shape: the code that buys
(market_buyer.py, buyer_forum.py) runs on data/buyer_policy.json and data/buyer_playbook.json, and this loop rewrites
them from the record. It decides on its own and reports after the fact (data/buyer_reports.md and Telegram); the owner
reads the report and corrects the direction when needed. It never touches the price model or the break rates
(model.py computes them from the ledger), and buyer_policy.clip keeps every number inside its range.

opus sees, all computed by code: orders and deals of the last hour and the last 6 hours (how orders ended, share of
list paid, surplus, seconds, shops tried, template messages), the last orders one by one (asks and offers), the
stars we gave and false claims we met, our buyer posts and their votes, settles this round against the 20 needed,
its last decisions with the hour before and after each, its notes and its memory (data/buyer_manager_memory.md).
It answers with number changes, playbook edits (negotiation and forum guidance, no digits), notes, the new memory,
problems (a code problem goes to the owner, who fixes code) and a report in Korean.

Model: opus at medium effort; when it fails, sonnet at medium. Each decision is a line in data/buyer_manager.jsonl.
"""
import argparse
import json
import os
import time
from collections import Counter
from pathlib import Path

import buyer_policy
import rules
import model
import notify
import policy
from manager import REPORT_CHARS, REPORT_SPEC, UPVOTE_RULE, WHY_SPEC, ask_model, change_lines, jsonl, upvote

os.environ["TZ"] = "Asia/Seoul"
time.tzset()

LOG = policy.DATA / "buyer_manager.jsonl"
REPORTS = policy.DATA / "buyer_reports.md"
MEMORY = policy.DATA / "buyer_manager_memory.md"
ORDERS = policy.DATA / "buyer.jsonl"
PURCHASES = policy.DATA / "purchases.jsonl"
POSTS = policy.DATA / "buyer_forum.jsonl"
MEMORY_CHARS = 1500
ORDERS_SHOWN = 12
PAYMENTS = Path(".market") / "payments.jsonl"      # set from --dir in main: the kit's record of what it paid
OFFSET = 300               # five minutes after the seller's manager, so the two opus calls do not overlap


def log(msg):
    print(f"{time.strftime('%m-%d %H:%M:%S')} [buyer-manager] {msg}", flush=True)


def refusal_kind(why):
    """Why the kit did not pay an agreed deal, as a kind; the reason itself is kept whole in the examples (cut at 60
    characters it lost the hours the order asked for, and the manager called a correct refusal a bug, 11-04)."""
    w = why.lower()
    if "shelf life" in w:
        return "food lasts less than the order's use_within_hours (+0.5 h): correct refusal"
    if "grade" in w:
        return "grade not worth the price under our accept policy"
    if "certificate" in w:
        return "no certificate"
    if "not in stock" in w:
        return "the shop no longer had the unit at checkout"
    return "other: " + why[:120]


def window(since, until):
    """One window of the buyer, from its own order log."""
    every = [r for r in jsonl(ORDERS, since) if r["ts"] < until]
    rs = [r for r in every if r.get("kind")]      # orders with no kind are the old chair/bookshelf/lamp kind: no
                                                  # SKU sells for them and the board does not count them
    agreed = [r for r in rs if r.get("end") == "deal" and r.get("price") and r.get("list")]
    paid = {}
    try:
        for line in PAYMENTS.read_text(encoding="utf-8").splitlines():
            try:
                x = json.loads(line)
                paid[x.get("order")] = x
            except ValueError:
                pass
    except OSError:
        pass
    deals = [r for r in agreed if (paid.get(r.get("order")) or {}).get("paid")]   # agreed and paid by the kit
    msgs = Counter()
    for r in rs:
        msgs.update(r.get("messages") or {})
    refused = [(r, str(paid[r["order"]].get("reason"))) for r in agreed
               if r.get("order") in paid and not paid[r["order"]].get("paid")]
    kinds = Counter(refusal_kind(why) for _, why in refused)
    return {"orders": len(rs), "old_kind_orders_skipped": len(every) - len(rs),
            "agreed": len(agreed), "paid_deals": len(deals),
            "kit_refused": {k: {"times": n, "examples": [
                f"{why} (order use_within_hours {r.get('use_within_hours')}, kind {r.get('kind')})"
                for r, why in refused if refusal_kind(why) == k][:2]} for k, n in kinds.most_common()},
            "ends": dict(Counter(str(r.get("end"))[:60] for r in rs).most_common(8)),
            "price_over_list": round(sum(r["price"] / r["list"] for r in deals) / len(deals), 3) if deals else None,
            "surplus_sum": sum(r["list"] - r["price"] for r in deals),
            "mean_secs": round(sum(r.get("secs", 0) for r in rs) / len(rs), 1) if rs else None,
            "second_shop_tried": sum(1 for r in rs if (r.get("tried") or 0) > 1),
            "messages_by_model": msgs.get("model", 0), "messages_by_template": msgs.get("template", 0)}


def recent_decisions(now, n=6):
    out = []
    for r in jsonl(LOG, 0)[-n:]:
        t = r["ts"]
        out.append({"when": time.strftime("%m-%d %H:%M", time.localtime(t)), "changed": r.get("changed"),
                    "playbook_changed": r.get("playbook_changed"), "reason": r.get("reason"),
                    "hour_before": window(t - 3600, t), "after": window(t, min(t + 3600, now))})
    return out


def summary(md, now):
    p, b = buyer_policy.load(), buyer_policy.load_playbook()
    db = model.connect()
    us = model.SHOP_NAME
    settles = [json.loads(r[0]) for r in db.execute("SELECT data FROM ledger WHERE round >= 3 AND kind='settle'")]
    ours = [d for d in settles if d.get("buyer") == us]
    bought = jsonl(PURCHASES, now - 6 * 3600)
    posts = jsonl(POSTS, now - 24 * 3600)
    votes = {}
    try:
        for r in db.execute("SELECT post, likes, dislikes FROM forum_votes v WHERE polled_at = "
                            "(SELECT MAX(polled_at) FROM forum_votes WHERE post = v.post)"):
            votes[r[0]] = (r[1], r[2])
        mine = db.execute("SELECT id, text FROM forum_posts WHERE role='buyer' AND lower(author) LIKE ? "
                          "ORDER BY id DESC LIMIT 10", (f"%{us.lower()}%",)).fetchall()
        others = db.execute("SELECT id, author, role, text FROM forum_posts WHERE lower(author) NOT LIKE ? "
                            "ORDER BY id DESC LIMIT 30", (f"%{us.lower()}%",)).fetchall()
    except Exception:
        mine, others = [], []
    last = [r for r in jsonl(ORDERS, now - 3600) if r.get("kind")][-ORDERS_SHOWN:]
    s = {
        "time": time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(now)),
        "hours_left_in_round": round(policy.hours_left(now), 1),
        "our_buyer_settles_this_round_vs_20_needed_to_rank": [len(ours), 20],
        "market_settles_this_round": len(settles),
        "last_hour": window(now - 3600, now),
        "last_6_hours": window(now - 6 * 3600, now),
        "orders_last_hour": [{k: r.get(k) for k in ("category", "kind", "use_within_hours", "budget", "shop", "list", "cap", "expect",
                                                     "quotes", "asks", "offers", "end", "price", "secs", "tried")}
                             for r in last],
        "our_reviews_6h": {"stars": dict(Counter(r.get("stars") for r in bought)),
                           "false_grade_claims": sum(1 for r in bought if r.get("claim_honest") is False)},
        "our_buyer_posts_24h": [{"text": r.get("text", "")[:280], "how": r.get("how")} for r in posts[-6:]],
        "our_buyer_posts_on_the_forum_with_votes(likes, dislikes)": [
            {"id": r[0], "text": (r[1] or "")[:200], "votes": votes.get(r[0])} for r in mine],
        "forum_newest_by_others": [{"id": r[0], "author": r[1], "role": r[2], "text": (r[3] or "")[:300]}
                                   for r in others],
        "your_last_decisions_and_what_followed": recent_decisions(now),
        "your_notes_from_last_hour": (jsonl(LOG, 0)[-1:] or [{}])[0].get("notes"),
    }
    return p, b, s


PROMPT = """You manage the buying side of an agent in a marketplace (Round 3, 14 days). The buyer's code gets an order
(a category, a list of SKUs, a budget, for food a use_within_hours), picks shops by expected surplus from a price
model on the public ledger, asks the best few shops for a price at once, haggles with the one whose ask leaves the most
surplus (the code chooses every offer; haiku writes the words), and pays only up to cap = min(value, budget). After a
deal it rates the shop from the market's receipt and posts its purchases on the forum as facts. You do not trade.
Once an hour you review the record and set what the code runs on next. The owner does not watch; they read your
report afterwards and correct you when needed.

Score (percentiles among buyers; the rules page below has the current formula): 0.5 x surplus (list price of what
was useful - price paid + warranty refunds) + 0.3 x votes on our buyer posts (from other owners' agents with 5+
deals) + 0.1 x number of deals + 0.1 x number of our buyer posts (hidden posts do not count). At least 20
settled deals are needed to rank. The kit stops an order after 300 s.

Policy now, with the allowed range of each number (a change moves at most a quarter of the range):
{policy}

Playbook now (text the code runs on; you may rewrite it):
  negotiation guidance (in front of haiku's prompt when it writes our offer): {negotiation}
  forum guidance (in front of haiku's prompt when it writes our buyer post): {forum}
Guidance steers words only: it cannot change an offer, the cap or which shop we pick, and it must contain no digits
and no words for percents or fractions (such guidance is refused). At most {chars} characters each. The posts must
stay facts from receipts: never how we choose shops or haggle, never our own shop, no attacks.

What the code does by design (guidance cannot change it): offers always rise and never pass min(ask, cap); we accept
at or under the shop's usual price, when the shop meets our offer, when it stops moving under the cap, or in the last
round; a second shop is opened only when the first ends without a deal and enough time is left. If the code itself
looks wrong (a bug, orders failing for one reason), say so in "problems" and the report; the owner fixes code.
Before calling something a bug, check it against the rules page below and the record: a refusal or a number that follows from the rules (an order asking more hours than a unit has, a supply window that is closed) is not a bug. "[...]" marks where this record shortened a long text; the buyer and the shop saw it whole.

The record (code-computed):
{summary}

Your memory (lessons so far, written by you):
{memory}

Review, in this order:
1. Your last decisions: what followed? Undo or adjust what did not work.
2. Anything wrong: deals far under the orders, many orders ending the same way, template messages, posts without
   votes or with dislikes, too few deals for the 20 needed.
3. What to change: numbers, playbook, or nothing. Change only for a reason in the record; no change is a fine answer.
   Mind the trade: lower offers raise surplus per deal but lose deals.
Answer with one JSON object only:
{{"review": "<what your last decisions did, or that there were none>",
 "problems": ["<each thing wrong you found>"],
 "changes": {{"<number name>": <value>, ...}},
 "playbook": {{"negotiation": "<the full new text, or null to keep>", "forum": "<the full new text, or null to keep>"}},
 "reason": "<two sentences: what in the record, what you changed>",
 {why_spec}
 "notes": "<what to watch next hour, for yourself>",
 "upvote": [{{"id": <post id>, "why": "<in Korean, a few words: what in it you used>"}}, ...],
 "memory": "<the full new memory, at most {mem_chars} characters, each lesson with the hours it rests on>",
 "report_ko": "{report_spec}"}}"""


def describe(p):
    return "\n".join(f"  {k} = {p[k]}  (range {lo} .. {hi})" for k, (_, lo, hi) in buyer_policy.NUMBERS.items())


def decide(md, now):
    p, b, s = summary(md, now)
    memory = MEMORY.read_text(encoding="utf-8") if MEMORY.exists() else ""
    prompt = PROMPT.format(policy=describe(p), negotiation=json.dumps(b["negotiation"] or "(none)"),
                           forum=json.dumps(b["forum"] or "(none)"), chars=policy.GUIDE_CHARS,
                           summary=json.dumps(s, ensure_ascii=False, indent=1), memory=memory or "(empty)",
                           mem_chars=MEMORY_CHARS, report_spec=REPORT_SPEC, why_spec=WHY_SPEC.format(skip="")) + "\n\n" + UPVOTE_RULE + rules.section("buyer")
    answer, used = ask_model(prompt)
    if answer is None or "review" not in answer:
        log("no decision this hour; the policy stays")
        report(now, used, {}, {}, [], "모델 호출이 실패해 이번 시간은 판단하지 못했습니다. buyer 정책과 playbook은 그대로입니다.")
        return
    for k in ("changes", "playbook"):
        if not isinstance(answer.get(k), dict):
            answer[k] = {}
    new = buyer_policy.clip(answer["changes"], p)
    changed = {k: (p[k], new[k]) for k in buyer_policy.NUMBERS if new[k] != p[k]}
    reason = str(answer.get("reason", ""))[:600]
    if changed:
        buyer_policy.save(new, reason)
    nb, refused = buyer_policy.clip_playbook(answer["playbook"], b)
    pb_changed = {k: (b.get(k), nb.get(k)) for k in buyer_policy.GUIDE_KEYS if nb.get(k) != b.get(k)}
    if pb_changed:
        buyer_policy.save_playbook(nb, reason)
    mem = answer.get("memory")
    if isinstance(mem, str) and mem.strip() and mem.strip() != memory.strip():
        if len(mem) > MEMORY_CHARS:
            refused.append(f"memory: longer than {MEMORY_CHARS} characters, kept the old one")
        else:
            MEMORY.write_text(mem.strip() + "\n", encoding="utf-8")
    liked = upvote(md, answer.get("upvote"), model.connect(), "buyer")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "model": used, "upvoted": liked, "asked": answer.get("changes"), "changed": changed,
                            "playbook_asked": answer.get("playbook"), "playbook_changed": pb_changed, "why": answer.get("why_ko"),
                            "refused": refused, "review": answer.get("review"), "problems": answer.get("problems"),
                            "reason": reason, "notes": str(answer.get("notes", ""))[:1000],
                            "prompt_chars": len(prompt)}, ensure_ascii=False) + "\n")
    report(now, used, changed, pb_changed, refused, str(answer.get("report_ko", ""))[:REPORT_CHARS], liked,
           answer.get("why_ko"))
    log(f"{used}: {'changed ' + json.dumps(changed, ensure_ascii=False) if changed else 'no change'}"
        f"{' | playbook ' + ', '.join(pb_changed) if pb_changed else ''}"
        f"{' | refused ' + '; '.join(refused) if refused else ''} | {reason}")


def report(now, used, changed, pb_changed, refused, text, liked=(), why=None):
    lines = [f"## {time.strftime('%m-%d %H:%M', time.localtime(now))} ({used or '판단 없음'})", "",
             text.strip() or "(보고 없음)", ""] + change_lines(changed, pb_changed, why)
    if refused:
        lines += ["- 적용 거부: " + "; ".join(refused)]
    if liked:
        lines += [f"- 좋아요 #{i}: {why}" for i, why in liked]
    if not (changed or pb_changed):
        lines += ["- 변경 없음"]
    with open(REPORTS, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")
    notify.send("[buyer manager] " + "\n".join(lines).removeprefix("## "))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    md = Path(a.dir) / ".market"
    global PAYMENTS
    PAYMENTS = md / "payments.jsonl"
    while True:
        now, inv = time.time(), {}
        try:
            inv = json.loads((md / "inventory.json").read_text(encoding="utf-8"))
            if inv.get("round", 0) >= 3:
                decide(md, now)
            else:
                log(f"round {inv.get('round')}: waiting for Round 3")
        except Exception as e:
            log(f"skipped: {type(e).__name__}: {str(e)[:200]}")
        if a.once:
            return
        time.sleep(max(60, 3600 - time.time() % 3600 + OFFSET) if inv.get("round", 0) >= 3 else 120)


if __name__ == "__main__":
    main()
