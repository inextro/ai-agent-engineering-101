"""Week 08 seller: an A2A v1.0 server that sells units from the stock the kit keeps in $MARKET_DIR/inventory.json.

    python seller.py --port 9999

The model speaks, the code decides. haiku (medium effort) writes every reply to the buyer from the facts of the
unit (grade, shelf life, certificate, warranty) and the price the code chose; the code picks the unit, the price,
when to close and what goes on the receipt. Week 06 is why: with the reserve only in the prompt, haiku sold a 260
bookshelf at 230 in 2 of 3 runs. A reply without the price as its last number, or with a number other than the
price (numbers in the product's own name, the buyer's aside and a food's hours left), goes back to haiku once with the reason; a second
failure, or a call that fails or times out, is replaced by a template built from the same facts. haiku never sees the cost, the floor or the policy, so no reply
can give them away (the final price is the floor, and it is said only as the final price).

Unit: among the units the buyer named (SKU, else name, else category), skipping units held by another open
negotiation, the code keeps one unit per grade (C, B, A; for food the grade is its shelf life left), the one to
clear first within it (food closest to its date, goods held longest). Each has its own ask, floor and concession
step. haiku picks which to offer from the buyer's words: the lowest grade that meets what the buyer asked for, said
plainly and cheaper, to a buyer who minds nothing; the best grade to a buyer who looks like a fixed script (it may
refuse a low grade at checkout). A bare number or yes keeps the current unit. When haiku fails the shop offers the
best grade. Since 11-04 01:50 the code offers one grade, the best, and a lower one only to a buyer who asks for it
(offer_grade):
buyers' kits left 59% of our food C deals and 55% of goods C unpaid, none of food A.

Price:
  opening ask  = list price x the arm Thompson sampling picks (0.7/0.8/0.9/1.0, per kind, model.py), cut by
                 c_discount for grade C, never under the floor
  floor        = cost x (1 + min_margin) + warranty premium (break rate of the grade x price), then lower in the
                 last `liquidate_hours` (down to the cost) and in the last 6 hours (down to half the cost), and for
                 food close to its date (half the cost: it is thrown away at cost otherwise)
  clearance    = stock bought before the 11-03 23:55 demand fix, in a category other shops sell 1-10/h (clearance.py):
                 ask at most the market's median share of list less 0.05, floor at most 0.9 x cost; logged with
                 arm "clearance" so the bandit does not learn from it
  an offer at or above the current ask closes at the ask (more is a misreading: "2 for 50000"). Below it the shop counters: each counter moves a
  1 / (counters left) share of the way from the ask down to max(floor, offer); after `max_concessions` counters
  the floor is the final price, repeated. An offer at or above the floor after that closes. The shop never walks.
Receipt: {shop, item (SKU), price, task, unit, claim (the true grade)}, warranty_hours on goods when the policy
offers it for that grade. The certificate is always shown (no cert: false). Claims are never false.

The manager's playbook adds its negotiation guidance to haiku's prompt (no digits in it; the code's rules come
first). Every message and reply is a line in data/turns.jsonl (template or not, tries, policy and playbook
versions) for the manager's hourly review. Every negotiation is one line in data/negotiations.jsonl when it opens and one when it closes; the price bandit
counts an open line with no close after 10 minutes as no deal. Requests for things not in stock go to
data/missed.jsonl (are buyers asking shops that hold none of it?).
"""
import argparse
import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization
from starlette.applications import Starlette

from a2a.helpers import get_message_text, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

import clearance
import harness
import model
import policy

SHOP = os.environ.get("SHOP_TITLE", "Inextro Market")       # the display name; the market knows us as `inextro`
MARKET_DIR = Path(os.environ.get("MARKET_DIR", ".market"))
NEGOTIATIONS = model.NEGOTIATIONS
MISSED = policy.DATA / "missed.jsonl"
TURNS = policy.DATA / "turns.jsonl"         # every message and reply, for the manager's hourly review
REPLY_MODEL, REPLY_EFFORT, REPLY_TIMEOUT = "haiku", "medium", 20
GRADE_WORDS = {
    "food": {"A": "grade A, plenty of shelf life left", "B": "grade B, a fair amount of shelf life left",
             "C": "grade C, close to its date"},
    "goods": {"A": "grade A, in normal condition with no defects", "B": "grade B, the box is damaged, the item is fine",
              "C": "grade C, refurbished"},
}
NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
ACCEPT = re.compile(r"\b(accept|accepted|agree|agreed|deal|ok|okay|yes|buy it|take it)\b|좋아|좋습니다|살게|구매할게|확정")
REFUSE = re.compile(r"\b(no|not|don't|dont|won't|cannot|can't|never|reject|walk)\b|안 |못 |싫")
SKU_RE = re.compile(r"\bG[0-9a-f]{8}\b")
# a buyer who asks for a lower grade in so many words (it is offered the grades then; anyone else gets the best)
LOWER_OK = re.compile(r"\b(?:grade\s*[bc]|[bc][ -]grade|refurb\w*|any grade|any condition|cheapest|cheaper one|"
                      r"box damaged|damaged box|close to (?:its|the) date|near(?:ing)? expiry)\b|리퍼|[BC]\s*등급|"
                      r"등급\s*[BC]|아무 등급|임박|박스\s*훼손|가장 싼|제일 싼", re.I)
# amounts in words (a price the code did not choose, or another grade's floor); the warranty's "six-hour" stays allowed
NUMBER_TEXT = re.compile(r"\b(?:hundreds?|thousands?|millions?|grand|twenty|thirty|forty|fifty|sixty|seventy|eighty|"
                         r"ninety|ten|eleven|twelve|fifteen)\b|\d\s*k\b|[0-9일이삼사오육칠팔구십백]\s*[만천](?![가-힣])|만\s*원|천\s*원", re.I)


def log(msg):
    print(f"[seller] {msg}", flush=True)


def append(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def accepts(text):
    """A plain yes. A question ("okay, what is your best price?", "is this deal still on?") is not one."""
    t = text.lower()
    return bool(ACCEPT.search(t)) and not REFUSE.search(t) and "?" not in t


UNIT_AFTER = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:hours?\b|hrs?\b|h\b|시간|days?\b|일\b|kg\b|g\b|ml\b|l\b|개|units?\b|"
                        r"pcs\b|%|percent|퍼센트|stars?\b|점\b)", re.I)


def offer_in(text, name, ref=None):
    """The buyer's number: the last amount in the message that is not part of the product's name, a SKU, a quantity
    with a unit ("24 hours", "2 units", "10kg") or, next to the price under talk, too small to be a price (under a
    tenth of `ref`: "2 for 50000"). "3만 5천", "35천" and "35k" read as 35000."""
    clean = SKU_RE.sub(" ", text)
    # amounts in words first: removing the name's "5" (5단 서랍장) from "3만 5천" left "3만 천", read as 30,000,000
    clean = re.sub(r"(\d+(?:\.\d+)?)\s*만(?:\s*(\d+)\s*천)?",
                   lambda m: f" {round(float(m.group(1)) * 10000 + int(m.group(2) or 0) * 1000)} ", clean)
    clean = re.sub(r"(\d+(?:\.\d+)?)\s*(?:천|k\b)", lambda m: f" {round(float(m.group(1)) * 1000)} ", clean, flags=re.I)
    for n in re.findall(r"\d[\d,]*", name or ""):   # whole numbers only: "900g" in the name must not eat "9000"
        clean = re.sub(rf"(?<![\d,]){re.escape(n)}(?![\d,])", " ", clean)
    clean = UNIT_AFTER.sub(" ", clean)
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", clean) if n.replace(",", "").isdigit()]
    if ref:
        nums = [n for n in nums if n >= 0.1 * ref]
    return nums[-1] if nums else None


