"""Week 08 review hook: stars for a paid deal, from the market's receipt only.

    python review.py --deal .market/deal.json

The kit runs it after each settled deal and its stars replace market_buyer's. The stars come from facts the market
signed (deal.json `settled`), never from anything the shop wrote:
    the shop claimed a grade the unit did not have (claim_honest false)  -> 1
    the unit is worth nothing to the order (value 0: food that will not last)  -> 1
    else by price / list price: 70% or under -> 5, 85% or under -> 4, at most list -> 3, above list -> 2
The review states the same facts in one line. The purchase is added to data/purchases.jsonl, where a later break
(.market/broken.jsonl names only the item and the time) is matched to the shop and the warranty.
"""
import argparse
import json
import os
import time
from pathlib import Path

import policy

PURCHASES = policy.DATA / "purchases.jsonl"
STEPS = ((0.70, 5), (0.85, 4), (1.00, 3))


def stars_for(price, list_price, settled):
    name = settled.get("name") or settled.get("sku") or "the item"
    if settled.get("claim_honest") is False:
        return 1, f"{name}: claimed grade {settled.get('claim')}, the market's record says {settled.get('grade')}."
    if settled.get("value") == 0:
        return 1, f"{name}: not usable for this order (shelf life too short)."
    if not list_price:
        return 3, f"{name} at {price}."
    r = price / list_price
    stars = next((s for cut, s in STEPS if r <= cut), 2)
    extra = (f", grade {settled['grade']}" if settled.get("grade") else "") \
        + (", as claimed" if settled.get("claim_honest") else "") \
        + (f", {settled['warranty_hours']} h warranty" if settled.get("warranty_hours") else "")
    return stars, f"{name}: paid {round(100 * r)}% of list{extra}."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deal", required=True)
    a = ap.parse_args()
    try:
        deal = json.loads(open(a.deal, encoding="utf-8").read())
    except (OSError, ValueError):
        print(json.dumps({"stars": 3, "review": "Deal completed."}))
        return
    settled, receipt = deal.get("settled") or {}, deal.get("receipt") or {}
    price = receipt.get("price") or settled.get("amount") or 0
    list_price = settled.get("value") or None          # value is the SKU's list price when the unit is usable
    if not list_price:
        try:
            cat = json.loads((Path(os.environ.get("MARKET_DIR", ".market")) / "catalog.json").read_text(encoding="utf-8"))
            list_price = (cat.get(settled.get("sku")) or {}).get("list_price")
        except (OSError, ValueError):
            pass
    stars, text = stars_for(price, list_price, settled)
    try:
        policy.DATA.mkdir(parents=True, exist_ok=True)
        with open(PURCHASES, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "order": (deal.get("order") or {}).get("order"),
                                "shop": settled.get("shop"), "sku": settled.get("sku"), "name": settled.get("name"),
                                "price": price, "list": list_price, "grade": settled.get("grade"),
                                "claim_honest": settled.get("claim_honest"), "warranty_hours":
                                settled.get("warranty_hours"), "expires_at": settled.get("expires_at"),
                                "ledger": settled.get("ledger"), "stars": stars}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    print(json.dumps({"stars": stars, "review": text}, ensure_ascii=False))


if __name__ == "__main__":
    main()
