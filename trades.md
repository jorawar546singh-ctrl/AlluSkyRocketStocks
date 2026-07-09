
---

# Trade Journal — Week of June 15–18, 2026

## ZEEL (IN) — STOPPED OUT — 2026-06-10
Entry ₹112.27 × 50 · Stop ₹104.41 (GTT) · Exit ₹102.92 (market fill, slipped ₹1.43)
P&L: −₹467.50. Lesson: GTT market orders slip on NSE; budget ~1.5% slippage into sizing.

## ACUTAAS (IN) — STOPPED OUT — 2026-06-15
Entry ₹3392.20 · Stop ₹3095.32 · Exit ₹3095.32
P&L: −₹296.88. Stop hit at the planned level. Clean exit, no slippage.

## RXT (US) — STOPPED OUT — 2026-06-16
Entry $6.785 × 132 · Stop $6.16 · Exit $6.16 (stop-limit sell, same day)
P&L: −$82.50. Round-tripped in one day — bought and stopped same session.
Lesson: a same-day reversal means the breakout failed immediately; nothing to do
but take the stop. This is the cost of doing business, not a mistake.

## MAAS (US) — EXITED BREAKEVEN — 2026-06-18
Entry $16.0999 × 50 · Exit $16.10
P&L: ~$0.00. Got out flat. No initial stop was recorded at entry —
fix going forward: set the stop before/at entry, every time.

## SRAD (US) — STOPPED OUT — 2026-06-18
Entry $15.98 × 22 · Stop $14.76 · Exit $14.77
P&L: −$26.62. Held ~9 days, stopped near the planned level (essentially no slippage).
Smallest loss of the week — the tight stop did its job.

## MRNA (US) — STOPPED OUT — entered 2026-06-18, exited 2026-06-22
Entry $63.13 × 18 · Stop $58.71 (set in Wealthsimple at entry) · Exit $58.71
P&L: -$79.56.
Lesson (important): the stop-limit order placed at entry was never moved as
price ran. The system kept tracking this position as "open" in the DB and
recalculated a trailing stop against live price every day (it briefly showed
current_stop $73.62 / running_high $81.80) - but that was fiction, since the
real position closed at the broker on 6/22 at the original, un-trailed stop.
The dashboard/digest reported this as a healthy open holding for ~2 weeks
after it had actually already been stopped out for a loss.
Fix going forward: when a stop-limit sell fills in Wealthsimple, mark the
position closed in the system the same day - don't let it keep "trailing"
a position that's already flat. There's no broker sync; this step is manual
and was missed here.

## BLZE (US) — STOPPED OUT — entered 2026-06-29, exited 2026-07-09
Entry $15.1299 × 71 · Exit $17.39 (stop-limit sell - stop moved up manually
from the original $13.60 as price ran)
P&L: +$160.47 (+14.9%).
Winner. This was the position flagged as a system exception - Darvas box low
predated a gap move, box-derived stop was structurally stale/too wide. The
manual hard stop ($13.60 at entry, moved up as price ran) was the right call
here instead of trusting the broken box level.

### Week summary
Closed 5 trades: 4 losses + 1 breakeven. US realized ≈ −$109; IN realized ≈ −₹764.
A losing week mechanically — every loss stayed small and near-plan, which is the
system working as designed during an unfavorable stretch. One position at a time
held except where overlap occurred. MRNA carries forward.
