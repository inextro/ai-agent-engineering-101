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
import re
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


def post_kind(text):
    """A buyer post's kind, its leading "[...]" tag ("[구매 후기]"), else "other"."""
    m = re.match(r"\s*\[([^\]]{1,20})\]", text or "")
    return m.group(1) if m else "other"


def read_avoid():
    """market_buyer's avoid list (data/buyer_avoid.json): shop -> refusals in its window."""
    try:
        return json.loads((policy.DATA / "buyer_avoid.json").read_text(encoding="utf-8")).get("refusals")
    except (OSError, ValueError):
        return None


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


def wilson(likes, dislikes, z=1.96):
    """Lower bound of the share of likes: a post's worth in the forum score (average over voted posts)."""
    n = likes + dislikes
    if n == 0:
        return None
    p = likes / n
    return (p + z * z / (2 * n) - z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5) / (1 + z * z / n)


def our_forum(board, us):
    """Our buyer's forum score and its percentile among ranked buyers in one /api/board snapshot."""
    ranked = [b for b in board.get("buyers", []) if b.get("all") is not None]
    me = next((b for b in ranked if ((b.get("owner") or {}).get("login") or "").lower() == us.lower()), None)
    if me is None or len(ranked) < 2:
        return None, None
    f = me.get("forum") or 0
    return f, sum(1 for b in ranked if (b.get("forum") or 0) < f) / (len(ranked) - 1)


AVG_DROP = 0.03   # how far under the forum score the last 2 h of posts must average before "평균 하락" fires


