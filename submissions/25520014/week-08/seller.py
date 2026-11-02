"""Week 08 seller: an A2A v1.0 server that sells units from the stock the kit keeps in $MARKET_DIR/inventory.json.

    python seller.py --port 9999

The model speaks, the code decides. haiku (medium effort) writes every reply to the buyer from the facts of the
unit (grade, shelf life, certificate, warranty) and the price the code chose; the code picks the unit, the price,
when to close and what goes on the receipt. Week 06 is why: with the reserve only in the prompt, haiku sold a 260
bookshelf at 230 in 2 of 3 runs. A reply without the price as its last number, or with a number other than the
price (numbers in the product's own name and the buyer's aside), goes back to haiku once with the reason; a second
failure, or a call that fails or times out, is replaced by a template built from the same facts. haiku never sees the cost, the floor or the policy, so no reply
can give them away (the final price is the floor, and it is said only as the final price).

Unit: among the units the buyer named (SKU, else name, else category), skipping units held by another open
negotiation, the code keeps one unit per grade (C, B, A; for food the grade is its shelf life left), the one to
clear first within it (food closest to its date, goods held longest). Each has its own ask, floor and concession
step. haiku picks which to offer from the buyer's words: the lowest grade that meets what the buyer asked for, said
plainly and cheaper, to a buyer who minds nothing; the best grade to a buyer who looks like a fixed script (it may
refuse a low grade at checkout). A bare number or yes keeps the current unit. When haiku fails the shop offers the
best grade.

Price:
  opening ask  = list price x the arm Thompson sampling picks (0.7/0.8/0.9/1.0, per kind, model.py), cut by
                 c_discount for grade C, never under the floor
  floor        = cost x (1 + min_margin) + warranty premium (break rate of the grade x price), then lower in the
                 last `liquidate_hours` (down to the cost) and in the last 6 hours (down to half the cost), and for
                 food close to its date (half the cost: it is thrown away at cost otherwise)
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
    "goods": {"A": "grade A, new and in perfect condition", "B": "grade B, the box is damaged, the item is fine",
              "C": "grade C, refurbished"},
}
NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
ACCEPT = re.compile(r"\b(accept|accepted|agree|agreed|deal|ok|okay|yes|buy it|take it)\b|좋아|좋습니다|살게|구매할게|확정")
REFUSE = re.compile(r"\b(no|not|don't|dont|won't|cannot|can't|never|reject|walk)\b|안 |못 |싫")
SKU_RE = re.compile(r"\bG[0-9a-f]{8}\b")


def log(msg):
    print(f"[seller] {msg}", flush=True)


def append(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def accepts(text):
    t = text.lower()
    return bool(ACCEPT.search(t)) and not REFUSE.search(t)


UNIT_AFTER = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:hours?\b|hrs?\b|h\b|시간|days?\b|일\b|kg\b|g\b|ml\b|l\b|개|units?\b|"
                        r"pcs\b|%|percent|퍼센트|stars?\b|점\b)", re.I)


def offer_in(text, name, ref=None):
    """The buyer's number: the last amount in the message that is not part of the product's name, a SKU, a quantity
    with a unit ("24 hours", "2 units", "10kg") or, next to the price under talk, too small to be a price (under a
    tenth of `ref`: "2 for 50000"). "3만 5천", "35천" and "35k" read as 35000."""
    clean = SKU_RE.sub(" ", text)
    for n in re.findall(r"\d[\d,]*", name or ""):   # whole numbers only: "900g" in the name must not eat "9000"
        clean = re.sub(rf"(?<![\d,]){re.escape(n)}(?![\d,])", " ", clean)
    clean = re.sub(r"(\d+(?:\.\d+)?)\s*만(?:\s*(\d+)\s*천)?",
                   lambda m: f" {round(float(m.group(1)) * 10000 + int(m.group(2) or 0) * 1000)} ", clean)
    clean = re.sub(r"(\d+(?:\.\d+)?)\s*(?:천|k\b)", lambda m: f" {round(float(m.group(1)) * 1000)} ", clean, flags=re.I)
    clean = UNIT_AFTER.sub(" ", clean)
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", clean) if n.replace(",", "").isdigit()]
    if ref:
        nums = [n for n in nums if n >= 0.1 * ref]
    return nums[-1] if nums else None


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
SOLD_HOLD = 15 * 60   # a unit sold stays out of offers this long (inventory.json drops it at the checkout)
ABANDONED = 10 * 60   # a talk this old is over (the kit stops a buyer at 300 s, often without a CancelTask)


def find_units(text, taken):
    """One unit per grade of what the buyer named, the one to clear first within the grade (food closest to its
    date, goods held longest): {grade: unit}, empty when there is none."""
    inv = inventory()
    if inv.get("round", 0) < 3:
        return {}, inv
    want = text.lower()
    units = [u for u in inv.get("units", []) if u.get("grade") in RANK and u["unit"] not in taken]
    named = [u for u in units if u["sku"].lower() in want]
    if not named:
        named = [u for u in units if (u.get("name") or "").lower() in want]
    if not named:
        named = [u for u in units if (u.get("category") or "").split(">")[-1].lower() in want]
    by_grade = {}
    for u in sorted(named, key=lambda u: (u.get("expires_at") or 0, u.get("supplied_at") or 0)):
        by_grade.setdefault(u["grade"], u)
    return by_grade, inv


def facts(u, warranty):
    """What the shop may say about the unit, in words (the price is the only digits the buyer should parse)."""
    kind = u.get("kind", "goods")
    parts = [GRADE_WORDS[kind].get(u.get("grade"), f"grade {u.get('grade')}"),
             "the market's certificate of its grade is shown at checkout"]
    if kind == "food" and u.get("expires_at"):
        hours = (u["expires_at"] - time.time()) / 3600
        parts.append("it keeps for more than a week" if hours > 168 else "it keeps for several days" if hours > 48
                     else "it keeps for about a day or two" if hours > 18 else "it must be used within hours")
    if warranty:
        parts.append(f"a {NUMBER_WORDS.get(warranty, warranty)}-hour warranty: if it breaks in that time the market "
                     "refunds the full price")
    elif kind == "goods":
        parts.append("no warranty")
    return "; ".join(parts)


def check_reply(reply, u, price, buyer_text):
    """The code's check on a reply, None when it passes, else what is wrong (haiku hears it on its one retry): the
    price as digits, last, and no digits but the price, the product name's and the buyer's own (made-up numbers).
    Calling a price final is left to the prompt: a word check rejected good replies ("it should last", "not final")."""
    allowed = {n.replace(",", "") for n in re.findall(r"\d[\d,]*", (u.get("name") or "") + " " + buyer_text)}
    nums = [n.replace(",", "") for n in re.findall(r"\d[\d,]*", reply)]
    others = [n for n in nums if n != str(price) and n not in allowed]
    if not reply:
        return "the reply was empty"
    if not nums or nums[-1] != str(price):
        return f"the price {price} must be written as digits as the very last thing in the message"
    if others:
        return f"it has numbers other than the price ({', '.join(others)}); drop them or spell them out"
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
        "other digits (spell out any other number). Reply with the message text only.")
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
or lowest; the price of that unit exactly once, as digits, as the last thing; no other digits>"}}"""


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


