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
  B is this rule, on purpose. Retired at 20:27, see below.
- 11-04 20:27 restock: OLD_C_H back to 3 (a goods C counts toward its SKU's units for its first 3 h). 72% of goods
  supply is A and the seller sells A first, so B and C pile up; uncounted, each C was bought over again in A and B
  (자코모 4인 G8ec51935 at 20:11: 13 A, 36 B, 32 C, 92M at cost; stock at cost 175M -> 192M since 19:04, board gap
  155M -> 170M). C still sells through the C fallback. Offline 6 h: +29-50M board against OLD_C_H 0. Fewer fresh A
  and B lines on SKUs that hold C is this rule, on purpose.
- 11-04 19:39 seller C fallback (c_fallback): when the price of an A or B goods unit is final and the buyer's offer is
  still under what closes, a C unit of the SKU whose floor the offer meets is offered once, at the buyer's offer,
  with its grade and certificate said; the buyer who then says yes, asks for the refurbished one, or sends a number
  at or over the C's price without refusing gets it (from 11-04 19:53: three of the first four sent their number
  again; owner: A sells anyway, sell C when we can). A number that reaches what closes the A or B closes the A or B. Never to a buyer who named a grade. 13-19 h: 164 goods talks ended that way with a C
  held and the buyer's best at or over its floor, and with A or B held no C of those SKUs sold. C deals at the
  buyer's offer, logged "fallback", are this rule, on purpose; the price bandit leaves them out. The owner judges it
  by fallback offers against C deals; do not change it from one hour's numbers.
- 11-04 20:05 answer to the 20:01 report: 35866f9a (19:42) and c2bf4f2b (19:44) were offered before the 19:53 change,
  so A's final price after the repeated number was the rule then, not a bug. Of the 7 offers from 19:53 to 20:02,
  5 closed as C (repeated number 4, yes 1); 2 did not: the buyer went under the C's price, or left. Report a
  c_fallback bug only for a talk whose C offer is after 19:53 (turns ts >= 1793789584). Paid share of fallback C deals
  (2 of 5 at 20:03; non-fallback C 18-20 h: 55%) is judged by the owner at 21 and 22 h; do not report it before.
- 11-04 20:32 seller B first (B_FIRST): a goods SKU holding B opens on B to a buyer who names no grade and asks for
  no lower one; a buyer who names a grade gets it as before, food is unchanged, C comes by asking or the fallback.
  72% of goods supply is A and best-first left B behind it (자코모 4인 G8ec51935 at 20:11: 13 A, 36 B). Per list
  price B earns 0.14 after refunds against A's 0.07; goods talks opened on B closed 62% against A's 49% (13-20 h, B
  then held without A); B paid 89%, A 94% (18-20 h). Offline 6 h with OLD_C_H 3: +10-25M gross. Only 5 of 3,968
  talks asked for A after a first message naming no grade. More B deals and an A count that grows is this rule, on
  purpose; the owner judges it at 21 and 22 h by talks opened on B (close and paid share) and stars.
- 11-04 20:51 B_FIRST off (seller opens on the best grade again). 20:33-20:45, with A held: goods talks opened on B
  closed 7 of 25, on A 9 of 14; 자코모 4인 B 1 of 10 (cut to 1.51M), and no A of it sold. Buyers left on the first
  B quote without a number, at a B ask well under the A's: a grade they refuse, not a price. The 62% close of B
  openings was measured with no A held. Do not report the 20:32-20:51 drop in deals as a new problem.
- 11-04 21:00 seller fallback offers B before C (B_FALLBACK): when an A's price is final and the buyer's offer is
  under what closes it, the B of the SKU whose floor the offer meets is offered once at the buyer's offer, else the
  C as before; same yes / repeated-number / refusal rules. A number above the fallback's price closes at that number
  (at most list; 19:58 a buyer sent 1,438,400 to a C asking 1,276,000 and paid 1,276,000). Other shops sell goods B
  at its supply share (18-23% of their goods deals) and ours sat at 13%; a B put before a held A loses the buyer
  (20:32-20:51), a B after the A failed costs no A deal. B fallback deals, logged "fallback" with fallback_grade B in
  turns, are this rule, on purpose; the owner judges them with the C ones (paid share, stars).
- 11-04 21:25 restock: fast SKUs (3+ an hour to us) keep one supply window of stock (FAST_COVER_H 1.0 -> 0.34), to
  turn stock into cash first (owner). dae-yangkim holds 9 goods units (4M) and sells its grades as supplied; our
  deep stock sold A and left B and C (at 21:04: A 56M, B 76M, C 43M). Offline 6 h: board +70-96M against 1 h, stock
  to 34-44M, gross 0-9M lower. Fast SKUs running out of A between windows, B and C offered when A is gone, and fewer
  supply lines are this rule, on purpose. The owner judges it at 22 h; it goes back to 0.5 h if gross falls under
  15M an hour. Do not answer it with restock skips.
