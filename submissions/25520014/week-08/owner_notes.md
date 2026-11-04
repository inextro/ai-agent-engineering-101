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
- 11-04 10:23 seller (HOLD_GAP 0.05): when the price would become final (or is final) because the buyer only raised
  a little, an offer at or above the floor within 5% of that price closes at the offer. 07:18-09:54 two such talks
  walked at 3.7% under a held final (218k profit lost), and holding there won 0 of 810 deals. SMALL_GAP stays 2%
  for the rest of the talk. Deals closing a few % under the last ask after small raises are this rule, on purpose;
  do not ask to widen SMALL_GAP for them. A buyer whose best offer is under the floor still gets no deal.
- 11-04 11:16 restock: a SKU's cap counts our sales per hour IN STOCK (hours we held at least one unit), not per
  hour since its first supply: buyers only ask shops whose offers list the SKU, so empty hours show no demand. Caps
  of SKUs that sell out fast rise (sofa G5e3784e6 6.8 -> 9.9 an hour); category totals do not change, so the stock
  at cost should not grow from this. More units of a few fast SKUs in a category is this rule, on purpose.
- 11-04 11:28 restock works per SKU, not per category: each SKU's k-th unit is worth P(it sells by the next
  refill, from our own in-stock rate of that SKU) x its own margin, ranked by value per won (turnover counts). A
  category no longer has one count shared by its SKUs, so a second SKU (quilt G5cd4ffbc behind sheet Ge62449c9)
  gets its own units. Offline it spent more of the cash at the same return (0.29 per won) and held about 27% more
  units, mostly fast sellers. More SKUs per category and a higher stock at cost after 11:28 are this change, on
  purpose; the owner watches it (back out if gross profit per hour does not rise while stock passes 34M at cost, or
  stock over 2 h old passes 35%). Do not answer it with restock skips; report what the record shows.
  13:08 check: kept (gross profit 4.88M -> 7.64M/h, stock 46.3M, 29% over 2 h old).
- 11-04 13:12 restock: a SKU that first appeared in the market more than 1 h after the round start counts its
  market sales per hour since its own first supply (at least 0.5 h), not over the whole window, and becomes a
  candidate only after 5 market settles (unless a buyer asked us for it or we hold it). High-end furniture (cost
  0.7M to 1.6M, from 11:02) was undercounted 6 to 10 times and we sold 3 of it in 3 h against dae-yangkim's 57.
  More high-priced SKUs and a larger stock at cost after 13:12 are this change, on purpose; the owner watches it.
- 11-04 13:24 seller: once the price is final, an offer up to 1% under the floor closes (FLOOR_TOL, never under
  the cost). 16 h of talks: 22 walked in that band (59만 margin), 30 deals at the floor had such an offer before
  (4만). Deals a little under the floor after 13:24 are this rule, on purpose; the owner may raise it to 2%.
