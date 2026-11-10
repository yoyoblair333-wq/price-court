# PythReader: keyless, consensus-verified Pyth prices for GenLayer

A small, standalone GenLayer Intelligent Contract that stores the latest **Pyth** price
for whitelisted feeds (BTC/USD, ETH/USD, SOL/USD by default), after every validator has
fetched and verified it independently. Any contract or client can then read the agreed
price with a plain view call.

It is the price-evidence layer of [PriceCourt](../../README.md), packaged on its own so
other builders can reuse it.

## The problem it solves

Since Pyth's Core upgrade (Aug 2026), the Hermes and Benchmarks HTTP APIs return
**401 without an API key**. An Intelligent Contract cannot hold a secret: its storage and
calldata are public. So `gl.nondet.web.get("https://hermes.pyth.network/...")` no longer
works from a contract.

PythReader reads the **keyless on-chain copy of the same Pyth feed** instead. The Pyth
Solana Receiver program keeps a sponsored `PriceUpdateV2` account per feed up to date on
Solana mainnet. Anyone can read it through public Solana JSON-RPC
(`getAccountInfo`, `finalized` commitment). No key, no relayer, no off-chain service.

## How it verifies a price

On `refresh(feed)` the leader and every validator each fetch the account and check:

| Check | Rejects |
|---|---|
| account `owner` == Pyth Solana Receiver `rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ` | look-alike accounts |
| first 8 bytes == `PriceUpdateV2` discriminator `22f123639d7ef4cd` | other account types |
| `verification_level` == `Full` | partially Wormhole-verified updates |
| embedded 32-byte feed id == the whitelisted id | an account holding a different feed |
| `price > 0` | broken updates |

Consensus rule (`validator_fn`): the validator accepts the leader's reading only if its
own reading is within **25 bps** and the publish times are at most **120 s** apart. A
leader that reports "unavailable" is accepted only if the validator cannot read a usable
price either. A forged leader price fails the tolerance check.

Storage rules: a reading is stored only if its `publish_time` is newer than the stored
one and no older than `max_staleness_seconds` (constructor argument, default 300) at the
transaction's consensus time. Otherwise nothing changes and the reason is stored in
`last_error`. `refresh` never reverts on an upstream problem, so the failure is visible.

## Interface

| Method | Kind | What it does |
|---|---|---|
| `refresh(feed)` | write, anyone | consensus read of the Pyth account; stores the price if verified, fresh and newer |
| `latest(feed)` | view | full record: price, conf, expo, publish time, bookkeeping, `last_error` |
| `get_price(feed)` | view | compact record; **reverts** if no verified price is stored yet |
| `list_feeds()` | view | `latest` for every whitelisted feed |
| `get_config()` | view | feeds (ids + Solana accounts), RPCs, tolerance, staleness, owner |
| `add_feed(feed, feed_id, account)` | write, owner | whitelist another Pyth feed (same on-chain checks apply) |
| `transfer_ownership(addr)` / `accept_ownership()` | write | two-step ownership |

Feed names are normalised: `btc/usd`, `BTC-USD`, `BTCUSD` all mean `BTC/USD`.

### Data format

`get_price("BTC/USD")` from the live deployment below:

```json
{"feed": "BTC/USD", "price": "74705.599649", "price_raw": "7470559964900",
 "conf_raw": "9376201780", "expo": -8, "publish_time": 1794285157}
```

* real price = `price_raw * 10^expo`. `price` is the same value as an exact decimal
  string (no floats anywhere).
* `conf_raw` is Pyth's confidence interval at the same exponent.
* `publish_time` is Pyth's publish time, unix seconds.

`latest(feed)` adds `feed_id`, `source` (`pyth-solana-receiver:<account>`), `available`,
`conf` (decimal), `updated_at` (consensus time of the storing refresh), `refreshes`,
`attempts` and `last_error`.

## Using it

### From another Intelligent Contract