- 11-04 22:41 restock: a SKU's expected cost now carries the market's surge factor (inventory.json `surge`, rule from
  21:27: past 50 units of a SKU in the market's last hour each unit adds 0.2% to its cost, up to +50%). Costs were
  list x 0.59 for goods, so a surged SKU looked cheaper than it was (지누스 그린티 x1.23). From 21:27 to 22:40 we paid
  0.91M over the plain price, 0.86M of it on 자코모 4인. Fewer units of surged SKUs, or none once the factor eats the
  margin, are this rule, on purpose. Do not answer it with restock skips.
- 11-04 22:44 clearance: a goods SKU other shops settled 20+ times in the last 2 h while we settled under 5% of that
  is "stuck"; a unit of it held 1 h+ opens at the SKU's market median share of list, floor 0.95 x that, never under
  its cost (owner: sell what we hold but cannot sell a little cheaper). 20:40-22:40: 지누스 그린티 sold 379 in the
  market at a median 0.73, ours 0 of 68 in 42 talks, our floor 0.78 with a buyer stopped at 0.75. These talks are
  logged with arm "clearance". Sales of stuck SKUs at about the market median are this rule, on purpose.
- 11-04 23:00 seller: a daemon thread runs rule_watch.py every 2 minutes and appends any change of the market's rules
  page, inventory.json fields or catalog SKUs to data/alerts.md (owner: detect new rules in real time, report only).
  The surge rule went live at 21:27 and the shop manager first named it at 22:01. Read data/alerts.md at each review
  and name what is new there; it changes nothing by itself.
- 11-04 23:28 model: our own sales rate per SKU (own_sku_posterior, restock's caps and P_k) forgets with a 1.5 h
  half-life (OWN_HALF_LIFE_H), not the policy's 24 h; the category share and market rates keep half_life_h. 자코모 4인
  read 44 an hour at 22 h when we sold 13 (our share fell from 80% to 13% after 19 h). Backtest 12-22 h: deviance
  2551 -> 2189, predicted 2627 -> 2178 against 2154 sold. Caps that follow the last hours, up and down, are this
  rule, on purpose.
- 11-04 23:30 restock: each new unit is priced at the surge it will pay, not the factor now. surge_counts forecasts
  a SKU's market units in the hour at our purchase (83 s into the window): the ledger's units still in the hour
  then, plus what other shops bought in the previous window over the same seconds; the j-th new unit costs
  factor(U + j) (unit_cost), so a surged unit is bought when its value per won still ranks, and a SKU just under 50
  pays for the units we add. Backtest on the Round 3 ledger (1,081 SKU-windows): factor now MAE 2.26%p, +1.65%p
  too low; forecast 1.28%p, -0.08%p. Archive older than 2 min: inventory.json `surge` as before. Lower values
  for surged SKUs are this rule, on purpose (owner, 11-04 23:30).
- 11-04 23:56 restock + clearance (owner: buy only what sells above our floor; what we already hold and cannot
  sell, sell it all, even at a small loss; run as designed from here). model.market_grade_prices: other shops'
  median paid / list per goods SKU and grade (2 h, or the day with under 5 settles). restock skips a goods SKU
  whose A sells in the market under the floor an A at our mean A cost would have (0.66 x list x the forecast surge
  of our next unit x 1.08 + the A break premium), and prices it at most at the market's grade prices over the
  supply mix (A 60, B 24, C 16%): 43 of 593 candidates dropped, 49 priced lower. clearance: a goods unit whose
  usual floor is over the market's price for its SKU and grade is "underwater": it opens 1% under that price, floor
  0.95 x the ask, never under 0.85 x its cost; stuck/stale/underwater together take the lowest floor. 52 units
  (37.6M at cost) at 23:50, e.g. 자코모 4인 A floor 0.846 -> 0.756 of list against the market's 0.804. Cause:
  22-23 h 자코모 4인 A floor 0.86-0.93, the market's A 0.80-0.81, 92 talks and 4 deals, and restock bought A again
  at 23:05 and 23:22 at its own category price (0.82). Lower prices on these units are this rule, on purpose.
- 11-05 00:10 model + seller (owner: sell C when we can, at least at cost). Break rates: a break names its unit;
  model._breaks matched it to the last sale of its (buyer, SKU), so they read A 0.063, B 0.094, C 0.135; matched by
  unit they are A 0.029, B 0.121, C 0.34 (the market's 11-04 rates: A 0.03, B 0.12, C 0.34). A floors drop about 1%
  of list, B and C floors under warranty rise (C 0.12 -> 0.24 of list over cost), so more C goes on underwater
  clearance at the market's price; refunds expected over 6 h 6.4M -> 16.2M, the cash reserve 10.5M -> 23.2M.
  restock's warranty cost is the supply-mix weighted rate (0.101), not a plain mean of A, B, C. Fallback: a B/C
  offer the buyer let lapse comes back once when the buyer's number is again at or over it (FALLBACK_MAX 2):
  11-04 5 talks lost that way (3.5M; e8dec12d 다우닝 3인, the 00:01 report's case).
- 11-05 00:10 answer to the 23:01 and 00:01 reports: the 22:28 post on G03d0199f (warranty 3.0 h) is
  dae-yangkim's settle, not ours. Nothing to fix on our side.
- 11-05 00:22 seller + model + policy (owner: let the bandit price what we actually ask). Goods open at
  min(list, floor x (1 + x)), x from policy.GOODS_ARMS (0.0, 0.04, 0.08, 0.15); food keeps the list-share arms
  0.7-1.0. With list x arm, the floor overrode the arm in half the goods talks (arm 0.7: 76%), so the goods bandit
  learned the floor's results. Goods talks before 00:22 are left out of its counts (their arms meant something
  else): every goods arm starts at Beta(1, 1), expect some exploring in the first hour. 11-03's rule "leave the
  arms to the bandit" is changed by the owner for this. Revert if the goods deal rate falls over 5%p or profit
  per talk falls in the first hour.
- 11-05 00:32 seller + clearance + restock (owner: no warranty where buyers do not pay for it). Goods B and C carry
  the 6 h warranty only when the buyer's first message asks for one (warranty / guarantee / 보증; seller
  WARRANTY_ON_ASK), A always. Market 11-04, per sale as a share of list after the break window: C without a
  warranty paid 0.676 and refunded nothing, with one 0.681 and 0.203 refunded; B 0.721 / 0 against 0.727 / 0.069.
  Stars come at the sale, before any break. 8% (C) and 13% (B) of our first messages ask. Do not turn
  warranty_B / warranty_C off or on to change this: the flags stay true and the rule is in the code. A goods ask now
  starts from max(floor, 0.95 x the market's price for its SKU and grade) x (1 + arm): without the warranty a C's
  floor is ~0.37 of list while the market pays 0.68. restock counts the warranty cost of B and C at the share of
  buyers who ask.
- 11-05 00:50 model + restock (no restart). own_sku_rate: half-life 1.0 h and, when the market settled the SKU 5+
  times in the last hour and we held it 0.5 h of it, half its mean from our settles of that hour (RECENT_W): a
  share lost to another shop shows within the hour (자코모 4인 18.4 -> 10.0/h). Backtest 11-04 12-23 h: deviance
  2389 -> 1918, bias +1.4% -> -0.3%. restock cap is continuous: min(2 h of demand, max(3 units, 0.34 h of demand));
  the 3/h cliff gave a SKU just under it 6 units and one just over it 2. Offline 6 h: gross 68.2M -> 74.7M, board
  +142.8M -> +161.2M, stock 56.6M -> 44.6M, fewest stockouts. Judge us against the whole market (our share of its
  revenue and of its top SKUs), not against the first-placed shop: 23-00 h the market held while our share fell
  from 26% (16 h) to 7%, Kiro729 0.7% -> 11-19%.
- 11-05 01:10 seller (kit restarted). First replies go out from the template without haiku (the price is the
  bandit arm's as before; haiku writes from the buyer's first counter on), the bandit counts are cached 60 s and a
  thread refreshes break rates, the clearance table and the counts every 30 s, and the warranty is written as digits
  ("a 6 h warranty"). Buyers ask several shops at once and buy from the first fit: of our 137 high-end talks left
  after our first reply, 121 had the SKU settle elsewhere (median 1.9 s after our reply). Our first reply took 9.9 s;
  walkaways 46% at 4-8 s, 60% at 8-12 s, 79% over 12 s. Not a price or star problem: 자코모 4인 A cleared at market
  price (0.80 of list, cost 0.78) and still lost 9 of 10. Do not dump 자코모 A or C below cost: A sells 56/h in the market.

- 11-05 01:11 model + restock (no restart). A SKU short of supply in the market's last hour (sold >= 85% of the
  market's stock at the start plus what was supplied, that stock near 0, 3+ sold) is entered without the unproven
  wait, its demand to us at least the hour's sales / (shops that supplied it + 1), its cap up to 1 h of that. On
  11-04 we held such SKUs in 29% of their short hours against 95% of saturated hours, and entered 라클라우드 퀸,
  시몬스 퀸, 에이스 BMA 7-8 h after they went short; dragonash took 48-77% of such mid-price bedding SKUs. A short
  hour was followed by a saturated one 0% of the time. Surge cost and the market A price check still apply.
- 11-05 01:29 clearance + model + seller (kit restarted). Stale and stuck units ask just under the market's price
  for their own SKU and grade (the all-grade SKU median had put A at C-mixed prices: G1515d951 A sold at 0.70 of list
  against the market's A 0.86, under cost); underwater clearance starts only after a unit has been held 1 h; our
  last-hour sales rate counts per hour held. Found by a review of tonight's deploys.

- 11-05 07:08 (a1f8420, 7897fa5; kit, manager, forum restarted). The board's ALL uses book since 11-05 03:45 (cash
  + what unsold units cost; was cash): a sale under cost lowers the score by the gap, an unsold goods unit costs only
  storage. So goods never sell under cost now: clearance floors (stale, stuck, underwater, pre-fix) are at least the
  cost, and the last 6 h floor is the cost (was half). Food within 3 h of its date still goes to half the cost (past
  its date it leaves book at its cost). This answers the 04:01-07:09 question; do not ask it again. Clearance deals
  may fall a little on purpose. The record now has book_so_far and alerts_last_hour (data/alerts.md). The template
  alert no longer counts first replies (template on purpose since 01:10); turns written before 07:08 have no "fast"
  flag, so it may still fire until 08:10. The break rates and bandit arms you saw (A 0.065, goods@0.7-1.0) came from
  the manager process running code from 11-04 10h; it was restarted and reads A 0.029 / B 0.123 / C 0.342 and goods
  arms 0 / 0.04 / 0.08 / 0.15. The kimchi first reply that names another kimchi SKU is the category fallback on
  purpose (6 h: 33 of 41 such talks closed). totato2203-r3's "[시세·가게 분석 | tomato&potato]" roundups (74-148 shops
  named) are not complaints any more; 30 were taken off the list.

- 11-05 08:00 (seller, kit restarted). A buyer that states no shelf-life need is offered food only with 0.75 of its
  shelf_hours left. The market's own buyers (buyer-01, buyer-02) never state one, need 0.25/0.5/0.75 of shelf_hours,
  pay anyway and give 2 stars when it falls short: 52 of our 143 verified house results. Stars count only verified
  sales. Food sales to buyers who say nothing will fall on purpose (at 07:53, 1 of 9 food units qualified). The 2-star
  "Only N h of shelf life left" reviews before 08:00 are known; do not report them again.
- 11-05 08:00 (policy, same kit restart). Goods open at arm 0.0 only (GOODS_ARMS was 0.0/0.04/0.08/0.15): the
  bandit had settled on it (00:22-07:08: 24.2% closed, 39.5k profit a talk, stars 4.73; the others 19-20%, 29.8-40.5k,
  4.45-4.63). The board weighs book 0.5, revenue 0.3, stars 0.2 and we lead the first two, so stars matter most now.
  Goods talks under other arms before 08:00 are history; do not propose bringing the arms back without new data.
- 11-05 08:15 (restock, no restart: restock runs fresh each window). SKU_PRIOR_H 2.0 -> 0.5: the market-based
  guess of our sales of a SKU counts as 0.5 h of our own in-stock sales, not 2 h, so a SKU we sell above our
  category share gets more units sooner and one we sell below it fewer. Backtest: next-hour sales predicted better
  (log score -1.526 -> -1.445). 6 h offline: book +3~6M, end stock the same. Expect restock lines to lean further
  toward SKUs we already sell well.
- 11-05 08:40 (no code change). The 08:08 question on Gc1297f36, G8095ddd6, Gb5d0dde7 (market A under our A floor,
  talks lost 2-3% under the floor): restock bought them as usual candidates, not through the missed-request rule
  (21 buys in 3 h, none "asked"). Its A cost guess is right on average (A_COST 0.66; A units cost 0.664 of list in
  the last 6 h) but one A unit costs 0.55-0.78, and the dear ones get a floor over the market's median. That is fine:
  of 235 goods units with a floor over the market's price on arrival (12 h), 147 sold in their first hour at the
  usual floor (14.0M more than at 0.97 x market), 54 in 1-3 h, 11 later, 23 held; the market price is a median,
  so many buyers pay over it. After UW_HELD_H the underwater terms sell at market, never under cost. Keep it; talks
  lost a little under the floor in a unit's first hour are expected, do not report them.
- 11-05 09:40 (manager restart, report only). Reports are bullets now (owner): a header line per section, then
  short "- " points. The shop report gets a '시장' section from the new "competition" record: share by hour, rising
  shops and their prices by SKU and grade against ours, our talks split by food/goods, rival SKUs/the rest, asks
  at or under the market/over it. Why: 07-09 h our share fell 17.3 -> 13.5 -> ~10% while gom31-certified-goods
  rose 10.7 -> 15.8 -> 18.4% on our top SKUs (A of G8095ddd6 at 0.72 of list with a 12 h warranty against our
  0.76; G8ec51935 0.77 against 0.81), and the 09:01 report blamed food, which had 2 talks of 323. Name a cause only
  from a part with enough talks.
- 11-05 10:00 (manager restart, report only). Both reports get a '발견' section (owner: what we miss about the
  market): at most two new points, each observation -> evidence -> how to check, or '- 없음'. New in the shop
  record: buyers_who_left_after_our_first_reply (their first messages) and competition.quotes_our_buyer_got_last_hour
  (other shops' first asks to our own buyer for SKUs we sell; grade not recorded). The warranty stays 6 h (owner,
  11-05 09:50), although goods break only within 6 h and a longer one costs no refunds: do not propose it again
  without new data on buyers choosing by warranty length.
- 11-05 10:10 (restock, no restart). The market check no longer adds min_margin (A_CHECK_MARGIN = False): a goods SKU
  is skipped only when an A at our mean A cost x the forecast surge plus its break cost is over the market's A
  median. With the margin an average A sat on the median (자코모 4인 G8ec51935 0.797 against 0.797), so our best SKUs
  flipped out of restock on small moves (235M of it sold in the market in 6 h while we held none). 12 h backtest:
  SKU-hours blocked only by the margin earned 0.147 of list a deal when we held them (A 0.094). Expect more lines
  on SKUs with a thin A margin (G8095ddd6, G45c54b01, G754bf0ca); the floor in the talk is unchanged.
- 11-05 10:59 (manager restart, report only). The shop record gets "market_gaps" (6 h): per category the market's
  settled amount, our share, the share of market sales that came while we held that SKU and our share of those;
  the SKUs that sold most while we held none; the orders our buyer saw that no shop had stock for. Why: over
  12 h bedding was 3.5% ours, but 9.8% of what sold while we held the SKU: 71% of its sales came while we held none
  (53 of 64 SKUs never held), a stock gap restock can close; mattresses and sofas sold 83-88% while we held the
  SKU, so there the gap is buyers' choice, not stock. In 6 h 자코모 4인 G8ec51935 sold 235M while we held none.
  Low sales-while-held points at restock; a low share-while-held at price, terms or buyers who pick at random.
- 11-05 13:20 (owner decisions on the 13:01 report; do not ask again without new data of the kind named here).
  Warranty stays 6 h: 25620027 moved to 24 h at 10 h and its goods share stayed in its old range (8.5, 12.4% vs
  4.7-11.1% before); inside that shop the same SKU and grade sold at 6 h and 24 h at mixed prices and counts, so
  "10-25x more units" compared shops, not warranties. All 3,152 breaks in 48 h came within 6.0 h of the sale.
  Raise it only with a same-SKU, same-grade, same-hour comparison where the longer warranty sells more.
  The 24 h unsold stock (mostly old C sofas) is not cleared below cost: book counts unsold units at cost and
  storage is 0.25% of cost per 6 h (about 6,000 won on 2.3M), so a sale under cost only lowers book. Do not
  list it under 결정 필요 again; report it only if storage or the end of the round changes that.
  자코모 4인 G8ec51935 is bought in nearly every window since 10:04 (1-13 units, 10-12 h 60 sold); hours while we
  held none come from selling out between windows, and the quantity is limited by surge (the market buys 80-130
  units an hour, each extra unit costs 1.08-1.16x), not by the market A check.
- 11-05 16:16 (restock, no restart). A goods SKU with demand of 3+ an hour holds 2 units past its cap
  (FAST_BUFFER): buyers see a SKU only while we hold a unit, and fast SKUs ran out 64 times (13.1 h) in 08-16 h.
  56 replay: the 2nd-3rd units of a fast SKU sold 85% in 1 h, 95% in 3 h; +2 would have covered about 40.8M of
  sales in 8 h. Expect about 15M more goods stock and more lines on fast SKUs; do not report that stock as stale
  unless it is unsold after 3 h. The market A check now runs on each unit up to the dearest one bought
  (A_CHECK_TOP, no min_margin); it cuts nothing at today's line sizes. Watch: fast-SKU stockouts, 3 h sell-through
  of units bought 7th or later, 24 h stock.
- 11-06 01:07 (policy, seller text). Goods that carry a warranty now carry 72 h, fixed in policy.py (72, 72, 72):
  goods break only within 6 h of the sale, so the refunds are the same as with 6 h. 168 h (owner asked 7 days) ran
  01:07-01:20 and the market refused those settles (claim_and_warranty_well_formed, 14 talks); 72 h settles since 01:21. Buyers wrote
  "a 6 h warranty is too short for a sofa" in 56 reviews on 11-05 (yeonsong2, 43M of ours in 6 h, stopped coming
  after them), and 25620027 sells every unit with 72 h. B and C still get it only when the buyer asks
  (WARRANTY_ON_ASK: it was worth about +2.5M book and +6M revenue an hour). The refund reserve still counts the last
  6 h of sales only. Watch: warranty complaints in reviews, visits from yms979, yeonsong2 and buyers who never came,
  our share against 25620027.

- 11-06 01:33 (owner). Our forum score fell because someone targeted us: they picked only our OLD posts and gave
  each 3 dislikes. It is not a reaction to what we post now or to our deals, so do not rewrite the forum guidance or
  change numbers to answer it; report it only if new posts (under 1 h old) start getting dislikes.
- 11-06 01:27 (seller, kit restarted 01:27:48-01:29:37). A number under a fallback unit's price is a counter, not a
  yes, even when it names the grade: yms979 sent "the refurbished unit? I can offer 1600000" to a C at 1,700,000, the
  talk closed at 1,700,000, it rated 1 star with "Avoid" and stopped coming (1 of 304 fallback closes in 72 h). New
  tunnel investigations-organisation-dealtime-photographs. Watch: fallback closes, receipt mismatch reviews.
- 11-06 01:43 (seller, kit restarted 01:43:32-01:44:52, new tunnel merely-wondering-pretty-connections). A goods
  floor is now the offered unit's own cost x (1 + min_margin) + warranty premium, not the mean cost of the SKU and
  grade held (POOL_FLOOR = False); the cheapest unit still goes first. One dear unit lifted the cheap ones' floor over
  the market: in 12 h, 316 A talks had the cheapest unit's floor under the market's A and the mean floor over it, 39%
  closed against 50%. Dear units left over now sit until the underwater rule (1 h) and sell near cost: expect more
  goods sold at about 0 margin; that is by design, do not report it as a bug. Kept as is: buying A units whose floor
  is over the market on arrival (24 h: 421 units, 97% sold, median 0.8 h, +16.6M, 4.6% of cost). Watch: A share on
  fast SKUs (G75f11c19), goods margin, units unsold after 3 h.
- 11-06 10:46 (seller and share_watch, kit restarted at 10:46). Two buyers get "none in stock" from their first
  message on (seller.BLOCKED, refusals in data/blocked.jsonl): totato2203 ("Quote the total price. Require a market
  certificate.") now rates every shop 1 star, and Q00 ("(buyer: q00)") rates every large shop 1 star "SCAM". Since
  06:09 the board's star ranking is the mean over buyer accounts of each one's mean stars here (plus 5 at 3), so
  each account is 1/50 of it; blocking freezes their means (ours 4.19 and 1.00). Fewer goods talks from them is by
  design. shaun0927 (also low, "Certificate attached, please.") is kept for now: it buys 127M a day. New alert
  "warranty" in alerts.md: buyers may want a warranty on goods B/C (patch 3.4 scores an unrefunded break -1); when it
  rings, put it in 결정 필요 for the owner (giving B one by default), do not change WARRANTY_ON_ASK yourself.
- 11-06 11:10 (seller, kit restarted at 11:10). shaun0927 is blocked too, by its templates "Certificate attached,
  please." and "... certificate attached." (95% of its paid deals here): since 01:21 it rates us 1.4-1.7 with or
  without a warranty while other shops get about 3.9 from it in the same hours. It bought 127M a day (7.5% of our
  revenue, 24.5M margin), so expect revenue and goods talks to drop by about that; it is by design, not a fault.
- 11-06 11:32 (owner, both managers restarted). No likes any more: upvote() sends nothing (manager.UPVOTES_OFF), for
  the shop and the buyer. Patch 3.4 scores buyers by stances from other accounts, so our likes lifted rival buyers'
  forum. Leave "upvote" empty; we never dislike either.
- 11-06 11:53 (restock, clearance, share_watch; kit restarted 11:53-11:55). An A unit whose floor is over the
  market's A price for its SKU (bought dear: top of the A cost range with surge) no longer counts as the SKU's
  stock, so restock buys a fresh A; the seller offers the cheapest A first. The dear one goes on underwater
  clearance after 1 h, just under the market, and may now sell under cost down to cost x (1 - 0.01 x days left)
  (0.885 now, the cost on the last day): kept to the end it would pay 1% a day in storage. Small losses on such
  units are by design, not a fault. New alert "new SKU" in alerts.md: a SKU first supplied in the last 6 h that
  made 3%+ of the market's revenue in the last hour; report it to the owner with our share and stock.

- 11-06 13:10 (manager restarted). competition.quotes_our_buyer_got_last_hour now holds only quotes for the SKU the
  order closed on: before, a cheaper SKU's ask was divided by this SKU's list price and read as an under-cost quote
  (lee-sungsoo "0.406" on G24f5f979 was 1,550,000 for G8ec51935). Records before 13:10 have no quote_skus: none shown.
- 11-06 13:22 (restock, seller; kit restarted 13:22). Goods demand is at least our sales per hour we held a sellable A
  over the last 12 h (A_RATE): SKUs whose buyers take only A read low while we held only B/C. Goods caps x 1.5
  (GOODS_CAP_X, owner): more goods stock and more buying is by design; book scores stock at cost. Goods B now gets
  the 72 h warranty without asking, priced as before (a 3 h trial to win back psyoung82, about 10M a day more
  refunds); more B refunds are expected, not a fault. At 16:30 report whether psyoung82 came back (visits line
  in alerts.md or visit_watch) so the owner can keep or revert it.
- 11-06 16:10 (share_watch restarted; seller comment only). Goods B keeps the 72 h warranty without asking (owner,
  the 13:22 trial ends kept): psyoung82 came back (2 -> 8 -> 24 paid deals here at 12, 13, 14 h) and the 44 B sold
  with it by 15:35 broke once (0.35M refunded). C still gets one only on ask. The share_watch warranty alert now
  watches C only and says so: no need to ask the owner about B's warranty again.
- 11-06 16:40 (owner; Patch 3.5, announced by the market, not yet on its rules page). (1) Buyer ALL takes the forum
  rating itself (0 to 1) in place of its percentile, for all of Round 3; the other parts stay percentiles. (2) A
  dislike can be bought with weight N: {"vote": post, "value": -N, "as": "buyer"} holds N votes on that author and
  costs unit x (N - 1)^2 in all (only the difference when raised), unit = 3% of the account's largest Book, at least
  20,000 (inventory.json dislike_power_unit). It leaves the shop's cash, lowers Book, never comes back, needs 50
  settled deals. The owner does not buy dislikes: if one is ever asked of the shop's cash, say so under 결정 필요.
- 11-06 18:30 (owner; seller restarted). Kiro729 rates every shop by price alone: paid/list 0.700 or under gets 5
  stars, 0.708 or over gets 4. Its goods talks (first message "... Please include a N-hour warranty.") are now
  priced at most 0.695 of list, never under break-even (cost + expected warranty refund); about 1.8M a day of margin
  for +0.005 on the star ranking, by design. These talks log arm "star_cap": lower prices there are not a fault.
  Report Kiro729's mean stars here and its deals over 0.70 of list if the cap stops holding.

- 11-06 19:19 (owner; seller restarted). A goods talk whose ask is within 1% of its floor (TIGHT_GAP; most A talks
  with the arm at 0.0) starts at its last step: the first counter is the floor, called final, and an offer up to 1%
  under the floor closes from the first offer (FLOOR_TOL, never under cost). Prices do not change; the 15-21 won
  steps and "we lowered" on them are gone. Fewer turns and earlier "final price" in these talks are by design.
- 11-06 19:19 (owner). Stale stock (24 h+ unsold): no owner decision needed. Clearance has covered every stale goods
  unit since 11-04 16:50 whenever it was bought, and the C units are already named as a cheaper option in the first
  reply (mention_alt, about 300 times a day; our C asks 0.565 of list against the market's 0.634) and still do not
  sell to us. Book counts them at cost; storage is 1% a day. Report a new cause under 발견 only.
- 11-06 21:00 (owner; manager restarted). The 19:19 stale-stock line above reached the code but not your process:
  the kit restart does not restart you, so the 20:01 report still asked for the clearance decision. From 21:01 the
  line says no decision is needed. The forum process (up since 11-05 07:09) was restarted at 20:11 for the same reason.
- 11-06 23:38 (owner; seller.py, kit restarted). Grade parsing fixed: "a grade C unit" read the article as grade A,
  and "Grade B or C is fine" kept only B, so a buyer who allowed C got A or B with C held (your 22:01 and 23:01
  G5cd4ffbc finding). Now every named grade counts (B or C, B/C, B, C 등급; the letter before "grade" must be a
  capital) and the lowest held of them is offered. 3 h replay: 41 of 516 openings move to C, 19 from A to B (buyer
  said "grade A or B"). 190 goods C units (1.02억 at cost) are held. Check the share of goods openings on C and C deals.

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
- 11-05 07:08 (7897fa5): an order haggles first with a shop whose ask is within our cap, then by surplus (12 h: 29
  orders started over the cap with another shop under it; 69% closed against 96%). The buyer posts stopped at 03:39
  ON PURPOSE for now: totato2203-r3 dislikes almost every post in the market since about 02 h (667 of 689 posts at
  04-06 h, its own excepted), so every kind of ours got a dislike and buyer_forum has none left to post. The owner
  holds it until the bombing fades (from 08:10 no automatic resume, see below). Do not report the
  pause as a fault; report when posting resumes or other posts stop getting dislikes. offers[0] over the cap is a
  record artifact (offers and asks are kept across all shops of an order, the cap is the last shop's).
- 11-05 07:3x (owner: watch the dislike bombing): forum_strategy_checks now carries
  market_dislike_share_by_hour_newest_first (other buyers' posts 10-70 min old in each hour, and how many got a
  dislike; under 2% before 11-05 01 h, 96-97% at 03-07 h). "싫어요 테러 진행" fires while the last hour is 30% or more,
  with the hours it has run (the 13:20 automatic resume was removed at 08:10); "싫어요 테러 잦아듦" fires
  once it falls under 10% after being on, so the owner decides when to resume. "글 멈춤" is left out while it is on:
  the pause is the owner's. Each hour say in one line whether the share is rising, steady or fading.
- 11-05 08:10 (buyer_forum, buyer_manager restarted). The owner stopped buyer posts until the dislike bombing is seen
  to end (buyer_forum.POSTING_PAUSED): there is no automatic resume at 13:20 any more. Report "싫어요 테러 잦아듦"
  with the numbers so the owner can decide; do not ask to restart buyer_forum or report the pause as a fault, and do
  not offer an automatic resume as an option: there is none.
- 11-06 01:33 (owner). The forum drop behind the latest forum alerts is targeted: someone picked only our OLD posts
  and gave each 3 dislikes. It is not about the kinds we post or their content, so do not treat it as a format
  failing, and do not pass the "평균 하락" alert on for it again. Report it only if new posts (under 1 h old) start
  getting dislikes.
- 11-06 10:32 (owner). The market's patch 3.4 (06:00) scores a buyer by Saved = sum over deals of (list - paid) / list,
  divided by (deals + 100); a deal that served nothing counts -1, or 0 if refunded, whatever its price. Goods accept
  now follows it: a grade is paid for only when (1 - p)(1 - price/list) - (p if no warranty) >= 0, p that grade's
  break rate; a receipt with a warranty of 6 h or more counts a break as 0 and the accept asks the kit for that
  warranty. Expect C goods without a warranty above ~46% of the list (before: 65%) to be refused ("grade C, the
  policy accepts ..." or "warranty 0 h ...") and fewer unrefunded breaks; this is intended, not a fault.
  Backtest on our 6,074 deals: 322 refused (C 320), Saved 0.246 -> 0.265.
- 11-06 10:40 (owner). Buyer posts resume (POSTING_PAUSED False) and a disliked post no longer benches its kind:
  patch 3.4 rates the forum by one stance per voting account per author, so the account that dislikes nearly every
  buyer post weighs 1 in all however many posts we write (our forum 0.343 -> 0.915 at 06:00 with the bombing on).
  Posts count 0.1 of the score (ours 345, percentile 0.62). Per-post dislike alerts are off; judge the forum by
  its score and percentile only. Do not pause posts for dislikes. This replaces the 11-05 notes on pausing posts.
- 11-06 10:40 (owner). The buyer now picks shops by expected saved rate (surplus / list price), and min_saved
  (start 0.20, yours to move in 0.10-0.30) caps the price (food list x (1 - min_saved); goods the price where an A
  unit under a warranty still saves it) and is the goods accept threshold. Expect fewer deals (backtest at 0.20:
  -24%) and a higher Saved (0.246 -> about 0.29); that drop in deals is intended. Weigh Saved (0.5) against deals
  (0.1) by their board percentiles in our_buyer_score_now.
- 11-06 10:48 (owner). Dislike alerts on our buyer posts (code, "싫어요 경보"): while the bombing is on (30%+ of
  other buyers' posts disliked in the last hour) only a post of ours with 2+ dislikes is raised, since the bomber's
  one dislike weighs 1 in all; with no bombing any dislike is raised. Put it under 결정 필요; posts are not paused
  by code, the owner decides. Say each hour whether the bombing is on, fading or over.
- 11-06 10:54 (owner). min_saved range is now 0.15-0.35 (was 0.10-0.30). Your 10:42 move to 0.25 matches the owner's
  plan. The owner's projection on the last 24 h of our deals (lost deals dropped, lower closing prices not counted):
  final Saved about 0.278 at 0.20, 0.290 at 0.25, 0.298 at 0.30, 0.289 at 0.35; within 24 h every value lands near
  0.255, so judge a move over a day, not an hour. Saved moves slowly because 6,100 past deals hold the mean.
- 11-06 11:12 (owner). open_under 0.80 -> 0.75 (policy version 5) as a trial of a lower first offer; min_saved 0.25
  and raise 0.40 unchanged. Each window now has "haggle": compare policy 5 with 4 on paid_over_first_ask (baseline
  last hour 0.927), deals_at_our_first_offer (5 of 84), lost_with_an_ask_under_cap (11) and deals per hour. Give it
  two hours before you move it; after that open_under is yours within its range, one of open_under/raise at a time.
- 11-06 11:32 (owner). No likes from us (shop or buyer): our likes raised rivals' forum (we had liked shaun0927 8
  times, BJEon01 and SungJinho once, all now above us). Our forum fell 0.915 -> 0.892 between 11:10 and 11:20, the only
  buyer that moved, likely one new dislike stance (voters are not public). Leave "upvote" empty; never dislike.
- 11-06 13:10 (buyer_manager restarted; market_buyer runs fresh per order). The haggle names another shop's quote only
  for the same SKU. A quote round often asks shops for different SKUs of the category (1,046 of 1,875 rounds), and
  "X quoted me Y for this" used a cheaper SKU's price, which was untrue. orders_last_hour now shows sku and quote_skus.
- 11-06 13:10 (owner, buyer_manager restarted). Dislikes are no longer watched (buyer_manager.DISLIKES_OFF): the
  account that dislikes every buyer post is known. No "싫어요 경보", no bombing line, no storm share in the record. Do
  not report dislikes or the bombing.
- 11-06 13:25 (owner, buyer_forum and buyer_manager restarted). Buyer posts stop again (buyer_forum.POSTING_PAUSED):
  from the 10:40 resume our forum fell 0.915 -> 0.889 (percentile 0.99 -> 0.94) while it held through the 06:09-10:30
  pause; each new post lets one more account open a dislike stance, and forum (0.3) loses more than the post count
  (0.1) gains. The pause is intended: do not report it, do not propose resuming without a new reason in the record.
- 11-06 15:44 (owner, buyer_forum restarted). Buyer posts resume (buyer_forum.POSTING_PAUSED = False, every 305 s as
  before). Through the 13:12-15:30 pause our forum held at 0.887 while every buyer that kept posting rose (+0.005 to
  +0.023 in 2 h) and only the non-posters held: voters vote on the newest 100 posts (~14 min), so a paused buyer gets
  no new like stances. Goal: forum back to 0.89+. The owner will stop posts again if ours drops 0.001 twice while the
  others rise; report the forum value each hour, but do not propose pausing on your own.
- 11-06 16:40 (owner; Patch 3.5, announced by the market, not yet on its rules page; this note holds over the
  prompt where they disagree). (1) Buyer ALL = 0.5 Saved + 0.3 forum + 0.1 deals + 0.1 posts, where forum is now
  the rating itself (Wilson lower bound of the like share, 0 to 1), not its percentile, for all of Round 3. Saved,
  deals and posts stay percentiles. "percentile_among_buyers.forum" and the "순위 하락: forum 백분위" alert no
  longer score: judge the forum by its value, 0.3 x the change (0.889 -> 0.887 costs 0.0006). Top buyers sit at
  0.87-0.91, so the rank no longer swings on a small gap, but the value itself still moves: with no dislikes
  0.889 is a like weight of about 31, and one more dislike weight takes the Wilson bound to about 0.84 (0.3 x 0.05
  = 0.015 of Buyer ALL). (2) A dislike can be bought with weight N on one author: unit x (N - 1)^2 in all, unit = 3%
  of the voter's own account's largest Book (at least 20,000), paid from its shop's cash, needs 50 settled deals.
  So a rival with a small Book buys weight cheaply (Book 1M: unit 30,000, N = 5 costs 480,000); ours costs about
  19M per unit. Report it if our forum value drops by more than 0.01 within an hour. We do not buy
  or cast dislikes (owner); leave "upvote" empty as before.
- 11-06 16:44 (review.py, buyer; no restart, the kit runs it per deal). Our buyer's stars now follow its own score,
  the same rubric for every shop: expected saved rate s = 1 - price/list, less break rate(grade) x price/list for
  goods without a warranty, against min_saved T read on every deal: 5 s >= T+0.10, 4 s >= T, 3 s >= T-0.10, 2 s >= 0,
  1 below 0 or a false claim / unusable unit. Raising min_saved makes the stars stricter at once (T 0.20 mean 4.13,
  0.25 3.71, 0.30 3.20 on the last 2,000 purchases); weigh that when you move it. Lower stars to other shops are by
  design, not a fault.
- 11-06 18:44 (owner; review.py scp only). Goods bought without a warranty: the stars above, 2 lower, never under 2
  (1 stays for a false claim, an unusable unit or a price over list), and the review says why ("No warranty, so N
  stars off ..."). Food is not touched. The buyer still buys without a warranty: accept and pricing are unchanged.
  Since patch 3.4 a break without a refund counts -1 in Saved. yms979 sells all goods without one, so most of our
  reviews there become 2 stars (replay to 11-17: its stars_rank -0.028); known and intended. Not a fault.
- 11-06 19:12 (owner; buyer_manager.py scp only). The forum check "순위 하락" (percentile 0.15 under its 6 h best) is
  gone: since patch 3.5 the forum rating scores as its value, so a rival's rise costs us nothing (17:05 it fired at
  percentile 0.83 while the value rose 0.885 -> 0.891). It is now "값 하락": the value more than 0.01 under the board
  an hour ago (12 h replay: fires once, 10:40 0.915 -> 0.891). The percentile is shown for reference only.
- 11-06 21:05 (owner; buyer_manager restarted). The 19:12 "값 하락" forum check above was in the file but not in your
  process until this restart; reports 19:05 and 20:05 still ran the old percentile check.
- 11-06 22:22 (owner; market_buyer.py, GRADE_OPENING). The goods opening now names the grades we take, at the
  expected price, as the accept policy decides: A always, B/C when they save min_saved there, and those that do only
  under a warranty "only with a warranty of at least 6 hours". Before, openings named no grade, shops quoted their
  cheapest unit (often C), and we refused the receipt after the haggle: 3 h to 22:10, 153 of 334 orders had a receipt
  refused for its grade (21% of those closed, 62% of the rest), 100 at moss-and-oak. This is our opening, not the shops'
  fault. Judge it on refused receipts (doomed) per order and paid deals per hour, against the hour before.
