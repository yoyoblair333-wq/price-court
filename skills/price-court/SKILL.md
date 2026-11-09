---
name: price-court
description: Strike, escrow and settle a plain-English price deal between two agents with PriceCourt, a GenLayer Intelligent Contract that parses the terms under validator consensus, holds both stakes, settles on Pyth price evidence and writes the verdict back as on-chain reputation. Use when two agents want to wager or hedge on whether BTC/USD, ETH/USD or SOL/USD will be above or below a price at a given UTC time, or when an agent needs to check a counterparty's PriceCourt win/loss record before dealing. Works alongside the Internet Court skill.
---

# PriceCourt

PriceCourt is an Internet Court partner integration: **Pyth** supplies the evidence,
**GenLayer** supplies the judgment. Two agents agree terms in plain English, both lock an
equal stake, and at the settlement time anyone calls `settle`. The pot goes to the winner
and both addresses' reputation records are updated.

## Where it sits in the Internet Court stack

| Layer | What PriceCourt does |
|---|---|
| 3 Contracts & obligations | `propose_deal` turns free text into fixed, structured terms (feed, UTC time, comparator, threshold, who wins if true) under validator consensus. The counterparty accepts those exact structured terms. |
| 4 Payment & escrow | Both stakes are held by the contract; the pot is released only by a verdict or refunded. |
| 5 Execution / evidence | Pyth price for a whitelisted feed, read by every validator from the Pyth Solana Receiver's verified on-chain price account. |
| 6 Verification & disputes | Validators must agree on the price (0.25% tolerance) **and** the resulting verdict; the payout and the per-address reputation record (`settled`, `won`, `lost`, `refunded`) follow deterministically. |

If the Internet Court entry skill is installed, this deal runs at the **Adjudicated**
trust level: funds are locked (Guarded) and an independent, consensus verdict decides
the outcome and writes it back to reputation.

## Boundaries (read before acting)

- Never propose or accept with the user's funds until the user has approved the stake
  amount, the counterparty address and the terms. Report what you observe first.
- Before accepting, read the deal with `deal <id>` and check the **structured** `terms`
  and `summary`, not just `terms_text`. Those structured terms are what settles the deal.
- Supported feeds: `BTC/USD`, `ETH/USD`, `SOL/USD` only. Stakes are equal on both sides.
- The settlement time must be an explicit instant with a timezone (use UTC, `...Z`), at
  least 60 seconds after the proposal. Acceptance must happen before it.
- A deal settles on the Pyth price published inside
  `[settle_at, settle_at + price_window_seconds]` (see `config`). Call `settle` promptly
  after `settle_at`; if nobody settles inside the window, `refund` returns both stakes.

## Setup

```bash
git clone <this repo> pricecourt && cd pricecourt
python -m venv .venv && . .venv/bin/activate
pip install genlayer-py==0.16.3
export PRICECOURT_CONTRACT=<contract address>   # see docs/LIVE_RUN.md, or deploy your own
```

The reference deployment lives on GenLayer **studionet** (chain 61999,
`https://studio.genlayer.com/api`) at `0x1A30EcE83aE60CbE84c00a97c0DE069B24B85a13`. Studio is a shared sandbox that is periodically
reset; if the address in `docs/LIVE_RUN.md` no longer answers `config`, deploy a fresh
copy (step 1b) and use that.

**Payouts on studionet.** Studio currently drops value transfers from a contract to an
externally-owned address (the verdict and reputation are still recorded, but the pot is
not credited). Contract-to-contract transfers work. On studionet, have each agent act
through its own `contracts/agent_wallet.py` wallet so the pot arrives;
`python scripts/live_demo.py` runs a whole deal that way. Tell the user about this before
staking from a plain account on studionet.

## Workflow

All commands are `python scripts/pricecourt.py ...`; each write prints its tx hash on
stderr and the resulting contract state as JSON on stdout.

1. **Accounts.** On studionet, create and fund throwaway test accounts (free, no captcha):
   `account new agent-a` and `account new agent-b`. Keys stay in `./.pricecourt/accounts/`.
   1b. *(Only if needed)* `deploy --as agent-a --window 600`.
2. **Check the counterparty.** `reputation <address>` shows settled / won / lost /
   refunded counts and amounts from earlier PriceCourt verdicts.
3. **Propose.** Write the terms so they name one feed, one threshold, one UTC instant and
   who receives the pot each way. Refer to yourself as "Agent A" and the counterparty as
   "Agent B":
   ```bash
   python scripts/pricecourt.py propose --as agent-a --counterparty <B address> --stake 5 \
     --terms "Agent A pays Agent B the pot if BTC/USD is above 95,000 at 2026-11-10T00:00:00Z, otherwise B pays A."
   ```
   If the terms are ambiguous, unsupported, or validators cannot agree on one reading,
   the transaction creates no deal (the CLI says so). Rephrase and retry.
4. **Counterparty reviews.** `deal <id>` and confirm `terms` / `summary` match the intent:
   `{"feed": "BTC/USD", "settle_at_iso": "...", "comparator": ">", "threshold": "95000",
   "winner_if_true": "counterparty"}`. To reject: `decline --as agent-b <id>`.
5. **Accept.** `accept --as agent-b <id>` (the matching stake is read from the deal).
   Until then the proposer may withdraw with `cancel --as agent-a <id>`.
6. **Settle.** `settle --as <anyone> <id> --wait` sleeps until `settle_at`, then submits
   `settle` and retries every 15 s inside the window while the result is "price not yet
   published" or "unavailable". The deal JSON shows `status: "settled"`, the `evidence`
   (Pyth price, expo, confidence, publish time), `condition_met` and `winner`.
7. **Fallback.** If the window closes without a verdict: `refund --as <anyone> <id>`.
8. **Record.** `reputation <address>` for each party now includes this verdict.

## Using the contract without the CLI

Methods (GenLayer `genlayer_py` / `genlayer-js` / `genlayer` CLI all work):

| Method | Kind | Notes |
|---|---|---|
| `propose_deal(counterparty: str, terms_text: str)` | write, payable | value = your stake |
| `accept_deal(deal_id: str)` | write, payable | value must equal the proposer's stake |
| `settle(deal_id: str)` | write | anyone; only inside the price window |
| `cancel_proposal(deal_id)` / `decline_deal(deal_id)` | write | proposer / counterparty, before acceptance |
| `refund_unsettled(deal_id)` | write | anyone, after the window closes unsettled |
| `get_deal(deal_id)`, `list_deals(offset, limit)`, `get_reputation(address)`, `get_config()` | view | |

```python
from genlayer_py import create_client, create_account
from genlayer_py.chains import studionet
c = create_client(chain=studionet, account=create_account(PRIVATE_KEY))
tx = c.write_contract(address=ADDR, function_name="accept_deal", args=["1"], value=STAKE_WEI)
c.wait_for_transaction_receipt(transaction_hash=tx)
print(c.read_contract(address=ADDR, function_name="get_deal", args=["1"]))
```

A transaction reaching ACCEPTED does not by itself mean the call succeeded; always read
`get_deal` afterwards and act on its `status` and `last_error`.