```python
from genlayer import *

class MyMarket(gl.Contract):
    oracle: Address

    def __init__(self, oracle: str):
        self.oracle = Address(oracle)

    @gl.public.write
    def settle(self) -> None:
        p = gl.get_contract_at(self.oracle).view().get_price("BTC/USD")
        price, expo, ts = int(p["price_raw"]), int(p["expo"]), int(p["publish_time"])
        # check ts against your own freshness rule, then compare exactly:
        # price * 10**expo > 95_000  <=>  price > 95_000 * 10**(-expo) when expo < 0
        ...
```

Your contract decides how fresh is fresh enough by checking `publish_time`. If you need
a fresh price inside one transaction, have a keeper (or the user) call
`refresh(feed)` first; it costs one consensus round.

### From genlayer-js

```ts
import { createClient, createAccount } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus } from "genlayer-js/types";

const client = createClient({ chain: studionet, account: createAccount() });
const reader = "0xBbbDE88057B1516Cad0a7c3EC5F28575D9391F1a";

const tx = await client.writeContract({ address: reader, functionName: "refresh", args: ["ETH/USD"], value: 0n });
await client.waitForTransactionReceipt({ hash: tx, status: TransactionStatus.FINALIZED });
const p = await client.readContract({ address: reader, functionName: "get_price", args: ["ETH/USD"] });
```

### From Python (genlayer-py)

`python primitives/pyth_reader/deploy_studio.py` deploys a fresh copy to studionet,
refreshes BTC/USD and ETH/USD, and writes every hash to `live_run.json`.
Pass `--contract 0x...` to refresh an existing deployment.

## Limitations

* **Latest price only, not historical.** A `PriceUpdateV2` account holds only the most
  recent update. Store the price you need when you need it.
* Freshness depends on Solana `finalized` lag (typically 15 to 30 s) plus Pyth's sponsored
  update cadence. Do not use it for sub-minute decisions.
* Relies on public Solana RPC endpoints (two are tried in order). If both are down,
  `refresh` stores nothing and says why.
* Only feeds that have a sponsored Pyth Solana Receiver account can be added.

## Tests and lint

```bash
genvm-lint check primitives/pyth_reader/pyth_reader.py
genvm-lint typecheck primitives/pyth_reader/pyth_reader.py
pytest primitives/pyth_reader/tests -q        # 41 direct-mode tests
```

The tests mock the Solana RPC reply with byte-exact `PriceUpdateV2` accounts and replay
the captured validator against different readings: tolerance, exponent mismatch,
publish-time skew, forged leader price, leader error, unavailable-vs-available, wrong
owner, wrong feed id, partial verification, wrong discriminator, non-positive price,
stale and non-monotonic prices, RPC fallback, admin and ownership.

## Live deployment (GenLayer studionet, chain 61999)

| | |
|---|---|
| Contract | `0xBbbDE88057B1516Cad0a7c3EC5F28575D9391F1a` |
| Explorer | https://explorer-studio.genlayer.com/address/0xBbbDE88057B1516Cad0a7c3EC5F28575D9391F1a |
| Deploy tx | `0x6533c1a40825b42b6a6fca791e82a233c86e9b9434dfbf91950ed967531497cc` (FINALIZED, MAJORITY_AGREE) |
| `refresh("BTC/USD")` | `0xe968d3adfa98d9d5e4f6f28ee48c791aca87cc56d4ddc16730e61ede38853ac4` (FINALIZED, MAJORITY_AGREE, 3 agree): stored 74705.599649 ± 93.76, publish_time 1794285157 |
| `refresh("ETH/USD")` | `0x1f97dd7d727e2d788ee58a995048757ae2a216eacac8ba6c5a62c0be35e42ee0` (FINALIZED, MAJORITY_AGREE, 3 agree): stored 2120.00944493 ± 2.61, publish_time 1794285195 |

Full record: [`live_run.json`](live_run.json). Studio is a shared sandbox that is reset
from time to time; redeploy with `deploy_studio.py` if the address is gone.
