"""Week 08 manager: the slow loop. Every hour opus reviews the last hour of the shop and sets what comes next.

    python manager.py --dir .              # runs forever, one decision an hour
    python manager.py --dir . --once       # one decision now (testing)

A parameter-level meta-harness: the code that decides (restock.py, seller.py) runs on data/policy.json, and this
loop rewrites that file from the record of how the code did. It never touches the posteriors (model.py computes
them from data); it moves what data alone cannot settle: half-life and prior weight when demand shifts, money
rules, margin, concessions, C discount, warranty by grade, the liquidation start. policy.clip keeps every value in
its range and within a quarter of the range per decision, so one bad answer cannot wreck the shop.

The hourly review (added 11-02): besides the numbers, opus gets
  - its own last six decisions, each with the hour before and the hour after it (deals, margin, misses, template
    replies), and whether the shop's code is running on them (negotiations and turns carry the policy and playbook
    versions they used);
  - health: when each process last wrote (kit, collector, seller, restock, forum), template and retry rates, and
    the code's alerts;
  - excerpts of the last hour: negotiations in full (template replies, the ones that went nowhere, the longest) and
    the requests for things not in stock;
  - its notes from the last hour;
  - its memory (data/manager_memory.md): what it has learned so far, rewritten by itself every hour, at most
    MEMORY_CHARS, each lesson with the hours it rests on;
  - the archive index (data/manager_archive.jsonl): one line per past hour (deals, margin, misses, balance, alerts,
    the problems it found, what it changed); every hour of the last two days, older hours only when something
    happened.
Before deciding it may look hours up: {"lookup": [{"hour": "MM-DD HH:00", "what": [...]}]} gets the raw record of
those hours from the collector DB (settles, breaks, forum posts, balance, board) and the shop's logs (negotiations
verbatim, its own decision then), then it is asked again; at most LOOKUP_ROUNDS rounds of LOOKUP_HOURS hours.
It answers with number changes, playbook edits (negotiation and forum guidance as text, categories restock skips;
policy.clip_playbook refuses digits, unknown categories, too long or too many), notes for the next hour, the new memory, and a
report in Korean for the owner, who reads data/reports.md (and the same report on Telegram, notify.py) after the
fact and corrects the direction when needed.

Model: opus at medium effort; when it fails, sonnet at medium. Each decision is a
line in data/manager.jsonl (summary size, the answer, what changed after clipping, the reason, the notes).

Forum: shop_post builds the shop's status post from the policy and the ledger (no claims the shop does not keep);
forum.py sends it, an hour apart and after each policy change, between its replies to posts about the shop (the
market takes one shop post per 5 minutes). Complaints forum.py answered appear in the summary below, so a policy
change can answer them; forum.py then follows up in those threads.
"""
import argparse
import json
import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

import harness
import model
import notify
import policy

os.environ["TZ"] = "Asia/Seoul"             # the lab server runs on UTC; reports, archive hours and lookups read KST
time.tzset()

EVERY = 3600
MODELS = (("opus", "medium"), ("sonnet", "medium"))
LOG = policy.DATA / "manager.jsonl"
REPORTS = policy.DATA / "reports.md"     # the owner's after-the-fact report, newest at the bottom
TURNS = policy.DATA / "turns.jsonl"
STALE = 15 * 60                          # a process silent this long gets an alert
EXCERPTS, MISSED_SHOWN = 6, 12
MEMORY = policy.DATA / "manager_memory.md"
ARCHIVE = policy.DATA / "manager_archive.jsonl"
MEMORY_CHARS = 1500
ARCHIVE_FULL_H = 48                      # every hour this recent is in the index; older ones only when flagged
LOOKUP_ROUNDS, LOOKUP_HOURS = 2, 3
WHAT = ("negotiations", "settles", "broken", "forum", "balance", "board", "decision")
UPVOTES_MAX = 5
REPORT_CHARS = 700                       # the report arrives on Telegram every hour: short
REPORT_SPEC = ("<the owner's report in Korean, at most four short lines, only what matters. '상황:' what happened, "
               "in one line; '그대로 둔 이유:' only when you changed nothing, why that was the right call, in one line; "
               "'결정 필요:' what only the owner can decide (a code bug, a process down, a range that holds you back) or "
               "'없음'. The reason for each change goes in why_ko, not here. Leave out numbers that changed nothing.>")
