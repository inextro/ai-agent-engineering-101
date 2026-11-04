# Owner's design notes

Each code or policy change the owner makes, with when and why. Add a line here with every deploy that changes what
the code does, so the managers do not read a deliberate effect as a fault and undo it.

## shop

- 11-04 07:18 seller: an offer at or above the floor and within 2% of the ask closes at the offer (SMALL_GAP). A raise
  too small to count as a raise used to freeze the price; 45 talks were lost that way in 10 h.
- 11-04 07:18 the record's restock_plan shows balance_at_last_restock and when the plan was made; it is not the live
  balance.
- 11-04 07:22 restock caps a SKU at its demand to us (paid sales + missed requests x close rate) over the last refill
  cycle (1-2 h), not 1 h of paid sales. The kit buys at most 10 lines a run (the course's kit; the owner cannot change
  it), about 20 lines a supply window; do not ask for more lines per window.
- 11-04 07:26 our share of a category's demand comes from our own sales in it (shrunk toward the shop-wide share).
- 11-04 07:46 the price bandit's reward takes 0.2 from the buyer's stars (the score's stars weight). So it opens more
  talks at the lower arms (0.7, 0.8) and fewer at 0.9 / 1.0: margin per deal falls ON PURPOSE, for stars and more
  deals. A falling hourly margin after this is not a reason to raise min_margin; judge by profit + revenue + stars.
- The floor already pays for breaks: with a warranty, floor = cost x (1 + min_margin) + break rate of the grade x the
  expected price; without a warranty there is no refund. A break rate above min_margin is not a loss at the floor.
- 11-04 09:01 the manager raised min_margin 0.08 -> 0.11 on the two reasons above; 11-04 09:23 the owner set it back
  to 0.08. Keep it there; if the record argues for a change, put it under 결정 필요 instead.
- 11-04 09:23 the template reply after a step down says "we came down", not "we can come down".
- 11-04 09:23 the complaint list holds only posts that name us or cite one of our settles; a post in our thread about
  another shop is still answered but not listed.
- 11-04 09:45 the record's stock shows unsold_24h_or_more (units, cost, top categories). From 1,000,000 won at cost
  the code puts an alert at the top of the report; widening clearance.py (now only stock bought before the 11-03
  23:55 fix) is the owner's call: say what the record shows under 결정 필요, do not answer it with restock skips.
  On 11-04 09:45 nothing was unsold for 24 h (88% of stock at cost was under 2 h old).
- 11-04 09:54 seller: a buyer who raised an earlier offer to just past the ask (at most 1.2 x ask) gets the deal at
  the ask at once; before, the shop asked "several units or a mistake?" and 4 talks in 24 h ended on that last turn.
  A first number above the ask, or more than 1.2 x ask, is still asked about.
- 11-04 09:54 template wording: goods grade A is "in normal condition with no defects" (never "new"); the warranty
  line says the market refunds the full price "from our balance".
- 11-04 09:54 the record's claims are true/false/none_made. none_made is a buyer checking out without our claim (all
  15 so far are one buyer, dariaklt); it is not a false claim. False has been 0.
- 11-04 09:54 the owner put the hours back into the negotiation playbook (version 10): food shelf life is said as the
  facts give it ("at least N hours", code eee8656), the warranty in words ("six-hour"). The 09:47 retries came from
  the warranty written as "6" and amounts in words, not from food hours; do not take the hours out again.

## buyer

- 11-04 07:18 a (shop, SKU) that answered 409 "not in stock" twice in 6 h goes to the end of the list (not skipped)
  for 6 h. 409 counts over a 6 h window include hours before this change until 13:18.
- 11-04 07:29 forum_alerts: "평균 하락" right after a post is usually the newest posts not having their votes yet;
  the owner reads it and decides (11-04: ignore it when it is that).
