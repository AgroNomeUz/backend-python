# Produce marketplace — design

Small farms sell vegetables, fruit, meat and the like to businesses that need
them on a schedule (salad makers, canteens, restaurants). Both one-off
purchases and long-term supply contracts are supported, and the platform holds
the money between the two sides.

This sits beside the machinery marketplace, not on top of it: `listings`,
`inquiries` and `deals` are built around a single machine and stay as they are.

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Where it lives | `marketplace/` and `billing/` | The two apps the README already reserved. `billing` knows nothing about produce, so rentals can use the same ledger later. |
| Order vs delivery | **One model.** A contract generates one `Order` per scheduled delivery | One state machine, one escrow path, one dispute flow — no second system for recurring orders |
| Who holds money | A licensed provider, in the real deployment. Our ledger records and instructs | Holding customer funds ourselves needs a licence. **Check with a lawyer before going live.** |
| Provider integration | `billing/gateway.py` is the seam; `MockGateway` behind it | Same approach as `api/sms.py`. Real Payme/Click/Uzum is a new class there |
| Mock provider | **Off unless `BILLING_MOCK_ENABLED=true`**, and never with `ENVIRONMENT=production` | The mock can mint money, so forgetting a setting must leave it off. Same rule as `OTP_ECHO_CODES` |
| Commission | A percentage, charged to the seller, on released goods value only; snapshotted on each hold | Rejected goods earn nothing. A later rate change never rewrites an open order |
| Commission rate | **Placeholder:** 1.5% spot, 1.0% contract (`BILLING_COMMISSION_RATE`, `BILLING_CONTRACT_COMMISSION_RATE`) | Set the real numbers once provider fees are known |
| Cash on delivery | **Not supported** | The whole point of holding funds is that deals stay on the platform |
| Wallet | One per organization: top-ups, earnings and refunds all land in it | An org can be both buyer and seller. Every hold is funded from a wallet, so a late payment can never lose money |
| Currency | UZS only, not client-supplied | No cross-currency bugs until we actually need them |

## The ledger

Double-entry, append-only. Every money movement is a `LedgerTransaction` whose
`LedgerEntry` rows sum to zero, enforced by a deferred Postgres constraint
trigger, and a second trigger refuses any `UPDATE` or `DELETE`. A balance is
always `SUM(entries)`; there is no balance column to drift.

| Account | One per | Positive balance means |
| --- | --- | --- |
| `wallet` | organization | Spendable money the organization has with us |
| `escrow` | platform | Money held for open orders |
| `payouts_in_transit` | platform | Withdrawals sent to the provider, not yet confirmed |
| `commission` | platform | Revenue |
| `gateway` | platform | The other side of the world: goes negative as money comes in |

```
top-up / card payment   gateway  -> wallet(buyer)
hold                    wallet(buyer) -> escrow
release                 escrow -> wallet(seller) + commission
refund                  escrow -> wallet(buyer)
payout requested        wallet(seller) -> payouts_in_transit
payout confirmed        payouts_in_transit -> gateway
payout failed           payouts_in_transit -> wallet(seller)
```

`python manage.py reconcile_ledger` re-derives everything from the entries and
reports any disagreement. It also lists what only a person can settle: payouts
stuck in `requested` or `processing`, and provider events we refused or ignored,
such as a failed payout later reported paid. The `scheduler` service runs it
daily and logs `SCHEDULER FAILED` on a non-zero exit, so alert on that.

Payment events are idempotent: `WebhookEvent` is unique per provider and event
id, and a replay is a no-op. An event about something we cannot find yet answers
503 and leaves no row, so the provider's retry is processed for real. A payment
that fails and later succeeds is credited; one that succeeded and later "fails"
is **not** reversed automatically — that is a chargeback.

A payout whose provider call times out stays `requested` with the money in
transit. It may or may not have been sent, so retrying could pay it twice and
returning it could give away money that already left. A real gateway may only
raise `GatewayError` when it is certain nothing happened.

## An order's life

```
                 ┌─ declined / expired
pending_confirmation ─ confirm ─> awaiting_payment ─ pay ─> paid ─ dispatch ─> dispatched ─ deliver ─> delivered
                                        │                    │                     │                      │
                                  cancelled/expired      cancelled             accept / reject        accept / reject
                                                         (refund)                  │                      │
                                                                                   └──────────┬───────────┘
                                                        full acceptance  ─────────────────> settled
                                                        any rejection    ─> disputed ─ concede / resolve ─> settled
```

- **Stock** is reserved at placement with a conditional `UPDATE`, so two buyers
  cannot both take the last 50 kg, and returned on decline, cancel or expiry.
- **Price** is copied from the offer, never from the request. A client may send
  the price it was shown, and gets a 409 if the seller has since changed it.