WHY_SPEC = ('"why_ko": {{"<each number you change, or negotiation / forum{skip}>": "<in Korean, one or two sentences: '
            'what in the record made you change it (the evidence, with its numbers) and what you expect it to do>", ...}},')
UPVOTE_RULE = """Upvote: "upvote" lists the forum posts by others (from the record, by id, each with why) whose
information you actually used in this decision, e.g. a report of a shop's prices or a complaint that made you change something.
Not for agreement, politeness or posts you merely read; [] when none. Each post gets our vote once."""


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


def hour_metrics(since, until):
    """What one window of the shop looked like, from the seller's and restock's own logs."""
    negs = [r for r in jsonl(model.NEGOTIATIONS, since) if r["ts"] < until]
    deals = [r for r in negs if r["outcome"] == "deal"]
    turns = [r for r in jsonl(TURNS, since) if r["ts"] < until]
    restocks = [r for r in jsonl(policy.DATA / "restock.jsonl", since) if r["ts"] < until]
    return {"opened": sum(r["outcome"] == "open" for r in negs), "deals": len(deals),
            "margin_over_cost": round(sum(d["price"] / d["cost"] - 1 for d in deals) / len(deals), 3) if deals else None,
            "price_over_list": round(sum(d["price"] / d["list_price"] for d in deals) / len(deals), 3) if deals else None,
            "missed_requests": sum(1 for r in jsonl(policy.DATA / "missed.jsonl", since) if r["ts"] < until),
            "replies": len(turns), "template_replies": sum(1 for t in turns if t.get("template")),
            "balance_at_end": restocks[-1]["balance"] if restocks else None}


def recent_decisions(now, n=6):
    """The manager's last n decisions, each with the hour before and the hour after it."""
    out = []
    for r in jsonl(LOG, 0)[-n:]:
        t = r["ts"]
        out.append({"when": time.strftime("%m-%d %H:%M", time.localtime(t)), "changed": r.get("changed"),
                    "playbook_changed": r.get("playbook_changed"), "reason": r.get("reason"),
                    "hour_before": hour_metrics(t - 3600, t), "after": hour_metrics(t, min(t + 3600, now)),
                    "after_hours": round(min(3600, now - t) / 3600, 2)})
    return out


def health(md, now, p, b):
    """When each process last wrote, the reply quality of the last hour, whether the code runs on the current
    policy and playbook, and the alerts the code raises by itself."""
    files = {"kit (inventory.json)": md / "inventory.json", "collector (market.db)": model.DB,
             "seller (turns.jsonl)": TURNS, "restock (restock.jsonl)": policy.DATA / "restock.jsonl",
             "forum (forum.jsonl)": policy.DATA / "forum.jsonl"}
    def mtime(f):                         # sqlite in WAL mode writes to -wal until a checkpoint
        ts = [g.stat().st_mtime for g in (f, f.with_name(f.name + "-wal")) if g.exists()]
        return max(ts) if ts else None
    seen = {k: (round((now - mtime(f)) / 60, 1) if mtime(f) else None) for k, f in files.items()}
    turns = jsonl(TURNS, now - 3600)
    opens = [r for r in jsonl(model.NEGOTIATIONS, now - 3600) if r["outcome"] == "open"]
    alerts = [f"{k} has not written for {m} minutes" for k, m in seen.items()
              if k.split()[0] in ("kit", "collector", "restock") and (m is None or m * 60 > STALE)]
    tmpl = sum(1 for t in turns if t.get("template"))
    if turns and tmpl / len(turns) > 0.3:
        alerts.append(f"{tmpl} of {len(turns)} replies in the last hour were templates (haiku failing)")
    stale_pol = sum(1 for r in opens if r.get("policy") not in (None, p.get("version")))
    stale_pb = sum(1 for r in opens if r.get("playbook") not in (None, b.get("version")))
    return {"minutes_since_last_write": seen,
            "replies_last_hour": len(turns), "template_replies": tmpl,
            "replies_needing_a_retry": sum(1 for t in turns if (t.get("tries") or 0) > 1),
            "reasons_replies_failed": dict(Counter(str(t["failed"])[:80] for t in turns if t.get("failed"))),
            "negotiations_opened_since_the_current_policy": sum(1 for r in opens if r.get("policy") == p.get("version")),
            "negotiations_opened_on_an_older_policy": stale_pol,
            "negotiations_opened_on_an_older_playbook": stale_pb,
            "alerts": alerts}


