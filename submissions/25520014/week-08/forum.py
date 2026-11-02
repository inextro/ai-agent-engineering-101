"""Week 08 forum: the shop answers every post about it and posts its status, through .market/outbox.jsonl.

    python forum.py --dir .            # runs forever, checks the forum every minute
    python forum.py --dir . --once     # one pass (testing)

A post is about us when it names the shop (its title or `inextro`), cites one of our settles in its refs, or
replies to one of our posts. Each gets one reply, praise or complaint, written by opus (medium; sonnet when it fails)
from the post, the ledger records it cites (grade, claim_honest, breaks, warranty), what the shop keeps and the
policy changes of the last day in a buyer's words. Internal numbers (margins, floors, costs, money rules, the
manager's reasons) never reach the model, and a reply with a number no source gave is retried once, then dropped. The reply promises only what the code does: certificates shown, claims true, the warranty as it is
now, and that the manager reads the complaint at its next hourly review. Complaints go into data/forum_state.json,
which manager.py puts in front of opus; when the policy changes afterwards, opus decides which complaints the change
answers and the shop follows up in those threads.

The market takes one shop post per 5 minutes (replies count), so this is the only writer of shop posts: replies
first, follow-ups next, the hourly status post (and one after each policy change) when nothing else is waiting.
"""
import argparse
import json
import os
import re
import time
from pathlib import Path

import harness
import manager
import model
import policy

STATE = policy.DATA / "forum_state.json"
LOG = policy.DATA / "forum.jsonl"
MODELS = (("opus", "medium"), ("sonnet", "medium"))
GAP = 310                 # seconds between our shop posts (the market's limit is 300)
STATUS_EVERY = 3600
FOLLOW_UP_DAYS = 2
SHOP = os.environ.get("SHOP_TITLE", "Inextro Market")   # as in seller.py
NAMES = ("inextro", SHOP.lower())


def log(msg):
    print(f"{time.strftime('%m-%d %H:%M:%S')} [forum] {msg}", flush=True)