def option(u, p, arm):
    """One unit the shop can offer, with its own asking price, floor and concession step."""
    kind = u.get("kind", "goods")
    warranty = int(p["warranty_hours"]) if kind == "goods" and p.get(f"warranty_{u.get('grade')}") else 0
    u = dict(u, _warranty=warranty)
    ask = u["list_price"] * arm * ((1 - p["c_discount"]) if u.get("grade") == "C" else 1)
    floor = floor_price(u, p, warranty, ask)
    return {"unit": u, "ask": max(round(ask), floor), "floor": floor, "step": 0, "warranty": warranty}


STALLS_FINAL = 3   # this many messages in a row without a higher offer and the current price becomes final


def next_move(o, offer, steps, opening, accepted, over_seen=False, moved=True, stalls=0, frozen=False):
    """What the shop does with option o on this buyer message, without changing o: close at a price, or say a price
    (the opening ask, a concession, the floor as the final price, the ask again when the offer is above it, or the
    same price when the buyer did not move). The shop concedes only when the buyer raised its offer: a message with
    no number or with the same or a lower number holds the price, and after STALLS_FINAL numbers in a row that did
    not rise the held price is final and stays so (frozen: a price called final never moves again). A lowball
    repeated never reaches the floor; a buyer who keeps raising walks the ladder."""
    if offer is not None and offer > o["ask"] and not over_seen:
        # above the ask is a misreading ("2 for 50000"): say the price of one unit; the same again closes at the ask
        return {"close": False, "price": o["ask"], "step": o["step"], "final": o["step"] >= steps,
                "opening": opening, "over": offer}
    if opening and offer is None:
        return {"close": False, "price": o["ask"], "step": 0, "final": False, "opening": True}
    if offer is None and accepted:
        return {"close": True, "price": o["ask"], "step": o["step"], "final": True}
    if offer is not None and (offer >= o["ask"] or (o["step"] >= steps and offer >= o["floor"])):
        # never above the ask: an offer over it is a misreading ("2 for 50000" read as 50000 for one)
        return {"close": True, "price": min(offer, o["ask"]), "step": o["step"], "final": True}
    if not opening and (not moved or frozen):
        return {"close": False, "price": o["ask"], "step": o["step"], "offer": offer, "hold": True,
                "final": frozen or o["step"] >= steps or (offer is not None and stalls + 1 >= STALLS_FINAL)}
    left = steps - o["step"]
    if left <= 0:
        ask, step = o["floor"], steps
    else:
        ask = max(o["floor"], round(o["ask"] - (o["ask"] - max(o["floor"], offer or 0)) / left))
        step = o["step"] + 1
        if step >= steps or ask == o["floor"]:
            ask, step = o["floor"], steps
    if offer is not None and offer >= ask:
        return {"close": True, "price": offer, "step": step, "final": True}
    return {"close": False, "price": ask, "step": step, "final": step >= steps, "offer": offer}