MIN_RAISE = 0.01    # share of the current price a buyer's new offer must add to count as raising
CLAIM_AHEAD = 30    # seconds from the close to the checkout (the buyer's kit checks out on the receipt)


def catalog():
    try:
        return json.loads((MARKET_DIR / "catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def grade_now(u, now=None):
    """The grade a unit has at checkout. Goods keep theirs; food drops with its shelf life left (A 80% or more,
    B 45% or more, C less), and inventory.json can be 10 s old and the talk minutes long, so it is worked out
    again from expires_at and the SKU's shelf_hours, CLAIM_AHEAD seconds on, never above the stored grade."""
    g = u.get("grade")
    shelf = (catalog().get(u.get("sku")) or {}).get("shelf_hours")
    if u.get("kind") != "food" or not u.get("expires_at") or not shelf or g not in RANK:
        return g
    left = (u["expires_at"] - (now or time.time()) - CLAIM_AHEAD) / (shelf * 3600)
    return min(g, "A" if left >= 0.8 else "B" if left >= 0.45 else "C", key=RANK.get)


def inventory():
    try:
        return json.loads((MARKET_DIR / "inventory.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


RANK = {"C": 0, "B": 1, "A": 2}
# 11-04 16:50: a lower goods grade held this long, and cheaper than the best by this much, is mentioned on the
# first reply (no price); the buyer who asks for it gets it, everyone else stays on the best grade. 13-16 h, 221
# talks came for SKUs where old C sat behind A or B, and only 20 of 2,338 buyers asked for a lower grade unasked.
MENTION_H = 3
MENTION_CHEAPER = 0.95
LOWER_WORDS = {"B": "box-damaged (grade B, the item is fine)", "C": "refurbished (grade C)"}
SOLD_HOLD = 6 * 60    # a unit sold stays out of offers this long (inventory.json drops it at the checkout; the kit
                      # pays within its 300 s order, so an unpaid close frees the unit soon after)
ABANDONED = 10 * 60   # a talk this old is over (the kit stops a buyer at 300 s, often without a CancelTask)


# How long the buyer needs the food to last, as buyers write it (seen in Round 3 on 11-03, 75 of 1,175 first
# messages, every one of them matched): "Must last at least 84 hours", "it must last 37.0 hours", "I need it to
# last 48 hours", "Need at least 12 h of shelf life", "at least 18h shelf life". A food unit that does not last
# that long is worth 0 to the buyer (agent.md), so its kit does not pay at any price: of our food deals with such a
# line, the 4 whose unit fell short were never paid and the 2 that met it were.
NEED_HOURS = re.compile(r"(?:\blast\b|shelf[ -]?life|유통기한|남은)[^.;\n]{0,40}?(\d+(?:\.\d+)?)\s*(?:h\b|hrs?\b|hours?\b|시간)"
                        r"|(\d+(?:\.\d+)?)\s*(?:h\b|hrs?\b|hours?\b|시간)\s*(?:of\s+)?(?:shelf[ -]?life|이상\s*남)", re.I)
NEED_MARGIN_H = 1.0   # the checkout comes after the talk: the unit must still meet the need then


def need_hours(text):
    """Hours the buyer says the food must last, None when it says nothing."""
    m = NEED_HOURS.search(text or "")
    return float(m.group(1) or m.group(2)) if m else None


NAMED_GRADE = re.compile(r"\bgrade\s*([abc])\b|\b([abc])[ -]grade\b|([ABC])\s*등급|등급\s*([ABC])", re.I)
# 11-04 20:32: a goods B held is offered before A to a buyer who names no grade. 72% of goods supply is A and the
# best-first rule left B behind it (자코모 4인 G8ec51935 at 20:11: 13 A, 36 B); per list price a B earns 0.14 after
# refunds against A's 0.07, a goods talk opened on B closed 62% against A's 49% (13-20 h), and B was paid 89% against
# A's 94% (18-20 h). Offline 6 h: +10-25M gross with OLD_C_H 3. C still comes only by asking or the fallback.
B_FIRST = False   # off from 11-04 20:51: opened on B with A held, goods talks closed 7 of 25 (A 9 of 14); 자코모 4인 1 of 10


def offer_grade(text, opts):
    """The one grade to offer: the best held, unless the buyer asks for a lower one (LOWER_OK). Then the lowest
    grade the buyer named that we hold, else the lowest grade at or above the lowest named, else our lowest.
    Goods B goes first (B_FIRST) to a buyer who names no grade and asks for no lower one."""
    held = sorted(opts, key=RANK.get)                    # lowest first
    if not LOWER_OK.search(text or ""):
        if (B_FIRST and "B" in opts and opts["B"]["unit"].get("kind", "goods") == "goods"
                and not NAMED_GRADE.search(text or "")):
            return "B"
        return held[-1]
    named = {next(x for x in m.groups() if x).upper() for m in NAMED_GRADE.finditer(text)}
    named = {g for g in named if g in RANK}
    if not named:
        return held[0]
    for g in held:
        if g in named:
            return g
    least = min(named, key=RANK.get)
    return next((g for g in held if RANK[g] >= RANK[least]), held[-1])


def mention_alt(g, opts):
    """The lower goods grade to mention next to grade g: the lowest held MENTION_H hours or more and priced at most
    MENTION_CHEAPER x g's ask; None when there is none (or g is already the lowest, or the item is food)."""
    now = time.time()
    for k in sorted(opts, key=RANK.get):
        u = opts[k]["unit"]
        if RANK[k] >= RANK[g]:
            return None
        if (u.get("kind", "goods") == "goods" and now - (u.get("supplied_at") or now) >= MENTION_H * 3600
                and opts[k]["ask"] <= MENTION_CHEAPER * opts[g]["ask"]):
            return k
    return None


# 11-04 19:39: when the price of an A or B goods unit is final and the buyer's offer is still under what closes, a C
# unit of the same SKU whose floor the offer meets is offered once, at the buyer's offer; the buyer who says yes on
# its next message gets it. Only for a buyer who never named a grade. 13-19 h, 164 goods talks ended that way with a
# C held and the buyer's best number at or over its floor, while A or B held kept the seller from offering any C
# (none of those SKUs' C sold 16:31-19:03). The open record keeps the arm; the deal has "fallback" and the price
# bandit leaves it out (model.bandit_stats).
# 11-04 21:00: the fallback offers B before C after a final A (B_FALLBACK). Other shops sell goods B at its supply
# share (18-23% of their goods deals, at 0.02-0.05 of list under their A) and ours sat at 13%: buyers refuse a B put
# before an A held (20:33-20:45, 7 of 25 closed, B_FIRST) but take one when the A is gone (62%), and 5 of the first
# 7 C fallback offers closed. B is paid 89%, C 55%.
FALLBACK_GRADES = ("A", "B")
# 11-05 00:20 (owner: sell C when we can, at least at cost): a fallback the buyer let lapse (a lower number) came
# back once, when the buyer's number is again at or over the last one offered. Before, one offer per talk: of 119
# offers (11-04) 14 lapsed and the buyer later sent at least its price, 5 of those ended with no deal (3.5M at the
# fallback price; e8dec12d 다우닝 3인: C offered at 1,550,000, buyer 1,400,000, then 1,500,000 ... 1,775,000, cancelled).
FALLBACK_MAX = 2
B_FALLBACK = True


def wants_lower(text, g):
    """The buyer asks for the lower grade g: "refurbished", "cheaper one", "grade C" (LOWER_OK) or names g."""
    named = {next(x for x in m.groups() if x).upper() for m in NAMED_GRADE.finditer(text or "")}
    return bool(LOWER_OK.search(text or "")) or g in named


def find_units(text, taken):
    """One unit per grade of what the buyer named, the one to clear first within the grade (food closest to its
    date, goods the cheapest): {grade: unit}, empty when there is none. Food that would not last the hours the
    buyer asks for (need_hours) is left out.
    A goods unit carries _pool_cost, the mean cost of the units of its SKU and grade we hold (not in another talk),
    and its floor is priced on that (floor_price). Units of one SKU and grade cost different amounts (Ultima A
    159k, 196k, 201k on 11-04), and until 11-04 14 h the one held longest was offered on its own cost: the 201k unit
    every time, a floor of 230k against a market at 210k. Cheapest first on the mean cost keeps the whole SKU and
    grade above cost x (1 + min_margin): what is left after the cheap unit costs more on average, so the next
    floor rises (8 h of 11-04 talks: 49 lost ones had a buyer at or above that floor, 171만 of margin).
    A goods unit held clearance.STALE_H hours goes before the cheapest (since 11-04 16:50): it is the one on sale."""
    inv = inventory()
    if inv.get("round", 0) < 3:
        return {}, inv
    want = text.lower()
    units = [u for u in inv.get("units", []) if u.get("grade") in RANK and u["unit"] not in taken]
    need = need_hours(text)
    if need is not None:
        until = time.time() + (need + NEED_MARGIN_H) * 3600
        units = [u for u in units if u.get("kind") != "food" or not u.get("expires_at") or u["expires_at"] >= until]
    named = [u for u in units if u["sku"].lower() in want]
    if not named:
        named = [u for u in units if (u.get("name") or "").lower() in want]
    if not named:
        named = [u for u in units if (u.get("category") or "").split(">")[-1].lower() in want]
    by_grade = {}
    now = time.time()                    # a goods unit held clearance.STALE_H hours goes first (11-04 16:50)
    for u in sorted(named, key=lambda u: (u.get("expires_at") or 0, not clearance.stale(u, now),
                                          u["cost"] if u.get("kind") == "goods" else 0, u.get("supplied_at") or 0)):
        by_grade.setdefault(u["grade"], u)
    for g, u in by_grade.items():
        if u.get("kind") == "goods":
            same = [x["cost"] for x in named if x["sku"] == u["sku"] and x["grade"] == g]
            by_grade[g] = dict(u, _pool_cost=sum(same) / len(same))
    return by_grade, inv


SAY_MARGIN_H = 0.5   # hours kept back from what the shop says is left: the talk and the checkout come after it


def hours_said(u, now=None):
    """The whole hours of shelf life the shop says a food unit has left ("at least N hours"), counted down from its
    expires_at less SAY_MARGIN_H; None for goods. Until 11-04 the shop said it only in words ("more than a week",
    "several days": digits other than the price failed the reply check), and buyers who need the food to last a
    stated number of hours could not tell; haiku guessed instead."""
    if u.get("kind") != "food" or not u.get("expires_at"):
        return None
    return max(0, int((u["expires_at"] - (now or time.time())) / 3600 - SAY_MARGIN_H))


def facts(u, warranty):
    """What the shop may say about the unit. The only digits are the price and, for food, its hours left."""
    kind = u.get("kind", "goods")
    parts = [GRADE_WORDS[kind].get(u.get("grade"), f"grade {u.get('grade')}"),
             "the market's certificate of its grade is shown at checkout"]
    h = hours_said(u)
    if h is not None:
        parts.append(f"at least {h} hours of shelf life left" if h >= 1 else "it must be used within the hour")
    if warranty:
        parts.append(f"a {NUMBER_WORDS.get(warranty, warranty)}-hour warranty: if it breaks in that time the market "
                     "refunds the full price from our balance")
    elif kind == "goods":
        parts.append("no warranty")
    return "; ".join(parts)


def check_reply(reply, u, price, buyer_text):
    """The code's check on a reply, None when it passes, else what is wrong (haiku hears it on its one retry): the
    price as digits, last, and no digits but the price, the product name's, the buyer's own and the food's hours left
    from facts() (one either side: the hour can turn between the prompt and the check) (made-up numbers).
    Calling a price final is left to the prompt: a word check rejected good replies ("it should last", "not final")."""
    allowed = {n.replace(",", "") for n in re.findall(r"\d[\d,]*", (u.get("name") or "") + " " + buyer_text)}
    h = hours_said(u)
    if h is not None:
        allowed |= {str(max(0, h + d)) for d in (-1, 0, 1)}
    nums = [n.replace(",", "") for n in re.findall(r"\d[\d,]*", reply)]
    others = [n for n in nums if n != str(price) and n not in allowed]
    if not reply:
        return "the reply was empty"
    if not nums or nums[-1] != str(price):
        return f"the price {price} must be written as digits as the very last thing in the message"
    if others:
        return f"it has numbers other than the price ({', '.join(others)}); drop them"
    w = NUMBER_TEXT.search(reply)
    if w:
        return f"it writes an amount in words ({w.group()}); the only amount may be the price, in digits, last"
    return None


def guidance():
    """The manager's negotiation guidance (playbook.json), as a prompt line; empty when there is none."""
    g = policy.load_playbook().get("negotiation")
    return (f"Shop manager's guidance (follow it where it fits; every rule here and what the shop does now come "
            f"first): {g}\n") if g else ""


def ask_checked(prompt, parse, tag, how=None):
    """haiku's answer through parse(answer) -> (value, problem). A problem gets one retry with the reason and the
    rejected answer; None when the call fails or both answers fail (the caller then uses a template). `how` gets
    the number of tries and why the last one failed, for data/turns.jsonl."""
    how = how if how is not None else {}
    for attempt in (1, 2):
        how["tries"] = attempt
        t0 = time.time()
        try:
            answer = harness.call(prompt, timeout=REPLY_TIMEOUT, model=REPLY_MODEL, effort=REPLY_EFFORT).strip()
        except Exception as e:
            log(f"{tag}: harness failed after {time.time() - t0:.1f}s ({type(e).__name__}), template")
            how["failed"] = f"harness {type(e).__name__}"
            return None
        value, problem = parse(answer)
        if problem is None:
            log(f"{tag}: {time.time() - t0:.1f}s{' (retry)' if attempt == 2 else ''} {answer[:240]!r}")
            return value
        log(f"{tag}: {time.time() - t0:.1f}s rejected ({problem}): {answer[:160]!r}")
        how["failed"] = problem
        prompt += (f"\n\nYour answer was rejected because {problem}. Write it again, keeping every rule above.\n"
                   f"Rejected answer: {answer[:600]}")
    log(f"{tag}: rejected twice, template")
    return None


def write_reply(u, price, situation, history, buyer_text, template, how=None, closing=False):
    prompt = (
        f"You are the sales clerk of {SHOP}, an online shop. A buyer is negotiating for one unit of "
        f"{u['name']!r}.\n"
        f"Facts about this unit (the only things you may state about it): {facts(u, u.get('_warranty'))}.\n"
        f"What the shop does now: {situation}\n"
        f"{'' if closing else guidance()}"   # a closing turn has nothing to steer; guidance once made it ask again
        f"Conversation so far:\n{history}\n"
        f"Buyer's last message: {buyer_text!r}\n\n"
        "Write the shop's reply in at most three short sentences, in the buyer's language (English when it is "
        "unclear). Answer the buyer's questions using only the facts above; if they ask for something not in the "
        "facts (delivery dates, returns, freebies, a manufacture date), say plainly that the shop cannot promise it. "
        "Never invent facts. Unless the shop's move says the price is final, do not call it final, best or lowest. "
        f"The price is {price}: write it exactly once, as digits, as the last thing in the message. Write no "
        "other digits (except the hours of shelf life left, exactly as the facts give them, when the buyer asks "
        "how long it keeps or says how long it must last) and no other amounts, not even in words. "
        "Reply with the message text only.")
    reply = ask_checked(prompt, lambda a: (a, check_reply(a, u, price, buyer_text)), "reply", how)
    return template if reply is None else reply


CHOOSE = """You are the sales clerk of {shop}, an online shop. A buyer wants one unit of what they asked for, and the
shop has units of different grades it could sell. The code has already set the price of each for this turn:

{options}

The shop would rather sell the lower grades (older stock), but only to a buyer who does not mind them:
- offer the lowest grade that meets what the buyer asked for (freshness, how long it must keep, condition, new or
  not); if they object to the grade on offer, move up to one that meets the objection;
- the buyer has said nothing about condition and writes like a person or a language model (free text, questions):
  offer the lowest grade, say plainly what it is and that it is the cheaper one; you may say better grades cost
  more, without their numbers;
- the buyer looks like a fixed script (template wording, bare numbers) and has said nothing about condition: offer
  the best grade, since a script may refuse a low grade at checkout;
- the shop currently offers {current}: keep it unless the buyer's words give a reason to switch.
{guide}
Conversation so far:
{history}
Buyer's last message: {buyer!r}

Answer with one JSON object only: {{"unit": one of {keys}, "reply": "<at most three short sentences in the buyer's
language (English when it is unclear), stating only facts of that unit; if they ask for something not in the
facts, say the shop cannot promise it; unless that unit's move says the price is final, do not call it final, best
or lowest; the price of that unit exactly once, as digits, as the last thing; no other digits except that unit's
hours of shelf life left exactly as its facts give them, when the buyer asks how long it keeps or says how long it
must last>"}}"""


def choose_reply(deal, moves, history, buyer_text, how=None):
    """haiku picks the grade from what the buyer said and writes the reply; the code checks it like any reply."""
    o = deal["opts"]
    fallback = deal["cur"] or max(o, key=RANK.get)   # unsure: the best grade (a buyer who minds rates a low one badly)
    options = "\n".join(f"{k} ({o[k]['unit']['name']}): {facts(o[k]['unit'], o[k]['warranty'])}. "
                        f"If you offer {k}: {situation(moves[k])}" for k in sorted(o, key=RANK.get))
    prompt = CHOOSE.format(shop=SHOP, options=options, current=deal["cur"] or "nothing yet",
                           keys=" or ".join(f'"{k}"' for k in sorted(o, key=RANK.get)), history=history,
                           buyer=buyer_text, guide=guidance())

    def parse(answer):
        try:
            a = json.loads(re.search(r"\{.*\}", answer, re.S).group())
            key, reply = str(a["unit"]).upper(), str(a["reply"]).strip()
        except (AttributeError, ValueError, KeyError, TypeError):
            return None, "the answer was not one JSON object with \"unit\" and \"reply\""
        if key not in o:
            return None, f"\"unit\" must be one of {', '.join(o)}"
        return (key, reply), check_reply(reply, o[key]["unit"], moves[key]["price"], buyer_text)

    got = ask_checked(prompt, parse, "choose", how)
    if got is None:
        return fallback, template(o[fallback]["unit"], moves[fallback])
    return got


# Goods of these grades get the warranty only when the buyer asks for one in its first message (owner, 11-05 00:50).
# Market goods settled 11-04 (break window closed), per sale as a share of list: C without a warranty 0.676 paid,
# no refund; with one 0.681 paid, 0.203 refunded (ours 0.666 / 0.200); B 0.721 / 0 against 0.727 / 0.069 (ours
# 0.726 / 0.109). Buyers do not pay for it and stars come at the sale, before any break. Of our first messages 8%
# (C), 13% (B), 31% (A) ask for a warranty; with one B and C still clear their cost, so those buyers keep it.
WARRANTY_ON_ASK = ("B", "C")
MKT_BASE = 0.95          # a goods ask starts from at least this share of the market's price for its SKU and grade
WANTS_WARRANTY = re.compile(r"warrant|guarantee|보증|보장", re.I)


def option(u, p, arm, wants_warranty=True):
    """One unit the shop can offer, with its own asking price, floor and concession step. wants_warranty: the buyer
    asked for a warranty (WARRANTY_ON_ASK grades get one only then)."""
    kind = u.get("kind", "goods")
    warranty = int(p["warranty_hours"]) if kind == "goods" and p.get(f"warranty_{u.get('grade')}") else 0
    if u.get("grade") in WARRANTY_ON_ASK and not wants_warranty:
        warranty = 0
    u = dict(u, _warranty=warranty)
    if kind == "goods" and arm in policy.GOODS_ARMS:
        # goods: a premium over the floor (policy.GOODS_ARMS, 11-05 00:22); the floor's break premium is priced at
        # what we ask, so the ask is set from a floor at 0.8 of list and the floor then follows that ask. Never from
        # under MKT_BASE x what the market pays for the SKU and grade: without a warranty a C's floor is about 0.39
        # of list and the market pays 0.68 for it (11-05 00:50)
        m = clearance.market_ratio(u)
        base = max(floor_price(u, p, warranty, 0.8 * u["list_price"]), MKT_BASE * m * u["list_price"] if m else 0)
        ask = min(u["list_price"], base * (1 + arm))
    else:
        ask = u["list_price"] * arm * ((1 - p["c_discount"]) if u.get("grade") == "C" else 1)
    clear = clearance.terms(u, c_discount=p["c_discount"], floor=floor_price(u, p, warranty, ask))
    #   bought before the demand fix in a category the market buys, stuck, underwater (its usual floor over the
    #   market's price for its SKU and grade, 11-04 23:50), or goods held clearance.STALE_H hours: cheaper now
    if clear:
        ask = min(ask, clear["ask"])
    floor = floor_price(u, p, warranty, ask)
    if clear:
        floor = max(1, min(floor, round(clear["floor"])))
    return {"unit": u, "ask": max(round(ask), floor), "floor": floor, "step": 0, "warranty": warranty,
            "clearance": bool(clear)}


SMALL_GAP = 0.02   # an offer at or above the floor and this close to the price under talk closes at the offer
# 11-04: 4 deals in 24 h were lost when a raise past the ask came on the last turn and the shop asked to confirm
OVER_RAISE = 1.2
STALLS_FINAL = 3   # this many messages in a row without a higher offer and the current price becomes final
HOLD_GAP = 0.05    # when the price would be (or is) final, an offer at or above the floor this close to it closes
# once the price is final, an offer this far under the floor still closes (never under the cost): 11-03 21:00 to
# 11-04 13:00, 22 talks walked within 1% under the floor (59만 margin lost), and only 30 deals at the floor had an
# earlier offer in that band (4만 given up). The floor is cost x 1.08 + the warranty refund, so 1% under it still pays
FLOOR_TOL = 0.01


def close_floor(o):
    """The lowest offer that closes once the price is final: the floor less FLOOR_TOL, but never under the cost (a
    floor already at or under the cost, for clearance or the last hours, stays as it is)."""
    lo = o["floor"] * (1 - FLOOR_TOL)
    return o["floor"] if lo < (o["unit"].get("_pool_cost") or o["unit"]["cost"]) else lo


def next_move(o, offer, steps, opening, accepted, over_seen=False, moved=True, stalls=0, frozen=False,
              raised=False):
    """What the shop does with option o on this buyer message, without changing o: close at a price, or say a price
    (the opening ask, a concession, the floor as the final price, the ask again when the offer is above it, or the
    same price when the buyer did not move). The shop concedes only when the buyer raised its offer: a message with
    no number or with the same or a lower number holds the price, and after STALLS_FINAL numbers in a row that did
    not rise the held price is final and stays so (frozen: a price called final never moves again). A lowball
    repeated never reaches the floor; a buyer who keeps raising walks the ladder. A buyer who raised an earlier
    offer to just past the ask (raised, at most OVER_RAISE x ask) is bidding for one unit: it closes at the ask."""
    if offer is not None and offer > o["ask"] and not over_seen and not (raised and offer <= OVER_RAISE * o["ask"]):
        # above the ask is a misreading ("2 for 50000"): say the price of one unit; the same again closes at the ask
        return {"close": False, "price": o["ask"], "step": o["step"], "final": o["step"] >= steps,
                "opening": opening, "over": offer}
    if opening and offer is None:
        return {"close": False, "price": o["ask"], "step": 0, "final": False, "opening": True}
    if offer is None and accepted:
        return {"close": True, "price": o["ask"], "step": o["step"], "final": True}
    if offer is not None and (offer >= o["ask"] or (o["step"] >= steps and offer >= close_floor(o))):
        # never above the ask: an offer over it is a misreading ("2 for 50000" read as 50000 for one)
        return {"close": True, "price": min(offer, o["ask"]), "step": o["step"], "final": True}
    if offer is not None and offer >= o["floor"] and o["ask"] - offer <= SMALL_GAP * o["ask"]:
        # a few hundred won apart is not worth the deal: buyers who raise under MIN_RAISE froze the price and walked
        # 45 times in 10 h at 2% or less above our floor (11-04 report, owner: mirror the buyer's SMALL_STEP)
        return {"close": True, "price": offer, "step": o["step"], "final": True}
    if opening:
        # the first number is where the buyer starts, not a raise: the shop keeps its asking price
        return {"close": False, "price": o["ask"], "step": o["step"], "final": False, "opening": True,
                "offer": offer, "first": True}
    if not moved or frozen:
        final = frozen or o["step"] >= steps or (offer is not None and stalls + 1 >= STALLS_FINAL)
        if final and offer is not None and offer >= close_floor(o) and o["ask"] - offer <= HOLD_GAP * o["ask"]:
            # a buyer who kept raising a little is at its limit: 07:18-09:54 two such talks walked at 3.7% under a
            # held final (218k profit lost) and holding there won 0 deals of 810, so close at the offer instead
            return {"close": True, "price": offer, "step": o["step"], "final": True}
        return {"close": False, "price": o["ask"], "step": o["step"], "offer": offer, "hold": True, "final": final}
    left = steps - o["step"]
    if left <= 0:
        ask, step = o["floor"], steps
    else:
        ask = max(o["floor"], round(o["ask"] - (o["ask"] - max(o["floor"], offer or 0)) / left))
        step = o["step"] + 1
        if step >= steps or ask == o["floor"]:
            ask, step = o["floor"], steps
    if offer is not None and (offer >= ask or (step >= steps and offer >= close_floor(o))):
        return {"close": True, "price": min(offer, ask), "step": step, "final": True}
    return {"close": False, "price": ask, "step": step, "final": step >= steps, "offer": offer}


def situation(m):
    p = m["price"]
    if "over" in m:
        return (f"the buyer's number ({m['over']}) is above the price; it may be for several units or a mistake. The "
                f"shop sells one unit at {p}; ask them to confirm.")
    if m["close"]:
        return f"the deal closes at {p} and the signed receipt is attached; thank the buyer briefly."
    if m.get("first"):
        return f"the buyer opened with {m['offer']}; the asking price is {p}; invite a better offer as a number."
    if m.get("opening"):
        return f"the asking price is {p}; ask for the buyer's offer as a number."
    said = f"the buyer offered {m['offer']}; " if m.get("offer") is not None else ""
    if m.get("hold") and not m["final"]:
        return (f"{said}the buyer has not raised their offer, so the shop stays at {p}; invite a better offer, and say "
                "nothing about whether this price is final, best or lowest.")
    if m["final"]:
        return f"{said}{p} is the shop's final price; say so politely."
    return f"{said}the shop comes down to {p}; say nothing about whether this price is final, best or lowest."


def template(u, m):
    p = m["price"]
    if m["close"]:
        return f"Deal: {u['name']}. Receipt attached. Price {p}"
    if m.get("opening"):
        return f"{u['name']}: {facts(u, u.get('_warranty'))}. Send your offer as a number. Asking {p}"
    word = "final price" if m["final"] else "we can do better if you raise your offer" if m.get("hold") \
        else "this unit's price" if m.get("swapped") else "we came down"
    return f"{u['name']}: {word}. Asking {p}"


def bare(text):
    """Only a number or a plain yes: nothing in it that could be a reason to switch units."""
    rest = ACCEPT.sub(" ", re.sub(r"\d[\d,.]*", " ", SKU_RE.sub(" ", text.lower())))
    return len(re.sub(r"[\W_]|won|krw|원", "", rest)) <= 3


def floor_price(u, p, warranty, est_price):
    cost = u.get("_pool_cost") or u["cost"]          # goods: the mean cost of the SKU and grade held (find_units)
    premium = BREAK.get(u.get("grade"), max(BREAK.values())) * est_price if warranty else 0
    floor = cost * (1 + p["min_margin"]) + premium
    left = policy.hours_left()
    if left < policy.LAST_HOURS_BELOW_COST:
        floor = 0.5 * cost
    elif left < p["liquidate_hours"]:
        span = p["liquidate_hours"] - policy.LAST_HOURS_BELOW_COST
        frac = (left - policy.LAST_HOURS_BELOW_COST) / span
        floor = cost + (floor - cost) * frac
    if u.get("kind") == "food" and u.get("expires_at") and u["expires_at"] - time.time() < 3 * 3600:
        floor = min(floor, 0.5 * cost)
    return max(1, round(floor))


BREAK = dict(model.BREAK_PRIOR)   # refreshed from model.py at most every 10 minutes
_break_at = 0


def refresh_break_rates():
    global _break_at
    if time.time() - _break_at < 600:
        return
    _break_at = time.time()
    try:
        BREAK.update(model.Estimates().break_rate)
    except Exception as e:
        log(f"break rates unavailable ({type(e).__name__}), keeping {BREAK}")


def sign_receipt(receipt, key_path="shop_key.pem"):
    key = serialization.load_pem_private_key(open(key_path, "rb").read(), password=None)
    body = {k: receipt[k] for k in ("shop", "item", "price", "task")}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    receipt["sig_seller"] = base64.b64encode(key.sign(canon)).decode()
    return receipt


class Seller(AgentExecutor):
    def __init__(self):
        self.open = {}   # task id -> deal
        self.sold = {}   # unit -> close time: still in inventory.json until the checkout, so still held

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        up = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        say = lambda text: up.new_agent_message(parts=[new_text_part(text)])
        text = get_message_text(context.message) or ""
        log(f"[task {task.id[:8]}] buyer: {text[:200]!r}")
        deal = self.open.get(task.id)
        if deal is None:
            await self.opening(task.id, text, up, say)
        else:
            await self.turn(task.id, deal, text, up, say)

    def held(self, but=None):
        """Units promised in open negotiations: the unit each one offers now (one still choosing holds none; if two
        pick the same unit, the market refuses the second checkout)."""
        now = time.time()
        self.sold = {k: t for k, t in self.sold.items() if now - t < SOLD_HOLD}
        for t in [t for t, d in self.open.items() if now - d["t0"] > ABANDONED]:   # the buyer left: free its unit
            d = self.open.pop(t)
            o = d["opts"][d["cur"] or next(iter(d["opts"]))]
            append(NEGOTIATIONS, {"ts": now, "task": t, "outcome": "cancel", "why": "abandoned",
                                  "kind": o["unit"].get("kind"), "arm": d.get("log_arm", d["arm"]), "last_ask": o["ask"]})
        return ({d["opts"][d["cur"]]["unit"]["unit"] for t, d in self.open.items() if t != but and d["cur"]}
                | set(self.sold))

    def swap_if_taken(self, task_id, deal, key):
        """Two talks that opened together may pick the same unit while haiku answers: the later one moves to
        another unit of the same SKU and grade (its floor never lower). None when nothing changed, True when it
        swapped, False when no unit is left."""
        o = deal["opts"][key]
        held = self.held(task_id)
        if o["unit"]["unit"] not in held:
            return None
        u = o["unit"]
        alt = next((x for x in inventory().get("units", []) if x["sku"] == u["sku"] and x.get("grade") == u.get("grade")
                    and x["unit"] not in held and x["unit"] != u["unit"]), None)
        if alt is None:
            return False
        new = option(alt, policy.load(), deal["arm"], deal.get("wants_warranty", True))
        o["unit"], o["floor"] = new["unit"], max(o["floor"], new["floor"])
        log(f"[task {task_id[:8]}] unit {u['unit']} was taken meanwhile; offering {alt['unit']}")
        return True

    def c_fallback(self, task_id, deal, key, m, offer):
        """The lower unit to offer once at the buyer's offer, or None (FALLBACK_GRADES): the price of an A or B goods
        unit is final, the offer is under what closes, the buyer never named a grade, and a lower unit of the SKU not
        in another talk has a floor the offer meets: B first after an A (B_FALLBACK), else C. It goes into the talk
        as an option of its grade, asking the offer, final."""
        o = deal["opts"].get(key)
        if (o is None or deal.get("fb_done", 0) >= FALLBACK_MAX or deal.get("named") or offer is None or m["close"]
                or not m["final"] or (deal.get("fb_done") and offer < deal.get("fb_last", 0))
                or o["unit"].get("kind", "goods") != "goods" or o["unit"].get("grade") not in FALLBACK_GRADES
                or offer >= close_floor(o)):
            return None
        held = self.held(task_id)
        u = o["unit"]
        units = inventory().get("units", [])
        lower = [g for g in (("B", "C") if B_FALLBACK else ("C",)) if RANK[g] < RANK[u.get("grade")]
                 and g not in deal["opts"]]
        for g in lower:
            cs = [x for x in units if x["sku"] == u["sku"] and x.get("grade") == g and x["unit"] not in held]
            if not cs:
                continue
            c = min(cs, key=lambda x: (x.get("supplied_at") or 0))          # the one held longest
            c = dict(c, _pool_cost=sum(x["cost"] for x in cs) / len(cs))
            co = option(c, policy.load(), deal["arm"], deal.get("wants_warranty", True))
            if offer < close_floor(co):
                continue
            co["ask"], co["step"] = max(round(offer), co["floor"]), deal["steps"]
            deal["fb_done"], deal["fb_last"] = deal.get("fb_done", 0) + 1, co["ask"]
            return co
        return None

    async def opening(self, task_id, text, up, say):
        by_grade, inv = find_units(text, self.held())
        if not by_grade:
            need = need_hours(text)
            append(MISSED, {"ts": time.time(), "text": text[:300], "round": inv.get("round"), "need_hours": need})
            log(f"[task {task_id[:8]}] nothing in stock for this request"
                + (f" (lasting {need:g} h)" if need is not None else ""))
            await up.reject(say("Sorry, we have none of that in stock right now."))
            return
        p = policy.load()
        await asyncio.to_thread(refresh_break_rates)       # both read whole files: off the event loop
        await asyncio.to_thread(clearance.table)           # rebuilt from the ledger every 10 min: off the loop too
        arm = await asyncio.to_thread(model.pick_arm, next(iter(by_grade.values())).get("kind", "goods"))
        wants_w = bool(WANTS_WARRANTY.search(text or ""))
        opts = {g: option(u, p, arm, wants_w) for g, u in by_grade.items()}
        if len(opts) > 1:
            # one grade, chosen by the code: the best we hold, since buyers' kits refuse to pay for low grades
            # (11-04, 6 h of our receipts: unpaid food A 0 of 33, B 29 of 105, C 34 of 58; goods C 6 of 11),
            # unless the buyer asks for a lower one: then the lowest grade it named that we hold ("grade C is fine"
            # gets C, not a choice of B and C; owner 11-04 01:40), or our lowest for "refurbished", "cheapest"
            g = offer_grade(text, opts)
            # no mention to a buyer who named a grade: it said what it wants
            alt = None if NAMED_GRADE.search(text or "") else mention_alt(g, opts)
            opts = {g: opts[g], **({alt: opts[alt]} if alt else {})}
        else:
            g, alt = next(iter(opts)), None
        deal = self.open[task_id] = {"opts": opts, "cur": g, "alt": alt, "arm": arm,
                                     # a clearance price is not the arm's: the bandit must not learn from it
                                     "log_arm": "clearance" if opts[g]["clearance"] else arm,
                                     "steps": int(p["max_concessions"]), "lines": [], "t0": time.time(),
                                     "named": bool(NAMED_GRADE.search(text or "")), "wants_warranty": wants_w,
                                     "pv": p.get("version"), "bv": policy.load_playbook().get("version")}
        for k, o in opts.items():
            u = o["unit"]
            log(f"[task {task_id[:8]}] {k} {u['sku']} {u.get('grade')} cost {u['cost']} list "
                f"{u['list_price']}: ask {o['ask']} (arm {arm}), floor {o['floor']}, warranty {o['warranty']} h")
        await self.turn(task_id, deal, text, up, say, opening=True)

    async def turn(self, task_id, deal, text, up, say, opening=False):
        """One buyer message: the code works out each option's move, haiku picks the unit (when there are two and
        the message could be a reason) and writes the reply, the code commits the moves and closes or counters."""
        opts, cur = deal["opts"], deal["cur"]
        held = self.held(task_id)
        for k in [k for k in opts if k != cur and opts[k]["unit"]["unit"] in held]:
            del opts[k]                      # another negotiation took that unit since
        if not opts:
            del self.open[task_id]
            await up.reject(say("Sorry, the last one of that just sold."))
            return
        deal["lines"].append(f"buyer: {text}")
        ref = opts[cur or next(iter(opts))]
        offer = offer_in(text, ref["unit"].get("name"), ref["ask"])
        # a raise is at least MIN_RAISE of the price under talk: +1 at a time must not walk the ladder to the floor
        moved = offer is not None and offer >= deal.get("best_offer", -1) + max(1, round(MIN_RAISE * ref["ask"]))
        moves = {k: next_move(o, offer, deal["steps"], opening, k == cur and offer is None and accepts(text),
                              deal.get("over_seen", False), moved, deal.get("stalls", 0), deal.get("frozen", False),
                              moved and deal.get("best_offer", -1) >= 0)
                 for k, o in opts.items()}
        history = "\n".join(deal["lines"][-8:])
        how = {}
        fb = deal.pop("fallback", None)          # the C offered at the buyer's price on the last reply (c_fallback)
        if fb and fb in opts:
            # buyers answer with numbers: at 19:40-19:45 three of the first four told of the C sent their number again.
            # A number at or over the C's price takes the C (owner, 11-04 19:50: A sells anyway, sell C when we can),
            # unless it reaches what closes the A or B: that buyer is paying for the better grade
            took = accepts(text) or wants_lower(text, fb) or (
                offer is not None and opts[fb]["ask"] <= offer and not REFUSE.search((text or "").lower())
                and (cur not in opts or offer < close_floor(opts[cur])))     # "No, the original one. 1603432" is a no
            if took and self.swap_if_taken(task_id, deal, fb) is not False:
                if offer is not None and opts[fb]["ask"] < offer <= opts[fb]["unit"]["list_price"]:
                    opts[fb]["ask"] = round(offer)   # 19:58: a buyer sent 1438400 to a C asking 1276000, closed at 1276000
                moves[fb] = {"close": True, "price": opts[fb]["ask"], "step": opts[fb]["step"], "final": True}
                opts = deal["opts"] = {fb: opts[fb]}
                cur = deal["cur"] = fb
                deal["fallback_deal"] = True
                log(f"[task {task_id[:8]}] the buyer took the {fb} unit at {opts[fb]['ask']}")
            else:
                del opts[fb]
                moves.pop(fb, None)
        deal["named"] = deal.get("named") or bool(NAMED_GRADE.search(text or ""))
        alt = deal.get("alt") if deal.get("alt") in opts else None
        if alt:
            # the lower grade only for a buyer who asks for it; anyone else stays on the best grade
            key = alt if not opening and wants_lower(text, alt) else cur   # the first message chose cur already
            if key == alt and self.swap_if_taken(task_id, deal, alt) is False:
                key = cur                    # the old unit was sold meanwhile: stay on the best
            if key == alt or not opening:    # chosen, or not asked for on the first reply: the talk has one unit
                opts = deal["opts"] = {key: opts[key]}
                deal["alt"] = None
                deal["log_arm"] = "clearance" if opts[key]["clearance"] else deal["arm"]
            u, m = opts[key]["unit"], moves[key]
            sit, tmpl = situation(m), template(u, m)
            if key == alt and not m["close"]:      # the first price of the lower unit, not a held one
                sit = (f"the buyer asked for the {LOWER_WORDS[u['grade']]} unit; its asking price is {m['price']}; "
                       "ask for the buyer's offer as a number.")
                tmpl = f"{u['name']}: {facts(u, u.get('_warranty'))}. Send your offer as a number. Asking {m['price']}"
            if opening and key != alt and alt in opts and not m["close"]:
                words = LOWER_WORDS[opts[alt]["unit"]["grade"]]
                sit += (f" Also say, in one short sentence with no price and no number, that the shop has a cheaper "
                        f"{words} unit of the same item if the buyer prefers it.")
                tmpl = tmpl.replace(". Send your offer", f". We also have a cheaper {words} one if you prefer. Send your offer")
            reply = await asyncio.to_thread(write_reply, u, m["price"], sit, history, text, tmpl, how, m["close"])
        elif len(opts) == 1 or (cur and bare(text)):
            key = cur or next(iter(opts))
            u, m = opts[key]["unit"], moves[key]
            reply = await asyncio.to_thread(write_reply, u, m["price"], situation(m), history, text, template(u, m),
                                            how, m["close"])
        else:
            key, reply = await asyncio.to_thread(choose_reply, deal, moves, history, text, how)
        if cur and key != cur:
            log(f"[task {task_id[:8]}] switched {cur} -> {key}")
        swapped = self.swap_if_taken(task_id, deal, key)
        if swapped is False:
            log(f"[task {task_id[:8]}] the unit was sold meanwhile and no other of its grade is left")
            del self.open[task_id]
            await up.reject(say("Sorry, the last one of that just sold."))
            return
        if swapped and moves[key]["price"] < opts[key]["floor"]:
            # the price was worked out on the old unit's floor; the new unit costs more: counter at its floor
            # (the price went up, so the reply must not say "we came down": 5ffb5c63 said it on its first reply)
            moves[key] = {"close": False, "price": opts[key]["floor"], "step": opts[key]["step"], "final": False,
                          "offer": offer, "opening": bool(moves[key].get("opening")), "swapped": True}
            reply = template(opts[key]["unit"], moves[key])
        deal["cur"] = key
        deal["over_seen"] = deal.get("over_seen") or "over" in moves[key]
        if moves[key].get("hold"):
            deal["stalls"] = deal.get("stalls", 0) + (offer is not None)   # a question holds, a number not raised counts
        else:
            deal["stalls"] = 0
        deal["frozen"] = deal.get("frozen") or bool(moves[key].get("hold") and moves[key]["final"])
        if offer is not None and "over" not in moves[key]:   # "2 for 80000" is not a bid for one unit
            deal["best_offer"] = max(offer, deal.get("best_offer", -1))
        for k, o in opts.items():
            o["ask"], o["step"] = moves[k]["price"], moves[k]["step"]
        fbo = None if opening else self.c_fallback(task_id, deal, key, moves[key], offer)
        if fbo:
            cu = fbo["unit"]; fg = cu.get("grade")
            deal["opts"][fg], deal["fallback"] = fbo, fg
            sit = (f"the buyer offered {offer}; the shop cannot go lower on this {opts[key]['unit'].get('grade')} unit and "
                   f"says so politely without naming its price. Then offer, in one or two short sentences, a "
                   f"{LOWER_WORDS[fg]} unit of the same item at the buyer's own price of {fbo['ask']}, and ask the "
                   "buyer to reply yes if they want it.")
            tmpl = (f"{cu['name']}: we cannot go lower on this one. We also have a {LOWER_WORDS[fg]} unit of it: "
                    f"{facts(cu, cu.get('_warranty'))}. It is yours at your offer if you reply yes. Price {fbo['ask']}")
            reply = await asyncio.to_thread(write_reply, cu, fbo["ask"], sit, history, text, tmpl, how, False)
            log(f"[task {task_id[:8]}] offered {fg} {cu['unit']} at {fbo['ask']} (floor {fbo['floor']}, cost {cu['cost']}) "
                f"after a final {moves[key]['price']} on {opts[key]['unit'].get('grade')}")
        deal["lines"].append(f"shop: {reply}")
        u, m = opts[key]["unit"], moves[key]
        append(TURNS, {"ts": time.time(), "task": task_id, "buyer": text[:600], "shop": reply[:600], "unit": key,
                       "grade": u.get("grade"), "sku": u["sku"], "price": m["price"], "close": bool(m["close"]),
                       "template": reply == template(u, m),
                       "tries": how.get("tries"), "failed": how.get("failed"),
                       "c_fallback": fbo["ask"] if fbo else None,
                       "fallback_grade": fbo["unit"].get("grade") if fbo else None,
                       "policy": deal["pv"], "playbook": deal["bv"]})
        if opening:
            append(NEGOTIATIONS, {"ts": time.time(), "task": task_id, "outcome": "open", "sku": u["sku"],
                                  "unit": u["unit"], "kind": u.get("kind"), "grade": u.get("grade"),
                                  "arm": deal.get("log_arm", deal["arm"]), "list_price": u["list_price"], "cost": u["cost"],
                                  "pool_cost": round(u["_pool_cost"]) if u.get("_pool_cost") else None,
                                  "ask": opts[key]["ask"], "floor": opts[key]["floor"], "chose": key,
                                  "options": {k: o["unit"].get("grade") for k, o in opts.items()},
                                  "mentioned": deal.get("alt"), "clearance": bool(opts[key]["clearance"]),
                                  "policy": deal["pv"], "playbook": deal["bv"]})
        if m["close"]:
            await self.close(task_id, deal, m["price"], reply, up, say)
            return
        log(f"[task {task_id[:8]}] {key} {'ask' if opening else 'counter'} {m['price']} "
            f"step {m['step']}/{deal['steps']} (offer {offer})")
        await up.requires_input(say(reply))

    async def close(self, task_id, deal, price, msg, up, say):
        o = deal["opts"][deal["cur"]]
        u = o["unit"]
        price = int(price)
        claim = grade_now(u)
        if claim != u.get("grade"):
            log(f"[task {task_id[:8]}] {u['sku']} dropped to grade {claim} during the talk; the claim says {claim}")
        receipt = {"shop": SHOP, "item": u["sku"], "price": price, "task": task_id, "unit": u["unit"],
                   "claim": claim}
        if o["warranty"]:
            receipt["warranty_hours"] = o["warranty"]
        await up.add_artifact(parts=[new_data_part(sign_receipt(receipt))], name="receipt")
        append(NEGOTIATIONS, {"ts": time.time(), "task": task_id, "outcome": "deal", "sku": u["sku"],
                              "unit": u["unit"], "kind": u.get("kind"), "grade": u.get("grade"), "arm": deal.get("log_arm", deal["arm"]),
                              "list_price": u["list_price"], "cost": u["cost"], "price": price,
                              "step": o["step"], "chose": deal["cur"], "seconds": round(time.time() - deal["t0"], 1),
                              "fallback": bool(deal.get("fallback_deal")), "fallback_offered": bool(deal.get("fb_done"))})
        log(f"[task {task_id[:8]}] DEAL {deal['cur']} {u['sku']} {u.get('grade')} at {price} "
            f"(cost {u['cost']}, step {o['step']})")
        self.sold[u["unit"]] = time.time()
        await up.complete(say(msg))
        del self.open[task_id]

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        deal = self.open.pop(task.id, None)
        if deal:
            o = deal["opts"][deal["cur"] or next(iter(deal["opts"]))]
            append(NEGOTIATIONS, {"ts": time.time(), "task": task.id, "outcome": "cancel",
                                  "kind": o["unit"].get("kind"), "arm": deal.get("log_arm", deal["arm"]), "last_ask": o["ask"],
                                  "fallback_offered": bool(deal.get("fb_done"))})
        await TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id).cancel()


def build_app(port):
    url = f"http://127.0.0.1:{port}/"
    card = AgentCard(
        name=SHOP,
        description="Certificates always shown, grades stated truthfully, warranty on goods. Food, furniture, "
                    "bedding, appliances and more, across the market's categories. Ask for an item by name or SKU, "
                    "then send your offer as a number; signed receipts.",
        version="2.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["application/json", "text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url, protocol_version="1.0")],
        skills=[AgentSkill(
            id="negotiate", name="Negotiate a purchase",
            description="Name the item (SKU or name) to get an asking price and the unit's grade, then reply with "
                        "your offer as a number on the same task. The shop closes with a signed receipt artifact "
                        "{shop, item, price, task, unit, claim, warranty_hours} or counters; it never walks away.",
            tags=["shopping", "negotiation"], examples=["I want to buy 종가 포기김치 10kg (G00001)", "42000"])],
    )
    handler = DefaultRequestHandler(agent_executor=Seller(), task_store=InMemoryTaskStore(), agent_card=card)
    return Starlette(routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9999)
    args = ap.parse_args()
    log(f"{SHOP} on http://127.0.0.1:{args.port}/ (stock from {MARKET_DIR}, replies by {REPLY_MODEL} {REPLY_EFFORT})")

    def watch_rules():
        """rule_watch.py every WATCH_S in a daemon thread (owner, 11-04 23 h): a market change is in data/alerts.md
        within minutes, not at the next hourly review. It reports only; a failure is logged and never stops a talk."""
        import threading
        import rule_watch

        def loop():
            while True:
                try:
                    rule_watch.main()
                except Exception as e:
                    log(f"[rule_watch] {type(e).__name__}: {str(e)[:160]}")
                time.sleep(120)
        threading.Thread(target=loop, name="rule_watch", daemon=True).start()
    watch_rules()
    uvicorn.run(build_app(args.port), host="127.0.0.1", port=args.port, log_level="warning")
