"""Checks seller.fair_lift (11-13): for the buyer whose first message is FAIR_BUYER the ask is at least FAIR_ASK x
list and its bid, min(0.90 x ask, 0.70 x list), closes when it is at or over the floor. No network, no model.

    DATA_DIR=<scratch>/data MARKET_DIR=<scratch>/.market python tests/fair_buyer.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seller

STEPS = 4
good = True


def talk(ask, floor, lp=1000000, cost=600000, fair=True):
    o = {"unit": {"list_price": lp, "cost": cost}, "ask": ask, "floor": floor, "step": 0}
    if fair:
        seller.fair_lift(o, STEPS)
    first = seller.next_move(o, None, STEPS, True, False)
    bid = round(min(0.90 * first["price"], 0.70 * lp))
    return first, bid, seller.next_move(o, bid, STEPS, False, False)


for label, args, want in (
        ("A at the market's price: asked 0.78, the 0.70 bid closes", dict(ask=728000, floor=665000), (780000, 700000, True, 700000)),
        ("without the rule: the 0.655 bid is countered", dict(ask=728000, floor=665000, fair=False), (728000, 655200, False, None)),
        ("ask already over 0.78: kept, the bid closes", dict(ask=900000, floor=665000), (900000, 700000, True, 700000)),
        ("a dear A, floor 0.74: the bid does not close, the floor is the final price", dict(ask=745000, floor=740000, cost=735000), (780000, 700000, False, 740000)),
        ("a cheap item: ask rounds up, never over the list", dict(ask=700, floor=600, lp=1000, cost=500), (780, 700, True, 700))):
    first, bid, m = talk(**args)
    got = (first["price"], bid, m["close"], m["price"] if m["close"] or args.get("fair", True) else None)
    ok = got == want and (not first["close"])
    print("ok " if ok else "BAD", label, "| first ask, bid, closed, price:", got, "want", want)
    good &= ok
ok = bool(seller.FAIR_BUYER.search("I want to buy G5b96e0fd. Please quote a price in credits.")) and not seller.FAIR_BUYER.search(
    "I want to buy G2c023a8c. Please quote this exact SKU, certificate, grade and warranty hours.")
print("ok " if ok else "BAD", "the first message is recognised, another buyer's is not")
good &= ok
print("ALL OK" if good else "FAILED")
