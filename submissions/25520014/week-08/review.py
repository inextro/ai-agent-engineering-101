"""Week 08 review hook: stars for a paid deal, from the market's receipt only.

    python review.py --deal .market/deal.json

The kit runs it after each settled deal and its stars replace market_buyer's. The stars come from facts the market
signed (deal.json `settled`), never from anything the shop wrote:
    the shop claimed a grade the unit did not have (claim_honest false)  -> 1
    the unit is worth nothing to the order (value 0: food that will not last; our accept asked for enough hours, so
    this is not what we agreed to buy)  -> 1
    else price / list price against the market's mean ratio (model.Prices, other buyers' deals):
    more than BAND under it -> 5, within BAND -> 4, more than BAND over it -> 3
Against the market rather than fixed cuts: when every shop sells near 60% of list, fixed cuts give everyone 5 and
the stars say nothing about which shop treated us better.
The review states the same facts in one line. The purchase is added to data/purchases.jsonl, where a later break
(.market/broken.jsonl names only the item and the time) is matched to the shop and the warranty.
"""
import argparse
import json
import os
import time
from pathlib import Path

import model
import policy

PURCHASES = policy.DATA / "purchases.jsonl"
BAND = 0.05                     # within 5 points of the market's mean ratio is an ordinary price


def market_ratio():
    try:
        return model.Prices(half_life_h=policy.load()["half_life_h"]).market
    except Exception:
        return model.PRICE_R0


def stars_for(price, list_price, settled, market):
    name = settled.get("name") or settled.get("sku") or "the item"
    if settled.get("claim_honest") is False:
        return 1, f"{name}: claimed grade {settled.get('claim')}, the market's record says {settled.get('grade')}."
    if settled.get("value") == 0:
        return 1, f"{name}: not usable for this order (shelf life too short)."
    if not list_price:
        return 4, f"{name} at {price}."
    r = price / list_price
    stars = 5 if r < market - BAND else 4 if r <= market + BAND else 3
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
    stars, text = stars_for(price, list_price, settled, market_ratio())
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