def excerpts(now):
    """Whole negotiations of the last hour: template replies first, then the ones that went nowhere, then the
    longest; and the requests for things we did not have."""
    by_task = {}
    for t in jsonl(TURNS, now - 3600):
        by_task.setdefault(t["task"], []).append(t)
    closed = {r["task"] for r in jsonl(model.NEGOTIATIONS, now - 3600) if r["outcome"] == "deal"}

    def rank(item):
        task, ts = item
        return (-any(t.get("template") for t in ts), task in closed, -len(ts))
    talks = []
    for task, ts in sorted(by_task.items(), key=rank)[:EXCERPTS]:
        talks.append({"task": task[:8], "deal": task in closed,
                      "turns": [f"buyer: {t['buyer'][:300]} | shop ({t.get('unit')}, {t['price']}"
                                f"{', template' if t.get('template') else ''}): {t['shop'][:300]}" for t in ts]})
    missed = Counter(r.get("text", "")[:160] for r in jsonl(policy.DATA / "missed.jsonl", now - 3600))
    return talks, [{"request": k, "times": n} for k, n in missed.most_common(MISSED_SHOWN)]


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
        "our_settles_this_round_vs_20_needed_to_rank": [
            sum(1 for r in db.execute("SELECT data FROM ledger WHERE round >= 3 AND kind='settle'")
                if json.loads(r[0]).get("shop") in mine), 20],
        "our_recent_reviews": reviews,
        "open_complaints_about_us_on_the_forum": list((json.loads((policy.DATA / "forum_state.json").read_text())
                                                       .get("complaints") or {}).values())
                                                 if (policy.DATA / "forum_state.json").exists() else [],
        "forum_newest": [{"id": r[0], "author": r[2], "role": r[3], "text": (r[4] or "")[:300],
                          "likes_dislikes": votes.get(r[0])} for r in posts],
    }
    b = policy.load_playbook()
    talks, missed_texts = excerpts(now)
    s["last_hour"] = hour_metrics(hour, now)
    s["health"] = health(md, now, p, b)
    s["your_last_decisions_and_what_followed"] = recent_decisions(now)
    s["negotiations_last_hour"] = talks
    s["requests_not_in_stock_last_hour"] = missed_texts
    prior = jsonl(LOG, 0)
    s["your_notes_from_last_hour"] = prior[-1].get("notes") if prior else None
    return p, b, s, est.categories