def forum_alerts(db, now, us):
    """Code checks that the forum strategy's premises broke (owner 11-04: tell me when the forum policy needs a
    change). Each alert is one Korean line with its numbers; the manager explains them, the owner decides."""
    out = []
    votes = {r[0]: (r[1] or 0, r[2] or 0) for r in db.execute(
        "SELECT post, likes, dislikes FROM forum_votes v WHERE polled_at = "
        "(SELECT MAX(polled_at) FROM forum_votes WHERE post = v.post)")}
    mine = db.execute("SELECT id, ts FROM forum_posts WHERE role='buyer' AND lower(author) = ? AND ts > ? ORDER BY ts",
                      (us.lower(), now - 6 * 3600)).fetchall()
    snaps = [json.loads(r[0]) for r in db.execute("SELECT raw FROM board WHERE ts > ? ORDER BY ts", (now - 6 * 3600,))]
    score, pct = our_forum(snaps[-1], us) if snaps else (None, None)
    # 1. a dislike: our three shapes have had none since 11-04 02:05; votes land within 5 min, so one hour's posts
    hit = [(i, votes[i][1]) for i, t in mine if t > now - 3900 and votes.get(i, (0, 0))[1] > 0]
    if hit:
        out.append("싫어요: 우리 글 " + ", ".join(f"#{i}({d}개)" for i, d in hit) + "이 싫어요를 받았습니다")
    # 2. new posts pull the average down: posts older than 10 min of the last 2 h, against the score now, by more
    # than AVG_DROP. Until 11-04 15 h it fired every hour: most posts land at 6 likes 0 dislikes (Wilson 0.61)
    # against a 0.635 score, which moved the score 0.637 -> 0.635 in 8 h with the percentile at 0.80-0.84; the
    # owner found the hourly alert dulled it. At 0.03 twelve 6/0 posts (0.610) stay quiet, two 5/0 of twelve
    # (0.602) or one 7/1 of twelve (0.603) still trip it.
    aged = [i for i, t in mine if now - 7200 < t < now - 600]
    worth = [w for w in (wilson(*votes[i]) for i in aged if i in votes) if w is not None]
    if score is not None and len(worth) >= 6 and sum(worth) / len(worth) < score - AVG_DROP:
        out.append(f"평균 하락: 최근 2시간 글 {len(worth)}편의 Wilson 하한 평균 {sum(worth) / len(worth):.3f}이 "
                   f"지금 forum 점수 {score:.3f}보다 낮아, 새 글이 평균을 깎고 있습니다")
    # 3. votes stopped: the last six posts older than 10 min have no likes at all
    last6 = [i for i, t in mine if t < now - 600][-6:]
    if len(last6) == 6 and all(votes.get(i, (0, 0))[0] == 0 for i in last6):
        out.append(f"투표 끊김: 최근 글 6편(#{last6[0]}~#{last6[-1]})이 10분이 지나도 좋아요 0입니다")
    # 4. percentile fell: more than 0.15 under its best in the last 6 h of board snapshots
    seen = [p for p in (our_forum(b, us)[1] for b in snaps) if p is not None]
    if pct is not None and seen and max(seen) - pct > 0.15:
        out.append(f"순위 하락: forum 백분위 {pct:.2f}, 최근 6시간 최고 {max(seen):.2f}에서 {max(seen) - pct:.2f} 떨어졌습니다")
    # 6. dislike bombing (owner 11-05): from 11-05 01 h one voter disliked nearly every post in the market (buyer
    # posts with a dislike: 23 h 0/290, 03 h 269/277), every kind of ours got one, and buyer_forum's 12 h rule left
    # nothing to post after 03:43. The owner holds our posts while it lasts and wants to hear when it fades.
    storm = dislike_share(db, now, us, votes)
    last = max((r.get("ts", 0) for r in jsonl(POSTS, now - 6 * 3600)), default=0)
    # buyer_forum posts again once one kind's newest disliked post is FORUM_VOTES_H old: the earliest kind decides
    hit = {}
    for i, t, x in db.execute("SELECT id, ts, text FROM forum_posts WHERE role='buyer' AND lower(author) = ? AND ts > ?",
                              (us.lower(), now - (FORUM_VOTES_H + 1) * 3600)):
        if votes.get(i, (0, 0))[1] > 0 and post_kind(x) in MAIN_TAGS:
            hit[post_kind(x)] = max(hit.get(post_kind(x), 0), t)
    resume = time.strftime("%H:%M", time.localtime(min(hit.values()) + FORUM_VOTES_H * 3600)) if hit else None
    if FORUM_PAUSED:                    # no automatic resume: the owner restarts posts after the bombing ends
        resume = None
    on = storm and storm[0]["share"] is not None and storm[0]["share"] >= STORM_ON
    was = any(s["share"] is not None and s["share"] >= STORM_ON for s in storm[1:])
    if on:
        hours = next((k for k, s in enumerate(storm) if s["share"] is None or s["share"] < STORM_ON), len(storm))
        out.append(f"싫어요 테러 진행: 최근 1시간 다른 buyer 글 {storm[0]['posts']}편 중 {storm[0]['disliked']}편"
                   f"({storm[0]['share']:.0%})이 싫어요를 받았습니다, {hours}시간째. 우리 buyer 글은 "
                   + ((f"{(now - last) / 60:.0f}분째 대기(의도)" if last else "6시간 넘게 대기(의도)")
                      if now - last > 1800 else "나가고 있습니다")   # last = 0 when no post in the window (11-05: "29897525분")
                   + (", 소유자 지시로 무기한 정지(테러가 끝난 게 확인될 때까지)" if FORUM_PAUSED else "")
                   + (f", 지금 규칙의 자동 재개는 {resume} 이후" if resume else ""))
    elif was and storm[0]["share"] is not None and storm[0]["share"] < STORM_OFF:
        out.append(f"싫어요 테러 잦아듦: 최근 1시간 다른 buyer 글 {storm[0]['posts']}편 중 {storm[0]['disliked']}편"
                   f"({storm[0]['share']:.0%})만 싫어요. 우리 buyer 글 재개를 owner가 판단할 때입니다"
                   + (f" (지금 규칙의 자동 재개는 {resume} 이후)" if resume else ""))
    # 5. posting stopped: no buyer post for 30 min (the three shapes ran out of material, or buyer_forum is down);
    # during the bombing the pause is the owner's, line 6 says so
    if now - last > 1800 and not on and FORUM_PAUSED:
        out.append("글 정지(소유자 지시, 의도): 마지막 buyer 글이 " + (f"{(now - last) / 60:.0f}분 전" if last else "6시간 넘게 전")
                   + "입니다. 재개는 소유자가 정합니다")
    elif now - last > 1800 and not on:
        out.append("글 멈춤: 마지막 buyer 글이 " + (f"{(now - last) / 60:.0f}분 전" if last else "6시간 넘게 전") + "입니다")
    return {"alerts": out, "forum_score": score, "forum_percentile": None if pct is None else round(pct, 2),
            "market_dislike_share_by_hour_newest_first": storm}


STORM_ON, STORM_OFF = 0.30, 0.10   # share of other buyers' posts with a dislike: bombing on / faded (normal: under 2%)
FORUM_VOTES_H = 12                 # buyer_forum.VOTES_H: a kind with a disliked post waits this long (not imported: its loop)
FORUM_PAUSED = True                # buyer_forum.POSTING_PAUSED (owner 11-05 08:10: no posts until the bombing is seen to end)
MAIN_TAGS = ("가격 동향", "상품 시세", "구매 후기")   # the tags of buyer_forum.MAIN, the only kinds it posts (ONLY_MAIN)