def situation(m):
    p = m["price"]
    if "over" in m:
        return (f"the buyer's number ({m['over']}) is above the price; it may be for several units or a mistake. The "
                f"shop sells one unit at {p}; ask them to confirm.")
    if m["close"]:
        return f"the deal closes at {p} and the signed receipt is attached; thank the buyer briefly."
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
    return f"{u['name']}: {'final price' if m['final'] else 'we can come down'}. Asking {p}"


def bare(text):
    """Only a number or a plain yes: nothing in it that could be a reason to switch units."""
    rest = ACCEPT.sub(" ", re.sub(r"\d[\d,.]*", " ", SKU_RE.sub(" ", text.lower())))
    return len(re.sub(r"[\W_]|won|krw|원", "", rest)) <= 3


def floor_price(u, p, warranty, est_price):
    cost = u["cost"]
    premium = BREAK.get(u.get("grade"), 0.1) * est_price if warranty else 0
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


BREAK = {"A": 0.1, "B": 0.1, "C": 0.1}   # refreshed from model.py at most every 10 minutes
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
                                  "kind": o["unit"].get("kind"), "arm": d["arm"], "last_ask": o["ask"]})
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
        new = option(alt, policy.load(), deal["arm"])
        o["unit"], o["floor"] = new["unit"], max(o["floor"], new["floor"])
        log(f"[task {task_id[:8]}] unit {u['unit']} was taken meanwhile; offering {alt['unit']}")
        return True

    async def opening(self, task_id, text, up, say):
        by_grade, inv = find_units(text, self.held())
        if not by_grade:
            append(MISSED, {"ts": time.time(), "text": text[:300], "round": inv.get("round")})
            log(f"[task {task_id[:8]}] nothing in stock for this request")
            await up.reject(say("Sorry, we have none of that in stock right now."))
            return
        p = policy.load()
        await asyncio.to_thread(refresh_break_rates)       # both read whole files: off the event loop
        arm = await asyncio.to_thread(model.pick_arm, next(iter(by_grade.values())).get("kind", "goods"))
        opts = {g: option(u, p, arm) for g, u in by_grade.items()}
        deal = self.open[task_id] = {"opts": opts, "cur": next(iter(opts)) if len(opts) == 1 else None, "arm": arm,
                                     "steps": int(p["max_concessions"]), "lines": [], "t0": time.time(),
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
        moved = offer is not None and offer > deal.get("best_offer", -1)
        moves = {k: next_move(o, offer, deal["steps"], opening, k == cur and offer is None and accepts(text),
                              deal.get("over_seen", False), moved, deal.get("stalls", 0), deal.get("frozen", False))
                 for k, o in opts.items()}
        history = "\n".join(deal["lines"][-8:])
        how = {}
        if len(opts) == 1 or (cur and bare(text)):
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
            moves[key] = {"close": False, "price": opts[key]["floor"], "step": opts[key]["step"], "final": False,
                          "offer": offer}
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
        deal["lines"].append(f"shop: {reply}")
        u, m = opts[key]["unit"], moves[key]
        append(TURNS, {"ts": time.time(), "task": task_id, "buyer": text[:600], "shop": reply[:600], "unit": key,
                       "grade": u.get("grade"), "sku": u["sku"], "price": m["price"], "close": bool(m["close"]),
                       "template": reply == template(u, m),
                       "tries": how.get("tries"), "failed": how.get("failed"),
                       "policy": deal["pv"], "playbook": deal["bv"]})
        if opening:
            append(NEGOTIATIONS, {"ts": time.time(), "task": task_id, "outcome": "open", "sku": u["sku"],
                                  "unit": u["unit"], "kind": u.get("kind"), "grade": u.get("grade"),
                                  "arm": deal["arm"], "list_price": u["list_price"], "cost": u["cost"],
                                  "ask": opts[key]["ask"], "floor": opts[key]["floor"], "chose": key,
                                  "options": {k: o["unit"].get("grade") for k, o in opts.items()},
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
                              "unit": u["unit"], "kind": u.get("kind"), "grade": u.get("grade"), "arm": deal["arm"],
                              "list_price": u["list_price"], "cost": u["cost"], "price": price,
                              "step": o["step"], "chose": deal["cur"], "seconds": round(time.time() - deal["t0"], 1)})
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
                                  "kind": o["unit"].get("kind"), "arm": deal["arm"], "last_ask": o["ask"]})
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
    uvicorn.run(build_app(args.port), host="127.0.0.1", port=args.port, log_level="warning")
