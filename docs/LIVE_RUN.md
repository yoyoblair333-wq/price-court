# Live run on GenLayer studionet (2026-11-09)

Network: **GenLayer studionet**, chain id 61999, RPC `https://studio.genlayer.com/api`
(hosted Studio, 5 validators, server-side LLM and web access).
Accounts are throwaway keys funded for free with Studio's `sim_fundAccount`.

**PriceCourt contract: `0x1A30EcE83aE60CbE84c00a97c0DE069B24B85a13`**
(deployed with `price_window_seconds = 600`).

The final on-chain state (both deal records, reputation, balances) as read after the
runs is in [`live_state.json`](live_state.json). Every transaction below was checked
with `genlayer_py`'s `get_transaction`. "Votes"
counts validator votes, where `idle` is a validator that did not vote in that round.

## Deploy

| Step | Tx hash | Status / result |
|---|---|---|
| deploy `contracts/price_court.py` (arg `600`) | `0xf1b0765beb6787e568ea944d4d5e462a75fa76060d0e781da8eb92b665ae6c79` | FINALIZED / MAJORITY_AGREE (3 agree, 2 idle) |

## Run 1: two plain accounts (EOAs), BTC/USD, counterparty wins

- Agent A (proposer): `0xCacEEB6d30dA6aB2DeAd2eB970D876afeA4CB902`
- Agent B (counterparty): `0x47a9fa452B4AEe743AabE5F4F292d692Ba19a584`
- Terms text: *"Agent A pays Agent B the pot if BTC/USD is above 74,000 at
  2026-11-09T16:57:00Z, otherwise B pays A."*
- Structured terms fixed by consensus at proposal:
  `{"feed": "BTC/USD", "settle_at_iso": "2026-11-09T16:57:00Z", "comparator": ">", "threshold": "74000", "winner_if_true": "counterparty"}`
- Stake: 5 GEN each, so the pot is 10 GEN.

| Step | Tx hash | Status / result | What it shows |
|---|---|---|---|
| `propose_deal` (A, 5 GEN) | `0x7bca185dcc61e479f1a5442212c3f14429cbad0b0b960239e077b35e562494bf` | FINALIZED / MAJORITY_AGREE (3 agree, 2 idle), `value_credited: true` | The LLM terms parse reached consensus; deal 1 was created. |
| `accept_deal` (B, 5 GEN) | `0x69b5b4d84ee64e521c7782ac06b12abfa4e9ad8c0761e26c0b9f423763759bf1` | FINALIZED / MAJORITY_AGREE, `value_credited: true` | Both stakes are in escrow; status `accepted`. |
| `settle` attempt 1 (16:57:0x) | `0x4951958e84d106a2c76b03038f3ce3e61e93fe8c3e361e4012d261a4ca08fc12` | FINALIZED / MAJORITY_AGREE | **Fail-closed working live.** The `finalized` Pyth account still held a pre-T price, so `last_error` = "latest Pyth price predates the settlement time" and nothing moved. |
| `settle` attempt 2 | `0xe7721a5f1ae5d1270bc337c7a54f3a4fe43b006d0dcf417471bb8dc8199a9b49` | FINALIZED / MAJORITY_AGREE (3 agree, 2 idle) | Verdict. Pyth BTC/USD **74,962.3431101** (expo -8, conf 47.75688990), published at unix `1794243441` = 16:57:21Z, 21 s after T. `74962.34 > 74000` is true, so **B wins**. |
| triggered payout: contract to B, 10 GEN | `0xf512993869d3d0c09473d8dceafa422f5285ec077fdbe4a1e603157baaa8b0c5` | FINALIZED / **NO_MAJORITY**, `value_credited: false`, leader `contract_not_found_handler` | **Studio limitation (see below).** The 10 GEN left the contract (its balance is 0) but was not credited to B's EOA (B is still at 95 GEN). |

Reputation after run 1: A has `settled 1, won 0, lost 1`, B has `settled 1, won 1, lost 0, win_rate_bps 10000`.