def dislike_share(db, now, us, votes, hours=12):
    """Per hour (newest first), other buyers' posts 10-70 min old in that hour and how many got a dislike. An hour
    with under 20 posts is None (too few to tell)."""
    rows = db.execute("SELECT id, ts FROM forum_posts WHERE role='buyer' AND lower(author) != ? AND ts > ?",
                      (us.lower(), now - (hours + 1) * 3600)).fetchall()
    out = []
    for h in range(hours):
        hi, lo = now - 600 - h * 3600, now - 600 - (h + 1) * 3600
        ids = [i for i, t in rows if lo < t <= hi]
        d = sum(1 for i in ids if votes.get(i, (0, 0))[1] > 0)
        out.append({"hours_ago": h, "posts": len(ids), "disliked": d,
                    "share": round(d / len(ids), 2) if len(ids) >= 20 else None})
    return out


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
    try:
        checks = forum_alerts(db, now, us)
    except Exception as e:
        checks = {"alerts": [f"forum 점검 실패: {type(e).__name__}: {str(e)[:120]}"]}
    last =[r for r in jsonl(ORDERS, now - 3600) if r.get("kind")][-ORDERS_SHOWN:]
    s = {
        "time": time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(now)),
        "hours_left_in_round": round(policy.hours_left(now), 1),
        "our_buyer_settles_this_round_vs_20_needed_to_rank": [len(ours), 20],
        "market_settles_this_round": len(settles),
        "last_hour": window(now - 3600, now),
        "last_6_hours": window(now - 6 * 3600, now),
        "shops_skipped_now_for_refusing_us_at_the_first_question": read_avoid(),
        "orders_last_hour": [{k: r.get(k) for k in ("category", "kind", "use_within_hours", "budget", "shop", "list", "cap", "expect",
                                                     "quotes", "asks", "offers", "end", "price", "secs", "tried")}
                             for r in last],
        "our_reviews_6h": {"stars": dict(Counter(r.get("stars") for r in bought)),
                           "false_grade_claims": sum(1 for r in bought if r.get("claim_honest") is False)},
        # counts apart from the examples: 6 examples under a "24h" key read as "6 posts in 24 h" (11-04 00:39)
        "our_buyer_posts_count": {"last_hour": sum(1 for r in posts if r.get("ts", 0) >= now - 3600),
                                  "last_24h": len(posts),
                                  "by_kind_24h": dict(Counter(post_kind(r.get("text", "")) for r in posts)),
                                  "interval": "fixed by code at about 5 minutes to the end of the round"},
        "our_buyer_posts_newest_6_examples": [{"text": r.get("text", "")[:280], "how": r.get("how")}
                                              for r in posts[-6:]],
        "our_buyer_posts_on_the_forum_with_votes(likes, dislikes)": [
            {"id": r[0], "text": (r[1] or "")[:200], "votes": votes.get(r[0])} for r in mine],
        "forum_strategy_checks": checks,
        "forum_newest_by_others":[{"id": r[0], "author": r[1], "role": r[2], "text": (r[3] or "")[:300]}
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
   "forum_strategy_checks" holds code checks that the forum strategy's premises broke (a dislike, new posts under
   the average, votes stopped, the percentile fell, posting stopped, dislike bombing on the whole market). When
   "alerts" is not empty, put each in '결정 필요:' with its likely cause from the record and the owner's options. Do
   not rewrite the forum guidance for these yourself: the owner decides the forum strategy. Watch the dislike
   bombing every hour: "market_dislike_share_by_hour_newest_first" is the share of other buyers' posts that got a
   dislike, hour by hour (under 2% before 11-05 01 h). Say in one line whether it is rising, steady or fading, and
   whether our buyer posts are paused or going out.
3. What to change: numbers, playbook, or nothing. Change only for a reason in the record; no change is a fine answer.
   Mind the trade: lower offers raise surplus per deal but lose deals.
4. Findings ('발견'): read the orders and posts for what the code cannot count, and report only what is new: a
   shop acting in a new way (bait asks raised after the first quote, refusals, hidden certificates, receipts over
   the ask), a shop pricing a SKU far under the others, a category whose quotes moved, a rule or a market behaviour
   that changed. Our own shop sells too: a shop that undercuts on goods it sells is worth one point. Each finding
   names its evidence (order ids, shops, counts) and how to check it; none is a fine answer.
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
    alerts = s["forum_strategy_checks"].get("alerts") or []
    answer, used = ask_model(prompt)
    if answer is None or "review" not in answer:
        log("no decision this hour; the policy stays")
        report(now, used, {}, {}, [], "모델 호출이 실패해 이번 시간은 판단하지 못했습니다. buyer 정책과 playbook은 그대로입니다.",
               alerts=alerts)
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
           answer.get("why_ko"), alerts)
    log(f"{used}: {'changed ' + json.dumps(changed, ensure_ascii=False) if changed else 'no change'}"
        f"{' | playbook ' + ', '.join(pb_changed) if pb_changed else ''}"
        f"{' | refused ' + '; '.join(refused) if refused else ''} | {reason}")


def report(now, used, changed, pb_changed, refused, text, liked=(), why=None, alerts=()):
    lines = [f"## {time.strftime('%m-%d %H:%M', time.localtime(now))} ({used or '판단 없음'})", ""]
    if alerts:      # the code's checks, word for word, so they stand out of the hourly report whatever opus wrote
        lines += ["[forum 점검 필요]"] + [f"- {a}" for a in alerts] + [""]
    lines += [text.strip() or "(보고 없음)", ""] + change_lines(changed, pb_changed, why)
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