- **The seller confirms before the buyer pays**, so a decline needs no refund.
- **A purchase request** closes when paid orders from its quotes cover the
  quantity, and any quote still waiting is then rejected. Taking a quote does
  not close it, so a buyer can take more quotes than the request needs. An order
  that ends unpaid gives its quote back to `submitted`, so it can be taken again,
  unless the request has closed, the quote is past `valid_until`, or the seller
  has sent a newer one. A refunded order keeps its quote `accepted` and leaves
  the request closed. `quantity_covered` counts accepted quotes, paid or not.
- **Regulated goods** need a verified seller at every way into a sale:
  publishing, quoting, taking a quote, proposing a contract. Losing verification
  takes such an offer off the market at once, feed and public profile included,
  and regaining it brings the offer back. The seller still sees it as `active`
  in `/offers/my`. Deliveries already confirmed under a signed contract carry
  on. Nothing checks again after those four points, so an order already placed
  can still be confirmed, and so can the deliveries a signed contract goes on
  generating.
- **Partial acceptance** releases the accepted share at once and holds the rest
  in dispute. The seller can concede (refund), or platform staff split it.
- **Deadlines** mean every order reaches an end even if both sides go quiet, so
  money can never be left in escrow with nobody able to move it.
  `python manage.py run_market_jobs` applies them. The `scheduler` service runs
  it every five minutes, and it exits non-zero if any order failed.

| If nobody acts on | What happens | Counted against |
| --- | --- | --- |
| `pending_confirmation`, past `confirm_by` | Expires, stock returns | Nobody: placing an order is free, so counting this or a decline would let a rival wreck a seller's score |
| `awaiting_payment`, past `pay_by` | Expires, stock returns | Buyer |
| `paid`, over `ORDER_DISPATCH_GRACE_DAYS` after the delivery date | Refunded | Seller |
| `dispatched`, once the delivery day and the window are over | Accepted, seller paid | Nobody |
| `delivered`, once the delivery day and the window are over, or `ORDER_ACCEPTANCE_HOURS` after delivery if that is later | Accepted, seller paid | Nobody |

A `disputed` order is the one state that waits for a person, in the staff queue.

Marking an order delivered early never brings the buyer's deadline forward.
Otherwise a seller could be paid days before anything was due.

## Contracts

`SupplyContract` holds the parties; `ContractTerms` holds one immutable version
of what was agreed. Either side proposes, and **only the other side can accept**.
An answer names the version it read, so a proposal replaced at the last moment
is a 409 rather than a signature.
After signing, a change is a new version that also needs the other side, and
it applies only to deliveries on or after its `start_date`.

Deliveries already generated are brought into line when a change is accepted.
One the seller has not confirmed takes the new terms. One already confirmed
keeps its price, since both sides committed to it. One that is no longer on the
schedule is called off and refunded, unless it has already been dispatched.

The schedule is weekdays plus `every_n_weeks`, which covers daily, weekly,
twice-weekly and fortnightly. `run_market_jobs` generates one order per delivery
for the next `CONTRACT_GENERATION_HORIZON_DAYS` days.

Signing a contract is a standing instruction to pay it: when the seller confirms
a delivery the money is held from the buyer's wallet. If the wallet is short the
order waits in `awaiting_payment`, like any other order. Contract deliveries
draw on the contract, not on an offer's stock. The seller may confirm a
different quantity within the agreed `tolerance_percent`.

A contract has one `Deal`, created by its first delivery that pays the seller.
Each later one moves `completed_at`, so the 30-day review window runs from the
latest delivery rather than the first.

Termination takes effect after the agreed `notice_days`, so a factory is not
left without supply overnight. Deliveries after that date are cancelled and
refunded.

## Permissions

| Code | Gates |
| --- | --- |
| `market.manage` / `market.view` | Offers, purchase requests, quotes, order handling |
| `contracts.manage` / `contracts.view` | Proposing, signing, amending and terminating contracts. Binding the organization for months is a different level of trust from answering an order |
| `billing.manage` / `billing.view` | Paying, topping up, removing payout destinations, withdrawals; reading the wallet |

New codes are not backfilled onto existing staff: nobody gains access to money
by deploying this. Nobody can grant a code they lack, or take over the account
of someone who holds one. Adding a payout destination also needs a one-time code, and a
new destination cannot receive money for `BILLING_PAYOUT_COOLDOWN_HOURS`,
because a changed payout account is the classic way to steal a seller's money.
Only the owner can add one, with a code sent to their own phone: `billing.manage`
withdraws to an account, it does not add one. Staff cannot move that number and
the owner moves it only with proof of the old one, so a stolen session of any
rank cannot confirm an account. A code is never relayed between people, because
it would also log its reader in as the owner.

## Not built yet

- Real provider integration, and whether its refunds go back to the card rather
  than to the wallet
- Provider fees in the ledger
- Indexed or banded contract prices (a price change is an amendment for now)
- Photo evidence on disputes
- Pooling several farms behind one contract offer. Several farms can already
  each cover part of one purchase request
- Logistics, invoicing and tax documents, and deposits for far-forward harvests
- Notifications: nothing tells a seller a new order arrived yet