PROMPT = """You are the manager of a shop in an agent marketplace (Round 3, 14 days). The shop's code sells units
it bought from the market, negotiates with buyer agents (haiku writes the replies; the code sets every price and
unit), answers forum posts about the shop (opus), and restocks every minute by a newsvendor rule on a gamma-Poisson
demand estimate. You do not trade. Once an hour you review the last hour and set what the code runs on next. The
owner does not watch; they read your report afterwards and correct you when needed.

Score (percentiles among shops): 0.5 x profit (balance - 5,000,000 - bankruptcy money; unsold stock counts as a
loss) + 0.3 x revenue + 0.2 x stars. Food rots (thrown away at cost); goods pay 1% of cost a day in storage and a
hidden share breaks within 6 h of the sale (a warranty refunds the full price from our balance). Buyers see only
shops that hold stock in the ordered category. Our claims are always true and certificates always shown.

Policy now, with the allowed range of each number (a change moves at most a quarter of the range):
{policy}

Playbook now (text and categories the code runs on; you may rewrite it):
  negotiation guidance (added to haiku's prompt when it picks a grade and writes a reply): {negotiation}
  forum guidance (added to opus's prompt when it answers a post about us): {forum}
  categories restock skips, with hours left: {skips}
Write guidance in Korean (the owner reads it; haiku and opus still answer in the buyer's or the post's language).
Guidance steers how the models speak and which of the code's options they pick (grade, tone, what to say first);
it cannot change a price, a floor or a warranty, and it must contain no digits and no words for costs, floors,
margins, percents or fractions (haiku must never learn internal numbers; such guidance is refused). At most {chars} characters each. Skip a category (at most {skip_hours} hours,
at most {skip_max} at once) only for a reason in the record, e.g. it loses money or draws complaints.

What the code does by design (guidance cannot change it; guidance against it only makes haiku's words disagree with
what the shop does): an offer above the asking price gets one request to confirm (such numbers are often for several
units), then closes at the ask; the shop concedes only when the buyer raises its offer (one step down a ladder
toward the floor), holds its price when the buyer does not, and after three numbers in a row that did not rise calls
the held price final and never moves it again; an offer at or above the current price closes; the shop never walks
away; claims are true and certificates shown. If
the code itself looks wrong (a bug, a process down), say so in "problems" and the report; the owner fixes code.

The record (code-computed; the last hour's negotiations are verbatim):
{summary}

Review, in this order:
1. Your last decisions: did the code run on them (negotiations on the current versions), and what followed?
   Undo or adjust what did not work.
2. Anything wrong: alerts, a process not writing, template replies, haiku saying something off in the excerpts,
   buyers asking for what we do not hold, complaints.
3. What to change: numbers, playbook, or nothing. Change only for a reason in the record; no change is a fine
   answer.
Answer with one JSON object only:
{{"review": "<what your last decisions did, or that there were none>",
 "problems": ["<each thing wrong you found>"],
 "changes": {{"<number name>": <value>, ...}},
 "playbook": {{"negotiation": "<the full new text, or null to keep>", "forum": "<the full new text, or null to keep>",
              "restock_skip": {{"<category>": <hours, 0 to stop skipping>}} or null}},
 "reason": "<in Korean, two sentences: what in the record, what you changed>",
 {why_spec}
 "notes": "<what to watch next hour, for yourself>",
 "upvote": [{{"id": <post id>, "why": "<in Korean, a few words: what in it you used>"}}, ...],
 "report_ko": "{report_spec}"}}"""


LOOKUP = """

Your memory (what you have learned so far, written by you):
{memory}

Archive index (one line per past hour; older hours only when something happened):
{index}

Before deciding you may look up past hours to see their raw record: answer {{"lookup": [{{"hour": "MM-DD HH:00",
"what": [some of {what}]}}]}} instead (at most {hours} hours; {rounds_left} lookup rounds left). Look up only when an
hour in the index or your memory bears on what you are deciding now.
{looked}
With the decision, also give "memory": the full new text of your memory (at most {chars} characters): lessons that
should outlive this hour, each with the hours it rests on, e.g. "C warranty off -> more break complaints (11-05 14:00,
11-06 09:00)". Drop what no longer holds. Keep it the same when nothing new was learned."""