- 11-04 13:24 forum: a complaint that cites one of our settles but names only another shop is not ours and stays
  off the list (post 3967 was about dragonash's 3 h warranty).
- 11-04 13:48 seller: among units of one SKU and grade the cheapest goods unit is offered first, and its floor is
  the mean cost of the units of that SKU and grade we hold (x 1.08 + the warranty premium), not its own cost. A deal
  a little under one unit's own-cost floor is this rule, on purpose; the SKU and grade as a whole stays above cost.
- 11-04 13:48 restock: a request we missed makes restock keep at least one unit of a SKU only for 2 h after it.
- 11-04 13:48 lost talks where the buyer's number was at or over our floor (10:23-13:48: 20, 110만): we were still
  stepping down. Closing at such an offer within 3/5/10% of our price would win 4/11/13 of them but give up
  41/59/118만 on 99/117/130 deals the buyer later raised into: no change; do not ask to close at the buyer's offer.
- 11-04 11:16 seller: when the unit is swapped for another (taken by a parallel talk) the reply keeps the opening
  wording, or says "this unit's price"; "we came down" on a first reply (5ffb5c63) is fixed.
- 11-04 15:30 apple Gfb2c0249 shelf-life complaints (1781, 2474, 4556) are not a unit-selection bug: each cites a
  sale whose buyer never said how many hours it needed (4556 cites the 05:16 sale, 32.2 h left), and the need
  appears only in the later post. Talks that state a need get a unit that meets it (36 h asked: 37-45 h sold).
  Do not report the unit-selection code for these again; report only a sale whose talk stated a need it missed.
- 11-04 15:30 forum: a post that names other shops as well as us goes on the complaint list only if it cites one
  of our settles (4596 was tomato&potato's ad listing nine shops). 3967 and 4596 were taken off the list by hand.
- 11-04 16:00 restock: a goods unit of grade C held 3 h or more (OLD_C_H) stays on sale but does not count toward
  its SKU's units, so A and B are bought next to it. The seller offers the best grade held, so C stayed and blocked
  them (sheet Ge62449c9 16 C, 3 B, 0 A; 13-16 h, 15 talks opened on C were cancelled by buyers taking only A or B).
  More units of a SKU that also holds old C are this rule, on purpose. The seller is unchanged: C is not pushed on
  buyers who did not ask for it (the owner's call: a buyer who wants A or B gets nothing from a C).
- 11-04 16:30 stock at cost has no fixed line any more (the 11:28 line of 34M is retired): it follows sales. At 16:23
  it was 109M against 99M/h of high-end sold at cost, 1.0 h of sales, 20% over 2 h old and none of the high-end over
  6 h. Report stock only when it covers more than 3 h of sales at cost, or more than 35% of it is over 2 h old.
- 11-04 16:30 the record's restock plan balance is the window's second run, after the first run bought (16:03 run
  74.5M and 49M bought, 16:05 run 39.2M). The balance an hour later is higher by the sales since; it is not a stale
  balance. Do not report the gap.
- 11-04 16:30 FLOOR_TOL stays 1%. 13:24-16:30: 8 talks walked 1-2% under the floor (82만, 71만 of it two sofa
  talks), while 62 deals had an earlier offer in that band (up to 93만 given up at 2%). Do not ask again unless the
  walks in that band pass the deals that had such an offer.
- 11-04 16:50 clearance: any goods unit held 6 h or more (clearance.STALE_H), whenever bought, opens at the market's
  median share of the list price for its SKU over the last day less 5% (grade C less c_discount too), floor 0.9 x
  that ask, never under 0.9 x cost; the seller offers such a unit before the cheapest of its grade. The owner's call:
  unsold goods only cost storage. Deals of old goods under the usual floor, logged with arm "clearance", are this rule,
  on purpose; restock does not buy those SKUs again (first unit 624th-748th of 752 by value per won).
- 11-04 16:50 seller: when we hold a lower goods grade of the SKU 3 h or more and at least 5% cheaper, the first reply
  on the best grade adds one sentence, no price, that a cheaper box-damaged (B) / refurbished (C) one is there. The
  buyer who asks for it ("refurbished", "cheaper one", "grade C") gets it on its next message; anyone else stays on the
  best grade. No mention to a buyer who named a grade. The open record has "mentioned". The owner judges both rules
  with tests/stale_check.py; do not change them from one hour's numbers.
- 11-04 19:09 restock: a SKU whose demand to us is 3 an hour or more (FAST_RATE) keeps 1 h of stock (FAST_COVER_H),
  not the refill cycle of all SKUs (about 2 h): it gets a line in every supply window (20 min, 16-24 min apart), so
  sofa G8ec51935, bought in each of 24 runs, held 69 units (94M at cost). The cash goes to the next SKUs, among them
  high-end ones we never stocked (라클라우드 G03d0199f, 시몬스 Gb5d0dde7, 헬렌스타인). Lower stock at cost, fewer units
  of the fast sofas, and new high-end SKUs after 19:40 are this rule, on purpose; a fast SKU out of stock for part of
  a window is the price of it (offline: 0.9M margin lost against 15M gained over 6 h). The owner watches board profit
  against dae-yangkim and stock at cost; do not answer it with restock skips.
- 11-04 19:22 restock: a goods unit of grade C does not count toward its SKU's units from the moment it arrives
  (OLD_C_H 3 -> 0). With A or B held the seller offers the best grade, so no C of those SKUs sold 16:31-19:03 and none
  of 97 buyers told of a cheaper C took it; under the 1 h cover a fresh C took the place of an A or B (에비뉴엘
  G8adbc159: 9 units held, 4 of them C). C stays on sale and goes to clearance at 6 h. A larger C count next to A and
  B is this rule, on purpose.
- 11-04 19:39 seller C fallback (c_fallback): when the price of an A or B goods unit is final and the buyer's offer is
  still under what closes, a C unit of the SKU whose floor the offer meets is offered once, at the buyer's offer,
  with its grade and certificate said; only the buyer who then says yes (or asks for the refurbished one) gets it,
  anyone else stays on A or B. Never to a buyer who named a grade. 13-19 h: 164 goods talks ended that way with a C
  held and the buyer's best at or over its floor, and with A or B held no C of those SKUs sold. C deals at the
  buyer's offer, logged "fallback", are this rule, on purpose; the price bandit leaves them out. The owner judges it
  by fallback offers against C deals; do not change it from one hour's numbers.

## buyer

- 11-04 07:18 a (shop, SKU) that answered 409 "not in stock" twice in 6 h goes to the end of the list (not skipped)
  for 6 h. 409 counts over a 6 h window include hours before this change until 13:18.
- 11-04 10:23 the owner keeps dae-yangkim G6b9db161 (unit u-c3faaaa21dd8) at the end of the list, not skipped: its
  409s (36 of 42 in 6 h) come only on orders where every other shop gave no quote, so those orders had no other deal,
  and the pair still paid 11 in 12 h. More 409s in an hour mean more orders ran out of other shops, not a broken
  rule; do not ask to skip it again. Ask only if it stops paying entirely for 6 h.
- 11-04 07:29 forum_alerts: "평균 하락" right after a post is usually the newest posts not having their votes yet;
  the owner reads it and decides (11-04: ignore it when it is that).
- 11-04 15:30 forum_alerts: "평균 하락" now fires only when the last 2 h of posts average 0.03 under the score.
  Posts at 6 likes 0 dislikes (Wilson 0.61) are the normal case and the owner ignores them; the old rule fired every
  hour on them. Report it only when it fires under the new rule.
- 11-04 17:17 buyer: another shop's quote is named only when it is under every ask THIS shop gave in the talk, not
  just its current ask. moss-and-oak raised 303,872 -> 339,154 after our first offer, we named lee-sungsoo's 324,608,
  and it closed at 324,608 (3 such deals, 17:05 report). The receipt was moss-and-oak's own, not mixed up. A receipt
  above the shop's own lowest ask is still taken while under the cap (24 h: 105 of 2,121 deals, 77만): refusing a
  closed talk would restart the order and lose some; do not ask for that check unless it passes 30만 an hour.