## Run 2: agents acting through AgentWallet contracts, ETH/USD, proposer wins

This run uses `scripts/live_demo.py` with `--contract` set to the same PriceCourt, so it
is deal 2 on that contract.

- Agent A key `0xf0fEAAF929a439e0856aA8719e7DF2CC98c03b91`, which owns wallet A **`0x087487f828e8D7F9659e2F2D9eB627120C977325`** (the proposer)
- Agent B key `0x839a895f64F0E7Bd68c40403031D1dBD3B1E66bd`, which owns wallet B **`0xE5E9A71EC0EC57C6538cfa11ba611ef65774f83C`** (the counterparty)
- Terms text: *"Agent A pays Agent B the pot if ETH/USD is below 2,000 at
  2026-11-09T17:23:00Z, otherwise B pays A."*
- Structured terms: `{"feed": "ETH/USD", "settle_at_iso": "2026-11-09T17:23:00Z", "comparator": "<", "threshold": "2000", "winner_if_true": "counterparty"}`
- Stake: 5 GEN each.

| Step | Tx hash | Status / result |
|---|---|---|
| deploy AgentWallet A | `0x4e8160b597c38cacf41391945cd4e86516c259658348550a13900bc752cc5f16` | FINALIZED / MAJORITY_AGREE |
| deploy AgentWallet B | `0xbd0237963cb004f1344baf6357c0fa3dcb907487bd8ce598cacf2c9a28e3b6ef` | FINALIZED / MAJORITY_AGREE |
| A deposits 10 GEN into wallet A | `0x15288f7c0b673a10b8bfecd8df6122f76c556dbc5568920d0fb837e6c6a0ed01` | FINALIZED / MAJORITY_AGREE |
| B deposits 10 GEN into wallet B | `0xae982de72a8ffb0094f22d457c0ee1e60dd005f832bec2c7a3d1c266a6480345` | FINALIZED / MAJORITY_AGREE (5 agree) |
| wallet A `call(propose_deal)` | `0xf13f096dab344b1777eb77c304c89fdad15a88cfb09b8083bc498c53ad140c3e` | FINALIZED / MAJORITY_AGREE |
| triggered `propose_deal` (5 GEN), LLM parse | `0x32d419cf8364612aa14eba596993e323ec7b0acaa08bfaa72c641a84f7cdfbdf` | FINALIZED / MAJORITY_AGREE, `value_credited: true` |
| wallet B `call(accept_deal)` | `0xac1398730595717d7cc53db5b96518c5f727c67e2b8a7425c25b6e422aa65436` | FINALIZED / MAJORITY_AGREE |
| triggered `accept_deal` (5 GEN) | `0x0c403d381249c7ccf0b5000e4bf120ed0e9b8c120af7f3936ef78377dae1db11` | FINALIZED / MAJORITY_AGREE, `value_credited: true` |
| `settle` attempt 1 | `0x8045b4eea071dcad3319a92c702b0103172876ae3109d20b4e2023df88fbfa3f` | FINALIZED / MAJORITY_AGREE. Pre-T price, so nothing moved and `last_error` was recorded. |
| `settle` attempt 2 | `0x9563d690e23c19a746343e84e3255052dda54a81e5e40ca348735ccc127e7664` | FINALIZED / MAJORITY_AGREE (3 agree, 2 idle). Verdict: Pyth ETH/USD **2,124.61939107** published at `1794245165` = 17:26:05Z, 185 s after T and inside the 600 s window. `2124.62 < 2000` is false, so **wallet A wins**. |
| triggered payout: contract to wallet A, 10 GEN | `0x83b9d2c2ae6bdf23b194c774d4a010074354b4baccbfc2c312f65a86669574d9` | FINALIZED / **MAJORITY_AGREE**, `value_credited: true` |

Balances:

| | before settle | after payout |
|---|---|---|
| wallet A | 5 GEN | **15 GEN** (+10 GEN pot) |
| wallet B | 5 GEN | 5 GEN |
| PriceCourt | 10 GEN | 0 |