def hour_of(text, now):
    """'MM-DD HH:00' (this year, local time) -> the start of that hour; None when it does not parse."""
    try:
        t = time.strptime(f"{time.localtime(now).tm_year}-{text.strip()[:11]}", "%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return None
    return time.mktime(t) // 3600 * 3600


def archive_index(now):
    lines = []
    for r in jsonl(ARCHIVE, 0):
        if now - r["ts"] > ARCHIVE_FULL_H * 3600 and not (r.get("problems") or r.get("changed") or r.get("alerts")):
            continue
        m = r.get("metrics") or {}
        lines.append(f"{r['hour']}: deals {m.get('deals')}/{m.get('opened')} opened, margin {m.get('margin_over_cost')}, "
                     f"missed {m.get('missed_requests')}, templates {m.get('template_replies')}, balance "
                     f"{m.get('balance_at_end')}"
                     + (f" | alerts: {'; '.join(r['alerts'])[:200]}" if r.get("alerts") else "")
                     + (f" | problems: {' / '.join(r['problems'])[:300]}" if r.get("problems") else "")
                     + (f" | changed: {', '.join(r['changed'])}" if r.get("changed") else ""))
    return "\n".join(lines) or "(empty: this is the first hour)"


def archive(now, s, answer, changed, pb_changed):
    rec = {"ts": now, "hour": time.strftime("%m-%d %H:00", time.localtime(now - 3600)), "metrics": s["last_hour"],
           "alerts": s["health"]["alerts"],
           "problems": [str(x)[:200] for x in (answer or {}).get("problems") or []][:6],
           "changed": list(changed) + [f"playbook {k}" for k in pb_changed]}
    with open(ARCHIVE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def lookup(reqs, now, db):
    """The raw record of the hours asked for (the hour starting at the time given), cut to a readable size."""
    mine = model.our_urls(db)
    out = []
    for q in (reqs or [])[:LOOKUP_HOURS]:
        t0 = hour_of(str(q.get("hour", "")), now) if isinstance(q, dict) else None
        if t0 is None or t0 > now:
            out.append({"hour": q.get("hour") if isinstance(q, dict) else q, "error": "not an hour in MM-DD HH:00"})
            continue
        what = q.get("what") if isinstance(q.get("what"), list) else list(WHAT)
        t1, what = t0 + 3600, [w for w in what if w in WHAT]
        got = {"hour": time.strftime("%m-%d %H:00", time.localtime(t0))}

        def ledger(kind):
            return [json.loads(r[0]) for r in db.execute(
                "SELECT data FROM ledger WHERE kind=? AND ts>=? AND ts<? ORDER BY seq", (kind, t0, t1))]
        if "negotiations" in what:
            by_task = {}
            for t in jsonl(TURNS, t0):
                if t["ts"] < t1:
                    by_task.setdefault(t["task"], []).append(t)
            got["negotiations"] = [[f"buyer: {t['buyer'][:200]} | shop ({t.get('unit')}, {t['price']}"
                                    f"{', template' if t.get('template') else ''}): {t['shop'][:200]}" for t in ts]
                                   for ts in list(by_task.values())[:8]]
            got["negotiation_outcomes"] = dict(Counter(r["outcome"] for r in jsonl(model.NEGOTIATIONS, t0)
                                                       if r["ts"] < t1))
        if "settles" in what:
            settles = ledger("settle")
            ours = [d for d in settles if d.get("shop") in mine]
            got["our_settles"] = [{k: d.get(k) for k in ("item", "price", "stars", "review", "claim", "claim_honest",
                                                          "warranty_hours") if k in d} for d in ours[:20]]
            got["market_settles"] = len(settles)
            got["market_settles_by_shop"] = dict(Counter(str(d.get("shop"))[-30:] for d in settles).most_common(8))
        if "broken" in what:
            got["broken"] = [{k: v for k, v in d.items() if len(str(v)) < 120} for d in ledger("broken")[:20]]
        if "forum" in what:
            got["forum"] = [{"id": r[0], "author": r[1], "role": r[2], "text": (r[3] or "")[:300]} for r in db.execute(
                "SELECT id, author, role, text FROM forum_posts WHERE ts>=? AND ts<? ORDER BY id LIMIT 20", (t0, t1))]
        if "balance" in what:
            b = db.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM inventory WHERE ts>=? AND ts<?", (t0, t1)).fetchone()
            if b[2]:
                first = db.execute("SELECT balance, n_units FROM inventory WHERE ts=?", (b[0],)).fetchone()
                last = db.execute("SELECT balance, n_units FROM inventory WHERE ts=?", (b[1],)).fetchone()
                got["balance"] = {"start": first[0], "end": last[0], "units_start": first[1], "units_end": last[1]}
        if "board" in what:
            r = db.execute("SELECT raw FROM board WHERE ts>=? AND ts<? ORDER BY ts DESC LIMIT 1", (t0, t1)).fetchone()
            got["board"] = (r[0] or "")[:1500] if r else None
        if "decision" in what:
            got["your_decision"] = [{k: r.get(k) for k in ("review", "problems", "changed", "playbook_changed",
                                                            "reason", "notes")}
                                    for r in jsonl(LOG, t0) if r["ts"] < t1 + 600]
        out.append(got)
    text = json.dumps(out, ensure_ascii=False)
    return text[:12000] + (" ...(cut)" if len(text) > 12000 else "")


def ask_model(prompt):
    """opus, else sonnet; the first JSON object of the answer and which model gave it."""
    for mdl, effort in MODELS:
        t0 = time.time()
        try:
            reply = harness.call(prompt, timeout=600, model=mdl, effort=effort)
        except Exception as e:
            log(f"{mdl} failed after {time.time() - t0:.0f}s: {type(e).__name__} {str(e)[:150]}")
            continue
        m = re.search(r"\{.*\}", reply, re.S)
        try:
            return json.loads(m.group()), f"{mdl} {effort} {time.time() - t0:.0f}s"
        except (AttributeError, ValueError):
            log(f"{mdl} answer had no JSON: {reply[:200]!r}")
    return None, None


def describe(p):
    lines = [f"  {k} = {p[k]}  (range {lo} .. {hi})" for k, (_, lo, hi) in policy.NUMBERS.items()]
    lines += [f"  {k} = {p[k]}  (true/false)" for k in policy.FLAGS]
    return "\n".join(lines)


def decide(md, now):
    p, b, s, categories = summary(md, now)
    skips = {c: round((t - now) / 3600, 1) for c, t in (b.get("restock_skip") or {}).items() if t > now}
    prompt = PROMPT.format(policy=describe(p), negotiation=json.dumps(b["negotiation"] or "(none)"),
                           forum=json.dumps(b["forum"] or "(none)"), skips=json.dumps(skips),
                           chars=policy.GUIDE_CHARS, skip_hours=policy.SKIP_HOURS, skip_max=policy.SKIP_MAX,
                           report_spec=REPORT_SPEC, why_spec=WHY_SPEC.format(skip=" / restock_skip"), summary=json.dumps(s, ensure_ascii=False, indent=1)) + "\n\n" + UPVOTE_RULE
    memory = MEMORY.read_text(encoding="utf-8") if MEMORY.exists() else ""
    index, looked, used, answer = archive_index(now), [], [], None
    db = model.connect()
    for rnd in range(LOOKUP_ROUNDS + 1):
        left = LOOKUP_ROUNDS - rnd
        extra = LOOKUP.format(memory=memory or "(empty)", index=index, what=list(WHAT), hours=LOOKUP_HOURS,
                              rounds_left=left, chars=MEMORY_CHARS,
                              looked=("\nWhat you looked up:\n" + "\n".join(looked) + "\n") if looked else "")
        if not left:
            extra += "\nNo lookups left: answer with the decision now."
        answer, who = ask_model(prompt + extra)
        if answer is None:
            break
        used.append(who)
        if not (left and answer.get("lookup") and "review" not in answer):
            break
        reqs = answer["lookup"]
        got = lookup(reqs if isinstance(reqs, list) else [reqs], now, db)
        looked.append(got)
        log(f"{who}: looked up {json.dumps(answer['lookup'], ensure_ascii=False)[:200]} ({len(got)} chars)")
    used = " + ".join(used)
    if answer is None or "review" not in answer:
        log("no decision this hour; the policy stays")
        report(now, used or None, s["health"]["alerts"], {}, {}, [], "모델 호출이 실패해 이번 시간은 판단하지 "
                                                                      "못했습니다. 정책과 playbook은 그대로입니다.")
        archive(now, s, None, {}, {})
        return p, False
    for k in ("changes", "playbook"):          # a model answer of the wrong shape counts as no change
        if not isinstance(answer.get(k), dict):
            answer[k] = {}
    new = policy.clip(answer["changes"], p)
    changed = {k: (p[k], new[k]) for k in list(policy.NUMBERS) + list(policy.FLAGS) if new[k] != p[k]}
    reason = str(answer.get("reason", ""))[:600]
    if changed:
        new = policy.save(new, reason)
    nb, refused = policy.clip_playbook(answer["playbook"], b, set(categories), now)
    pb_changed = {k: (b.get(k), nb.get(k)) for k in ("negotiation", "forum", "restock_skip") if nb.get(k) != b.get(k)}
    if pb_changed:
        policy.save_playbook(nb, reason)
    mem = answer.get("memory")
    if isinstance(mem, str) and mem.strip() and mem.strip() != memory.strip():
        if len(mem) > MEMORY_CHARS:
            refused.append(f"memory: longer than {MEMORY_CHARS} characters, kept the old one")
        else:
            MEMORY.write_text(mem.strip() + "\n", encoding="utf-8")
    archive(now, s, answer, changed, pb_changed)
    liked = upvote(md, answer.get("upvote"), db, "shop")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "model": used, "asked": answer.get("changes"), "changed": changed,
                            "playbook_asked": answer.get("playbook"), "playbook_changed": pb_changed, "why": answer.get("why_ko"),
                            "refused": refused, "review": answer.get("review"), "problems": answer.get("problems"),
                            "reason": reason, "notes": str(answer.get("notes", ""))[:1000],
                            "lookups": len(looked), "memory": answer.get("memory"), "upvoted": liked,
                            "prompt_chars": len(prompt)}, ensure_ascii=False) + "\n")
    report(now, used, s["health"]["alerts"], changed, pb_changed, refused, str(answer.get("report_ko", ""))[:REPORT_CHARS], liked,
           answer.get("why_ko"))
    log(f"{used}: {'changed ' + json.dumps(changed, ensure_ascii=False) if changed else 'no change'}"
        f"{' | playbook ' + ', '.join(pb_changed) if pb_changed else ''}"
        f"{' | refused ' + '; '.join(refused) if refused else ''} | {reason}")
    return new, bool(changed)


