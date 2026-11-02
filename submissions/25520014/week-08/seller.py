"""Week 08 seller: an A2A v1.0 server that sells units from the stock the kit keeps in $MARKET_DIR/inventory.json.

    python seller.py --port 9999

The model speaks, the code decides. haiku (medium effort) writes every reply to the buyer from the facts of the
unit (grade, shelf life, certificate, warranty) and the price the code chose; the code picks the unit, the price,
when to close and what goes on the receipt. Week 06 is why: with the reserve only in the prompt, haiku sold a 260
bookshelf at 230 in 2 of 3 runs. A reply that fails, times out or carries a number other than the price (numbers in
the product's own name aside) is replaced by a template built from the same facts.

Unit: among the units the buyer named (SKU, else name, else category), skipping units held by another open
negotiation, the code keeps two: LOW, the stock to clear (food closest to its date, which is its grade; goods of the
lowest grade) and HIGH, the best unit, when it is really better. Each has its own ask, floor and concession step.
haiku picks which one to offer from the buyer's words: LOW, said plainly and cheaper, to a buyer who does not mind;
HIGH to a buyer who asks for freshness or grade, objects to LOW, or looks like a fixed script (it may refuse LOW at
checkout). A bare number or yes keeps the current unit. When haiku fails the shop offers HIGH.

Price:
  opening ask  = list price x the arm Thompson sampling picks (0.7/0.8/0.9/1.0, per kind, model.py), cut by
                 c_discount for grade C, never under the floor
  floor        = cost x (1 + min_margin) + warranty premium (break rate of the grade x price), then lower in the
                 last `liquidate_hours` (down to the cost) and in the last 6 hours (down to half the cost), and for
                 food close to its date (half the cost: it is thrown away at cost otherwise)
  an offer at or above the current ask closes at the offer. Below it the shop counters: each counter moves a
  1 / (counters left) share of the way from the ask down to max(floor, offer); after `max_concessions` counters
  the floor is the final price, repeated. An offer at or above the floor after that closes. The shop never walks.
Receipt: {shop, item (SKU), price, task, unit, claim (the true grade)}, warranty_hours on goods when the policy
offers it for that grade. The certificate is always shown (no cert: false). Claims are never false.

Every negotiation is one line in data/negotiations.jsonl when it opens and one when it closes; the price bandit
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


def offer_in(text, name):
    """The buyer's number: the last number in the message that is not part of the product's name or a SKU."""
    clean = SKU_RE.sub(" ", text)
    for n in re.findall(r"\d[\d,]*", name or ""):
        clean = clean.replace(n, " ")
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", clean) if n.replace(",", "").isdigit()]
    return nums[-1] if nums else None


def inventory():
    try:
        return json.loads((MARKET_DIR / "inventory.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


RANK = {"C": 0, "B": 1, "A": 2}


def find_units(text, taken):
    """The two units the clerk can offer: LOW (the stock to clear: food closest to its date, goods of the lowest
    grade) and HIGH (the best unit), or HIGH = None when it is not really better than LOW."""
    inv = inventory()
    if inv.get("round", 0) < 3:
        return None, None, inv
    want = text.lower()
    units = [u for u in inv.get("units", []) if u.get("grade") != "expired" and u["unit"] not in taken]
    named = [u for u in units if u["sku"].lower() in want]
    if not named:
        named = [u for u in units if (u.get("name") or "").lower() in want]
    if not named:
        named = [u for u in units if (u.get("category") or "").split(">")[-1].lower() in want]
    if not named:
        return None, None, inv
    low = min(named, key=lambda u: (u.get("expires_at") or float("inf"), RANK.get(u.get("grade"), 3),
                                    u.get("supplied_at") or 0))
    high = max(named, key=lambda u: (RANK.get(u.get("grade"), -1), u.get("expires_at") or 0,
                                     -(u.get("supplied_at") or 0)))
    better = RANK.get(high.get("grade"), -1) > RANK.get(low.get("grade"), -1) or (
        high.get("expires_at") and low.get("expires_at") and high["expires_at"] - low["expires_at"] > 24 * 3600)
    return low, (high if better else None), inv


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


FINAL_WORDS = re.compile(r"\b(best|lowest|final|last)\b|최선|최저|마지막|최종", re.I)


def check_reply(reply, u, price, final, t0, buyer_text=""):
    """The code's guard on a reply: the price last, no digits but the price, the product name's and the buyer's own,
    no 'final' before the end."""
    allowed = {n.replace(",", "") for n in re.findall(r"\d[\d,]*", (u.get("name") or "") + " " + buyer_text)}
    nums = [n.replace(",", "") for n in re.findall(r"\d[\d,]*", reply)]
    others = [n for n in nums if n != str(price) and n not in allowed]
    if not reply or str(price) not in nums or others or nums[-1] != str(price):
        log(f"reply: {time.time() - t0:.1f}s rejected (numbers {nums}): {reply[:160]!r}")
        return False
    if not final and FINAL_WORDS.search(reply):      # haiku says "best I can do" on the first counter anyway
        log(f"reply: {time.time() - t0:.1f}s rejected (calls a non-final price final): {reply[:160]!r}")
        return False
    return True


def write_reply(u, price, situation, history, buyer_text, template, final=True):
    prompt = (
        f"You are the sales clerk of {SHOP}, an online shop. A buyer is negotiating for one unit of "
        f"{u['name']!r}.\n"
        f"Facts about this unit (the only things you may state about it): {facts(u, u.get('_warranty'))}.\n"
        f"What the shop does now: {situation}\n"
        f"Conversation so far:\n{history}\n"
        f"Buyer's last message: {buyer_text!r}\n\n"
        "Write the shop's reply in at most three short sentences, in the buyer's language (English when it is "
        "unclear). Answer the buyer's "
        "questions using only the facts above; if they ask for something not in the facts (delivery dates, "
        "returns, freebies, a manufacture date), say plainly that the shop cannot promise it. Never invent facts. "
        f"The price is {price}: write it exactly once, as digits, as the last thing in the message. Write no "
        "other digits (spell out any other number). Reply with the message text only.")
    t0 = time.time()
    try:
        reply = harness.call(prompt, timeout=REPLY_TIMEOUT, model=REPLY_MODEL, effort=REPLY_EFFORT).strip()
    except Exception as e:
        log(f"reply: harness failed after {time.time() - t0:.1f}s ({type(e).__name__}), template")
        return template
    if not check_reply(reply, u, price, final, t0, buyer_text):
        return template
    log(f"reply: {time.time() - t0:.1f}s {reply[:240]!r}")
    return reply


CHOOSE = """You are the sales clerk of {shop}, an online shop. A buyer wants one unit of what they asked for, and the
shop has two units it could sell. The code has already set the price of each for this turn:

LOW  ({low_name}): {low_facts}. If you offer LOW: {low_move}
HIGH ({high_name}): {high_facts}. If you offer HIGH: {high_move}

The shop would rather sell LOW (older stock, cheaper), but only to a buyer who does not mind it:
- the buyer asks for freshness, a long shelf life, a good grade or a new item, or objects to LOW: offer HIGH;
- the buyer has said nothing about condition and writes like a person or a language model (free text, questions):
  offer LOW, say plainly what it is and that it is the cheaper one; you may say a better unit costs more, without
  its number;
- the buyer looks like a fixed script (template wording, bare numbers) and has said nothing about condition: offer
  HIGH, since a script may refuse LOW at checkout;
- the shop currently offers {current}: keep it unless the buyer's words give a reason to switch.

Conversation so far:
{history}
Buyer's last message: {buyer!r}

Answer with one JSON object only: {{"unit": "LOW" or "HIGH", "reply": "<at most three short sentences in the
buyer's language (English when it is unclear), stating only facts of that unit; if they ask for something not in the facts, say the shop cannot
promise it; the price of that unit exactly once, as digits, as the last thing; no other digits>"}}"""


def choose_reply(deal, moves, history, buyer_text):
    """haiku picks LOW or HIGH from what the buyer said and writes the reply; the code checks it like any reply."""
    o = deal["opts"]
    fallback = deal["cur"] or "high"         # unsure: the better unit (a buyer who minds would rate LOW badly)
    prompt = CHOOSE.format(
        shop=SHOP, current=(deal["cur"] or "nothing yet").upper(), history=history, buyer=buyer_text,
        **{f"{k}_{f}": v for k in ("low", "high") for f, v in (
            ("name", o[k]["unit"]["name"]), ("facts", facts(o[k]["unit"], o[k]["warranty"])),
            ("move", situation(moves[k])))})
    t0 = time.time()
    try:
        answer = harness.call(prompt, timeout=REPLY_TIMEOUT, model=REPLY_MODEL, effort=REPLY_EFFORT)
        a = json.loads(re.search(r"\{.*\}", answer, re.S).group())
        key, reply = str(a["unit"]).lower(), str(a["reply"]).strip()
    except Exception as e:
        log(f"choose: failed after {time.time() - t0:.1f}s ({type(e).__name__}), {fallback.upper()} by template")
        return fallback, template(o[fallback]["unit"], moves[fallback])
    if key not in o:
        key = fallback
    m = moves[key]
    if not check_reply(reply, o[key]["unit"], m["price"], m["final"] or m["close"], t0, buyer_text):
        return key, template(o[key]["unit"], moves[key])
    log(f"choose: {time.time() - t0:.1f}s {key.upper()} {reply[:240]!r}")
    return key, reply


def option(u, p, arm):
    """One unit the shop can offer, with its own asking price, floor and concession step."""
    kind = u.get("kind", "goods")
    warranty = int(p["warranty_hours"]) if kind == "goods" and p.get(f"warranty_{u.get('grade')}") else 0
    u = dict(u, _warranty=warranty)
    ask = u["list_price"] * arm * ((1 - p["c_discount"]) if u.get("grade") == "C" else 1)
    floor = floor_price(u, p, warranty, ask)
    return {"unit": u, "ask": max(round(ask), floor), "floor": floor, "step": 0, "warranty": warranty}


def next_move(o, offer, steps, opening, accepted):
    """What the shop does with option o on this buyer message, without changing o: close at a price, or say a price
    (the opening ask, a concession, or the floor as the final price)."""
    if opening and offer is None:
        return {"close": False, "price": o["ask"], "step": 0, "final": False, "opening": True}
    if offer is None and accepted:
        return {"close": True, "price": o["ask"], "step": o["step"], "final": True}
    if offer is not None and (offer >= o["ask"] or (o["step"] >= steps and offer >= o["floor"])):
        return {"close": True, "price": offer, "step": o["step"], "final": True}
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
    if m["close"]:
        return f"the deal closes at {p} and the signed receipt is attached; thank the buyer briefly."
    if m.get("opening"):
        return f"the asking price is {p}; ask for the buyer's offer as a number."
    said = f"the buyer offered {m['offer']}; " if m.get("offer") is not None else ""
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
        """Units promised in open negotiations: the unit each one offers now, or both before it has chosen."""
        return {o["unit"]["unit"] for t, d in self.open.items() if t != but
                for k, o in d["opts"].items() if d["cur"] in (None, k)}

    async def opening(self, task_id, text, up, say):
        low, high, inv = find_units(text, self.held())
        if low is None:
            append(MISSED, {"ts": time.time(), "text": text[:300], "round": inv.get("round")})
            log(f"[task {task_id[:8]}] nothing in stock for this request")
            await up.reject(say("Sorry, we have none of that in stock right now."))
            return
        p = policy.load()
        refresh_break_rates()
        arm = model.pick_arm(low.get("kind", "goods"))
        opts = {"low": option(low, p, arm)}
        if high is not None:
            opts["high"] = option(high, p, arm)
        deal = self.open[task_id] = {"opts": opts, "cur": None if high else "low", "arm": arm,
                                     "steps": int(p["max_concessions"]), "lines": [], "t0": time.time()}
        for k, o in opts.items():
            u = o["unit"]
            log(f"[task {task_id[:8]}] {k.upper()} {u['sku']} {u.get('grade')} cost {u['cost']} list "
                f"{u['list_price']}: ask {o['ask']} (arm {arm}), floor {o['floor']}, warranty {o['warranty']} h")
        await self.turn(task_id, deal, text, up, say, opening=True)

    async def turn(self, task_id, deal, text, up, say, opening=False):
        """One buyer message: the code works out each option's move, haiku picks the unit (when there are two and
        the message could be a reason) and writes the reply, the code commits the moves and closes or counters."""
        opts, cur = deal["opts"], deal["cur"]
        held = self.held(task_id)
        for k in [k for k in opts if k != cur and opts[k]["unit"]["unit"] in held]:
            del opts[k]                      # another negotiation took that unit since
        deal["lines"].append(f"buyer: {text}")
        offer = offer_in(text, opts[cur or "low"]["unit"].get("name"))
        moves = {k: next_move(o, offer, deal["steps"], opening, k == cur and offer is None and accepts(text))
                 for k, o in opts.items()}
        history = "\n".join(deal["lines"][-8:])
        if len(opts) == 1 or (cur and bare(text)):
            key = cur or "low"
            u, m = opts[key]["unit"], moves[key]
            reply = await asyncio.to_thread(write_reply, u, m["price"], situation(m), history, text,
                                            template(u, m), m["final"] or m["close"])
        else:
            key, reply = await asyncio.to_thread(choose_reply, deal, moves, history, text)
        if cur and key != cur:
            log(f"[task {task_id[:8]}] switched {cur.upper()} -> {key.upper()}")
        deal["cur"] = key
        for k, o in opts.items():
            o["ask"], o["step"] = moves[k]["price"], moves[k]["step"]
        deal["lines"].append(f"shop: {reply}")
        u, m = opts[key]["unit"], moves[key]
        if opening:
            append(NEGOTIATIONS, {"ts": time.time(), "task": task_id, "outcome": "open", "sku": u["sku"],
                                  "unit": u["unit"], "kind": u.get("kind"), "grade": u.get("grade"),
                                  "arm": deal["arm"], "list_price": u["list_price"], "cost": u["cost"],
                                  "ask": opts[key]["ask"], "floor": opts[key]["floor"], "chose": key,
                                  "options": {k: o["unit"].get("grade") for k, o in opts.items()}})
        if m["close"]:
            await self.close(task_id, deal, m["price"], reply, up, say)
            return
        log(f"[task {task_id[:8]}] {key.upper()} {'ask' if opening else 'counter'} {m['price']} "
            f"step {m['step']}/{deal['steps']} (offer {offer})")
        await up.requires_input(say(reply))

    async def close(self, task_id, deal, price, msg, up, say):
        o = deal["opts"][deal["cur"]]
        u = o["unit"]
        price = int(price)
        receipt = {"shop": SHOP, "item": u["sku"], "price": price, "task": task_id, "unit": u["unit"],
                   "claim": u.get("grade")}
        if o["warranty"]:
            receipt["warranty_hours"] = o["warranty"]
        await up.add_artifact(parts=[new_data_part(sign_receipt(receipt))], name="receipt")
        append(NEGOTIATIONS, {"ts": time.time(), "task": task_id, "outcome": "deal", "sku": u["sku"],
                              "unit": u["unit"], "kind": u.get("kind"), "grade": u.get("grade"), "arm": deal["arm"],
                              "list_price": u["list_price"], "cost": u["cost"], "price": price,
                              "step": o["step"], "chose": deal["cur"], "seconds": round(time.time() - deal["t0"], 1)})
        log(f"[task {task_id[:8]}] DEAL {deal['cur'].upper()} {u['sku']} {u.get('grade')} at {price} "
            f"(cost {u['cost']}, step {o['step']})")
        await up.complete(say(msg))
        del self.open[task_id]

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        deal = self.open.pop(task.id, None)
        if deal:
            o = deal["opts"][deal["cur"] or "low"]
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