def read(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def ours(post, mine):
    return post["id"] in mine or "inextro" in str(post.get("author", "")).lower()


def our_post_ids(md, st):
    """Ids of the posts we wrote: from the market's answers in outbox.sent.jsonl, plus the ones already known."""
    ids = set(st["mine"])
    try:
        for line in (md / "outbox.sent.jsonl").read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            ans = rec.get("answer") or rec.get("result") or {}
            if isinstance(ans, dict) and rec.get("as", "shop") == "shop" and ans.get("id") is not None:
                ids.add(ans["id"])
    except (OSError, ValueError):
        pass
    return ids


def our_settles(db, seqs):
    """The ledger entries a post cites that are our settles, with any break of that buyer's unit within 6 h."""
    mine = model.our_urls(db)
    out = {}
    for seq in seqs:
        row = db.execute("SELECT ts, kind, data FROM ledger WHERE seq=?", (seq,)).fetchone()
        if not row or row["kind"] != "settle":
            continue
        d = json.loads(row["data"])
        if d.get("shop") not in mine:
            continue
        broke = db.execute(
            "SELECT 1 FROM ledger WHERE kind='broken' AND ts BETWEEN ? AND ? AND json_extract(data,'$.buyer')=? "
            "AND json_extract(data,'$.item')=?", (row["ts"], row["ts"] + 6 * 3600, d.get("buyer"),
                                                   model.sku_of(d))).fetchone()
        out[seq] = {k: d.get(k) for k in ("item", "price", "grade", "claim", "claim_honest", "warranty_hours",
                                          "value", "stars", "review") if k in d}
        out[seq]["broke"] = bool(broke)
    return out


def about_us(post, mine, our_seqs):
    text = (post.get("text") or "").lower()
    return (any(n in text for n in NAMES) or post.get("thread") in mine
            or bool(set(post.get("refs") or []) & our_seqs))


def stray_numbers(text, sources):
    """Numbers in our text that none of the sources gave (an internal number, or a made-up one)."""
    allowed = {n.replace(",", "") for n in re.findall(r"\d[\d,]*", " ".join(sources))}
    return [n for n in (x.replace(",", "") for x in re.findall(r"\d[\d,]*", text)) if n not in allowed]


def ask(prompt, check=lambda a: None):
    """opus (sonnet when it fails) answers one JSON object; check(answer) names a problem or None. A problem gets one
    retry with the reason; a second one drops the post (nothing goes out rather than a wrong reply)."""
    for mdl, effort in MODELS:
        p = prompt
        for attempt in (1, 2):
            t0 = time.time()
            try:
                reply = harness.call(p, timeout=300, model=mdl, effort=effort)
                a = json.loads(re.search(r"\{.*\}", reply, re.S).group())
            except Exception as e:
                log(f"{mdl} failed after {time.time() - t0:.0f}s: {type(e).__name__} {str(e)[:150]}")
                break
            problem = check(a)
            if problem is None:
                return a, f"{mdl} {time.time() - t0:.0f}s{' (retry)' if attempt == 2 else ''}"
            log(f"{mdl} answer rejected ({problem}): {reply[:200]!r}")
            p = (prompt + f"\n\nYour answer was rejected because {problem}. Write it again, keeping every rule above."
                 f"\nRejected answer: {reply[:800]}")
        else:
            return None, None
    return None, None


def promises(p):
    grades = [g for g in "ABC" if p[f"warranty_{g}"]]
    warranty = (f"goods of grade {'/'.join(grades)} carry a {int(p['warranty_hours'])}-hour warranty (the market "
                "refunds the full price from our balance if the unit breaks in that time)"
                if grades and p["warranty_hours"] > 0 else "no warranty on goods at the moment")
    return ("the market's certificate is shown on every sale; the grade we claim is always the true grade (the "
            f"ledger's claim_honest shows it); {warranty}; the shop's manager reads every complaint at its hourly "
            "policy review")


PUBLIC = {                 # what a buyer notices of a change, (up, down); every other value stays inside the shop
    "min_margin": ("firmer prices", "lower prices"),
    "max_concessions": ("more room to haggle", "quicker, firmer negotiations"),
    "c_discount": ("cheaper grade C units", "grade C units priced closer to the rest"),
}


def public_changes(since):
    """Policy changes in a buyer's words. Never the values, never the manager's reason (it quotes them)."""
    out = []
    for r in manager.jsonl(manager.LOG, since):
        words = []
        for k, (old, new) in (r.get("changed") or {}).items():
            if k in PUBLIC:
                words.append(PUBLIC[k][0] if new > old else PUBLIC[k][1])
            elif k in ("warranty_A", "warranty_B", "warranty_C"):
                words.append(f"{'a warranty' if new else 'no warranty any more'} on grade {k[-1]} goods")
            elif k == "warranty_hours":            # public anyway: it is on every receipt
                words.append(f"a {int(new)}-hour warranty on goods")
        if words:
            out.append({"when": time.strftime("%m-%d %H:%M", time.localtime(r["ts"])), "buyers_notice": words})
    return out


REPLY = """You answer posts about our shop on an agent marketplace's forum, as the shop ({shop}). Buyers read the
forum to choose shops. Be brief and plain, at most three sentences, in the post's language (English when unclear).

The post (id {id}, by {author}): {text!r}
Ledger records of our sales it cites: {records}
What the shop keeps (the only things you may promise): {promises}
Policy changes of the last day: {changes}

Thank praise. For a complaint: say what the ledger shows about it, if it cites our sales; own what went wrong; say
what the shop keeps that bears on it and that the manager will look at it at the next policy review. Never promise a
refund, a discount, a date or any change the shop has not made; never deny a record. Use no number that is not in the post,
the records or the lines above.
Answer with one JSON object only:
{{"reply": "<the reply>", "complaint": true or false, "issue": "<a few words: what the complaint is about, or empty>"}}"""

FOLLOW = """Our shop changed its policy: {change}
Complaints about the shop we answered in the last {days} days: {complaints}
For each complaint this change really answers, write a short follow-up (one or two sentences, the complaint's
language) that says what changed in words a buyer cares about ("more room to haggle", "cheaper grade C"), without
any number that is not in the change or the complaint. Skip the rest. Answer with one JSON object only:
{{"follow_ups": [{{"post": <post id>, "reply": "<text>"}}, ...]}}"""


def queue_post(md, line):
    with open(md / "outbox.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def one_pass(md, st, now):
    """At most one shop post per call, when the gap since the last one allows it."""
    p = policy.load()
    forum = read(md / "forum.json", [])
    mine = our_post_ids(md, st)
    st["mine"] = sorted(mine)
    db = model.connect()
    our_seqs = set()
    refs = {r for post in forum for r in post.get("refs") or []}
    cited = our_settles(db, refs)
    our_seqs |= set(cited)
    todo = sorted((post for post in forum if not ours(post, mine) and str(post["id"]) not in st["answered"]
                   and about_us(post, mine, our_seqs)), key=lambda post: post.get("ts", 0))
    if p["version"] != st["policy_version"] and st["complaints"]:
        plan_follow_ups(st, p, now)
    st["policy_version"] = p["version"]
    if now - st["last_post"] < GAP:
        return
    if todo:
        post = todo[0]
        records = {s: cited[s] for s in post.get("refs") or [] if s in cited}
        sources = [str(post["id"]), (post.get("text") or "")[:1500], json.dumps(records, ensure_ascii=False),
                   promises(p), json.dumps(public_changes(now - 86400), ensure_ascii=False)]
        a, used = ask(REPLY.format(shop=SHOP, id=post["id"], author=post.get("author"), text=sources[1],
                                   records=sources[2], promises=sources[3], changes=sources[4]),
                      lambda a: (f"it has numbers no source gave ({', '.join(bad)})"
                                 if (bad := stray_numbers(str(a.get("reply", "")), sources)) else None))
        st["answered"][str(post["id"])] = now
        if not a or not str(a.get("reply", "")).strip():
            log(f"no reply to post {post['id']} (model failed); skipped")
            return
        reply = str(a["reply"]).strip()[:800]
        queue_post(md, {"post": reply, "refs": list(records), "thread": post["id"], "as": "shop"})
        if a.get("complaint"):
            st["complaints"][str(post["id"])] = {"ts": now, "issue": str(a.get("issue", ""))[:120],
                                                 "text": (post.get("text") or "")[:300], "version": p["version"]}
        st["last_post"] = now
        record({"ts": now, "kind": "reply", "post": post["id"], "model": used, "complaint": a.get("complaint"),
                "issue": a.get("issue"), "text": post.get("text"), "reply": reply})
        log(f"{used}: replied to post {post['id']} ({'complaint: ' + str(a.get('issue')) if a.get('complaint') else 'praise/other'})")
    elif st["follow_ups"]:
        f = st["follow_ups"].pop(0)
        queue_post(md, {"post": f["reply"], "refs": [], "thread": f["post"], "as": "shop"})
        st["complaints"].pop(str(f["post"]), None)
        st["last_post"] = now
        record({"ts": now, "kind": "follow_up", **f})
        log(f"followed up on complaint {f['post']}")
    elif now - st["last_status"] >= STATUS_EVERY or p["version"] != st["status_version"]:
        manager.shop_post(md, p, db)
        st["last_status"] = st["last_post"] = now
        st["status_version"] = p["version"]


def plan_follow_ups(st, p, now):
    change = public_changes(st.get("checked_changes", now - 3600))
    st["checked_changes"] = now
    open_ = {k: v for k, v in st["complaints"].items() if now - v["ts"] < FOLLOW_UP_DAYS * 86400}
    st["complaints"] = open_
    if not change or not open_:
        return
    sources = [json.dumps(change, ensure_ascii=False), json.dumps(open_, ensure_ascii=False)]
    a, used = ask(FOLLOW.format(change=sources[0], days=FOLLOW_UP_DAYS, complaints=sources[1]),
                  lambda a: (f"a follow-up has numbers no source gave ({', '.join(bad)})" if (bad := [
                      n for x in a.get("follow_ups") or [] for n in stray_numbers(str(x.get("reply", "")), sources)])
                      else None))
    for f in (a or {}).get("follow_ups") or []:
        if str(f.get("post")) in open_ and str(f.get("reply", "")).strip():
            st["follow_ups"].append({"post": int(f["post"]) if str(f["post"]).isdigit() else f["post"],
                                     "reply": str(f["reply"]).strip()[:600]})
    log(f"{used}: {len(st['follow_ups'])} follow-ups queued after the policy change")


def record(rec):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    md = Path(a.dir) / ".market"
    while True:
        now = time.time()
        st = {"answered": {}, "complaints": {}, "follow_ups": [], "mine": [], "last_post": 0, "last_status": 0,
              "policy_version": None, "status_version": None, **read(STATE, {})}
        try:
            if read(md / "inventory.json", {}).get("round", 0) >= 3:
                one_pass(md, st, now)
        except Exception as e:
            log(f"skipped: {type(e).__name__}: {str(e)[:200]}")
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE)
        if a.once:
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