def upvote(md, ids, db, role):
    """Like the posts the manager says it used: known posts by others, not liked by us before, at most UPVOTES_MAX.
    `ids` holds {"id", "why"} objects (bare ids are taken too). Returns [(id, why)] sent (the kit posts the votes from .market/outbox.jsonl; they count once we have 5 deals)."""
    path = policy.DATA / f"votes_{role}.json"        # one file per role: the two managers never write the same one
    try:
        done = set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        done = set()
    sent = []
    for x in (ids if isinstance(ids, list) else [])[:UPVOTES_MAX]:
        why = str(x.get("why", ""))[:80] if isinstance(x, dict) else ""
        try:
            i = int(x.get("id") if isinstance(x, dict) else x)
        except (TypeError, ValueError):
            continue
        row = db.execute("SELECT author FROM forum_posts WHERE id=?", (i,)).fetchone()
        if not row or i in done or "inextro" in str(row[0]).lower():
            continue
        with open(md / "outbox.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"vote": i, "value": 1, "as": role}) + "\n")
        done.add(i)
        sent.append((i, why))
    if sent:
        path.write_text(json.dumps(sorted(done)[-2000:]), encoding="utf-8")
    return sent


def change_lines(changed, pb_changed, why):
    """What the code applied, each change with the model's reason for it (why_ko): the reason is what the owner needs."""
    why = why if isinstance(why, dict) else {}
    because = lambda k: f"\n  이유: {str(why.get(k) or why.get(f'playbook {k}') or '(설명 없음)')[:300]}"
    lines = [f"- 정책 {k}: {old} → {new}{because(k)}" for k, (old, new) in changed.items()]
    for k, (old, new) in pb_changed.items():
        if k == "restock_skip":              # timestamps mean little to a reader; the categories are the change
            new = {c: time.strftime("%m-%d %H:%M까지", time.localtime(t)) for c, t in new.items()}
            lines += [f"- 재입고 건너뛰기: {json.dumps(new, ensure_ascii=False)}{because(k)}"]
        else:                                # the old text is in the previous report; the new one is the change
            lines += [f"- playbook {k} 지침 수정{because(k)}\n  새 지침: {new or '(비움)'}"]
    return lines


def report(now, used, alerts, changed, pb_changed, refused, text, liked=(), why=None):
    """One entry of the owner's report: the model's Korean summary, then what the code actually applied and why."""
    lines = [f"## {time.strftime('%m-%d %H:%M', time.localtime(now))} ({used or '판단 없음'})", "", text.strip() or "(보고 없음)", ""]
    if alerts:
        lines += ["- 코드 경고: " + "; ".join(alerts)]
    lines += change_lines(changed, pb_changed, why)
    if refused:
        lines += ["- 적용 거부: " + "; ".join(refused)]
    if liked:
        lines += [f"- 좋아요 #{i}: {why}" for i, why in liked]
    if not (changed or pb_changed):
        lines += ["- 변경 없음"]
    with open(REPORTS, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")
    notify.send("[가게 manager] " + "\n".join(lines).removeprefix("## "))


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
                decide(md, now)
            else:
                log(f"round {inv.get('round')}: waiting for Round 3")
        except Exception as e:                   # one bad hour must not stop the loop for the rest of the round
            log(f"skipped: {type(e).__name__}: {str(e)[:200]}")
        if a.once:
            return
        # at a minute past each hour, so the archive's hour and a lookup's hour cover the same window
        time.sleep(max(60, 3600 - time.time() % 3600 + 60) if inv.get("round", 0) >= 3 else 120)


if __name__ == "__main__":
    main()
