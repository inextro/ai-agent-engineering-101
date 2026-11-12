"""Runs market_buyer.main() against fake shops (no network, no model) to check the patch 3.6 rules: the cap per
grade (the price where the expected share of the shop's margin kept is still min_share), the haggle going where the
most margin is kept, no offer under the least the unit can have cost, and the grades the accept policy names.

    DATA_DIR=<scratch>/data MARKET_DIR=<scratch>/.market BUYER_LLM=0 python tests/buyer_share.py <scratch>

A fake shop names a grade and an ask, comes down by a step to its floor, and closes when our offer reaches the floor.
List price 20000, min_share 0.75 (11-13; 0.70 before): caps A 14894, B 12601, C 10327.
"""
import io
import json
import re
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

out = Path(sys.argv[1])
(out / "data").mkdir(parents=True, exist_ok=True)
(out / ".market").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SKU, LIST = "Gtest0001", 20000
cat = {SKU: {"name": "test milk", "category": "식품>우유", "list_price": LIST, "kind": "food", "shelf_hours": 200}}
(out / ".market" / "catalog.json").write_text(json.dumps(cat))
order = {"order": "o-test", "item": "우유", "category": "식품>우유", "budget": 30000, "acceptable": [SKU],
         "use_within_hours": 48, "kind": "food"}
(out / ".market" / "order.json").write_text(json.dumps(order))

import buyer
import market_buyer as mb

SHOPS, sent = {}, []


def task(state, text, tid, data=None):
    t = {"id": tid, "status": {"state": state, "message": {"parts": [{"text": text}]}}}
    if data:
        t["artifacts"] = [{"parts": [{"data": data}]}]
    return t


def send(url, text, task_id=None):
    name = url.split("//")[1]
    s = SHOPS[name]
    sent.append((name, text))
    nums = [int(n) for n in re.findall(r"\d{4,}", text)]
    if task_id is None:                                   # the opening
        s["now"] = s["ask"]
    elif nums and nums[-1] >= s["floor"]:
        return task("TASK_STATE_COMPLETED", f"Deal at {nums[-1]}.", name,
                    {"price": nums[-1], "item": SKU, "claim": s.get("claim", s["grade"]), "unit": "u-0123456789ab"})
    else:
        s["now"] = max(s["floor"], s["now"] - s["step"])
    grade = f"Grade {s['grade']} unit, 150 h of shelf life left. " if s["grade"] else "150 h of shelf life left. "
    return task("TASK_STATE_INPUT_REQUIRED", f"{grade}The price is {s['now']}.", name)


buyer.find_endpoint = lambda base: (None, base)
buyer.send = send
buyer.walk_away = lambda url, tid, why: sent.append((url.split("//")[1], f"walk away: {why}"))
sys.argv = ["market_buyer.py", "--shops", str(out / ".market/shops.json"), "--order", str(out / ".market/order.json")]


def run(shops):
    SHOPS.clear()
    SHOPS.update(shops)
    (out / ".market" / "shops.json").write_text(json.dumps(
        [{"name": n, "url": f"http://{n}", "offers": [{"sku": SKU, "qty": 1}]} for n in shops]))
    sent.clear()
    mb.DONE.clear()
    mb.STARTED = time.time()
    buf = io.StringIO()
    with redirect_stdout(buf):
        mb.main()
    return mb.past_orders()[-1], json.loads(buf.getvalue().strip().splitlines()[-1])


def check(label, ok, rec, result):
    print(("ok " if ok else "BAD"), label, "| end=%r shop=%s price=%s cap=%s offers=%s accept=%s" % (
        rec.get("end"), rec.get("shop"), rec.get("price"), rec.get("cap"), rec.get("offers"), result.get("accept")))
    return ok


good = True
# 1. an A unit at 0.74 of the list: over the old cap (0.68 of the list), now a deal, and the accept names A alone
rec, res = run({"a": {"grade": "A", "ask": 16000, "floor": 14800, "step": 400}})
good &= check("A at 14800 closes", rec["end"] == "deal" and rec["price"] <= 14894 and res["accept"].get("grades") == ["A"]
              and min(rec["offers"]) >= 11000, rec, res)
# 2. a C unit that stops at 0.60 of the list: under the old cap, now over C's (0.516)
rec, res = run({"c": {"grade": "C", "ask": 13200, "floor": 12000, "step": 400}})
good &= check("C at 12000 is refused", rec["end"].startswith("ask ") and not res.get("receipt") and rec["cap"] == 10327, rec, res)
# 3. a C asking 0.56 and an A asking 0.74: the A keeps more of the margin, so the haggle starts there
rec, res = run({"c": {"grade": "C", "ask": 11200, "floor": 11000, "step": 100},
                "a": {"grade": "A", "ask": 14800, "floor": 14000, "step": 400}})
good &= check("A before a cheaper C", rec["end"] == "deal" and rec["shop"] == "a" and rec["tried"] == 1, rec, res)
# 4. a B asking under the least a B can cost (0.40 of the list): taken at once, no counter-offer
rec, res = run({"b": {"grade": "B", "ask": 7900, "floor": 7000, "step": 300}})
good &= check("B under its least cost is taken at once", rec["end"] == "deal" and rec["price"] == 7900 and rec["offers"] == [7900], rec, res)
# 5. the shop says B, the receipt claims C at a price only A and B are worth
rec, res = run({"liar": {"grade": "B", "claim": "C", "ask": 13000, "floor": 12500, "step": 200}})
good &= check("receipt claiming C at a B price is refused", not res.get("receipt") and "claims C" in str(rec.get("doomed")), rec, res)
# 6. no grade in the message: B's cap, and the accept leaves C out at that price (the kit checks the certificate)
rec, res = run({"mute": {"grade": None, "claim": None, "ask": 13400, "floor": 12400, "step": 400}})
good &= check("no grade named: B's cap, accept A or B", rec["end"] == "deal" and rec["cap"] == 12601 and res["accept"].get("grades") == ["A", "B"], rec, res)
print("ALL OK" if good else "FAILED")