Reputation after run 2: wallet A has `settled 1, won 1, amount_won 5 GEN`, wallet B has
`settled 1, lost 1, amount_lost 5 GEN`.

Operational note: between the two settle attempts, the Studio RPC answered one
`eth_sendRawTransaction` with an HTTP 502 page, which stopped the demo script before
its retry. Settle attempt 2 was submitted with `scripts/pricecourt.py settle 2 --wait`
by a third account. Both scripts now retry transient submission failures and start
settling 30 s after T.

## Findings about the environment, reproduced live

### 1. Pyth Hermes and Benchmarks require an API key (HTTP 401)

These URLs were fetched by GenLayer validators through a probe contract on studionet
(`0x83D6cE26Df03049De2dddbbe7319d8A576A4632f`, `0x8475EC5b2E1Fd0bC2c65Ef3Bbb3077A52D7c42B9`):

| URL | Result |
|---|---|
| `https://benchmarks.pyth.network/v1/updates/price/1762646400?ids=e62df6…5b43&parsed=true` | `401 unauthorized` |
| `https://hermes.pyth.network/v2/updates/price/1762646400?ids[]=e62df6…5b43&parsed=true` | `401 unauthorized` |
| `https://hermes.pyth.network/v2/updates/price/latest?ids[]=…` | `401 unauthorized` (`web.render`: `WEBPAGE_LOAD_FAILED`, status 401) |
| `https://api.github.com/zen` (control) | `200` |

Pyth's documentation states that the Core upgrade completed on Aug 26, 2026 and that
Hermes now requires an API key, sent as `Authorization: Bearer`. A key in contract
storage or calldata would be public, so PriceCourt reads Pyth's on-chain Solana
receiver accounts instead (see the README). The same probe confirmed that
`getAccountInfo` on `api.mainnet-beta.solana.com` and `solana-rpc.publicnode.com`
returns the BTC/ETH/SOL `PriceUpdateV2` accounts. Each is owned by
`rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ`, is fully verified, carries the
whitelisted feed ids, and has a price only seconds old.

### 2. studionet drops contract-to-EOA value transfers

Probe contracts were deployed: sender `0xDEE01ab2d18a9738429C71Ea0a924974982Ea3B1`,
receiver `0xB7F3AB1592F3A9412124f462f379F3CD89BAF130`, and a second pair with sender
`0x4D7Aa12C855CD9266aA6a3DB9c9369A91Fb231D6` and a receiver with no receive hook,
`0xA1Aa64B8B4a03B0d5D40f168905c6072D32cb3b8`.

| Transfer emitted by a contract | Triggered tx | Result |
|---|---|---|
| `emit_transfer` to a fresh EOA | `0x4a516b0bb040efea0b7c47d440a5c0b732f4bf4b0d7ced9d09634e4b3c43c78b` | NO_MAJORITY, `value_credited: false`, leader `contract_not_found_handler`; the sender was debited |
| `emit_transfer` to a contract with `__receive__` | `0x581feb220c5ccbb0f330faea916eab08edffa5e1bc7680abb48e3c3153557741` | MAJORITY_AGREE, credited |
| `emit(value=…).take()` to a contract | `0xaf671f4437855deea6bf053ef993d6c5c5dc2e38c0e08c1d3377307e1c9bd759` | MAJORITY_AGREE, credited |
| `emit_transfer` to a contract **without** a receive hook | `0xc8d37514264c6ba0236e0b28ffbaa2719f15182c863dac4a7ff260ff7299003c` | MAJORITY_AGREE, credited |

This is why run 1's payout to an EOA was lost and run 2's payout to a wallet contract
arrived. PriceCourt's payout code is the same in both runs.

## Reproduce

```bash
pip install -r requirements-dev.txt
python scripts/live_demo.py --minutes 12          # fresh PriceCourt and wallets, a full deal, a JSON log
# or step by step with the CLI: see skills/price-court/SKILL.md
```
