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

Consensus rule (`validator_fn`), **v2**: consensus is on a *canonical observation*, not on a
raw reading. Every node (leader and validators) reduces its own verified reading to the
same deterministic form:

* price truncated to `price_sig_digits` significant digits (default **5**, exact integer
  arithmetic, trailing zeros folded into the exponent: BTC `74962.34311` -> `74962`),
* `publish_time` floored to a `time_bucket_seconds` bucket (default **60 s**).

A validator accepts the leader's output only if it is **exactly equal** to its own
canonical observation. The value that gets stored is therefore one that every agreeing
validator computed independently, so *every accepted output produces the same downstream
decision*: a consumer asking "is BTC above X?" gets the same answer whichever validator
led the round. A reading that straddles a bucket edge simply fails to agree and the round
is retried with fresh reads (liveness is traded for an identical material result). A
leader that reports "unavailable" is accepted only if the validator cannot read a usable
price either; the reason text is diagnostic and is not compared.

> **Why v2.** v1 accepted any leader price within 25 bps of the validator's own reading and
> then stored the leader's exact value. Two acceptable leader values (e.g. `96123.99` and
> `96124.00`) could therefore sit on opposite sides of a downstream threshold. v2 removes
> the tolerance window entirely: there is exactly one acceptable output per round.
> `test_threshold_decision_is_identical_for_every_accepted_output` covers the case.

Storage rules: a reading is stored only if its `publish_time` is newer than the stored
bucket and no older than `max_staleness_seconds` (constructor argument, default 300) at the
transaction's consensus time. Otherwise nothing changes and the reason is stored in
`last_error`. `refresh` never reverts on an upstream problem, so the failure is visible.

## Interface

| Method | Kind | What it does |
|---|---|---|
| `refresh(feed)` | write, anyone | consensus read of the Pyth account; stores the price if verified, fresh and newer |
| `latest(feed)` | view | full record: price, conf, expo, publish time, bookkeeping, `last_error` |
| `get_price(feed)` | view | compact record; **reverts** if no verified price is stored yet |
| `list_feeds()` | view | `latest` for every whitelisted feed |
| `get_config()` | view | feeds (ids + Solana accounts), RPCs, canonical precision, staleness, owner |
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
pytest primitives/pyth_reader/tests -q        # 52 direct-mode tests
```

The tests mock the Solana RPC reply with byte-exact `PriceUpdateV2` accounts and replay
the captured validator against different readings: same bucket, bucket edge, exponent mismatch,
publish-minute mismatch, threshold-decision identity, forged leader price, leader error, unavailable-vs-available, wrong
owner, wrong feed id, partial verification, wrong discriminator, non-positive price,
stale and non-monotonic prices, RPC fallback, admin and ownership.

## Live deployment (GenLayer studionet, chain 61999), v2, 2026-11-20

Two deployments of the same v2 source, both with **exact consensus on the canonical
observation** (3 of 3 voting validators agreed on every round below).

**Demo instance** (`max_staleness_seconds = 604800`): stores the agreed canonical price.

| | |
|---|---|
| Contract | `0x26d259e1eB6c857c80065B0537Ffa3574d3f3eCe` |
| Explorer | https://explorer-studio.genlayer.com/address/0x26d259e1eB6c857c80065B0537Ffa3574d3f3eCe |
| Deploy tx | `0x0185fcc111fadf26f82166385a129649012330be94cfa4467447bcc1450375cc` (FINALIZED) |
| `refresh("BTC/USD")` | `0xc9d5e72b7861eab69a24ac053341887f9aca1ba2d88dbde6ab6de51d60a9f7f7` (FINALIZED, MAJORITY_AGREE): stored **61788** (5 significant digits), publish bucket 1795161600 |
| `refresh("ETH/USD")` | `0x3945c742d106890ab869a8caf53c173939091895b626f394270fe1e982298b9d` (FINALIZED, MAJORITY_AGREE): stored **1821.3**, publish bucket 1794920400 |

**Strict instance** (`max_staleness_seconds = 300`, the default): validators again agreed
exactly on the canonical reading, and the contract then **refused to store it** because
the Pyth Solana account was older than 5 minutes (`last_error` = "Pyth price is older than
max_staleness_seconds"). This is the intended fail-closed behaviour.

| | |
|---|---|
| Contract | `0x464441fB14e0885a6f16F46D1d8B404E46fc55E0` |
| Deploy tx | `0x83ef611348d3957c99c735b3b76634dc164438bcf8ecb6dae10efdcee2b855b2` (FINALIZED, MAJORITY_AGREE) |
| `refresh("BTC/USD")` | `0xeb40be66e044ad0f232707e2903e06668d7f57aa403bcf0ec0a4af861b93718a` (FINALIZED, MAJORITY_AGREE), not stored: stale |
| `refresh("ETH/USD")` | `0x7c6068f367e736a0cad0e418d869f1ee7474b2220cc0c8079ec63dcf2956dd30` (FINALIZED, MAJORITY_AGREE), not stored: stale |

> **Operational note (observed 2026-11-20).** The sponsored Pyth `PriceUpdateV2` accounts on
> Solana were updating slowly that day: BTC/USD was about 6 hours old and ETH/USD about
> 3 days old at read time. PythReader reports this honestly instead of storing a stale
> value. Pick `max_staleness_seconds` to match the freshness your consumer needs; a
> consumer that needs minute-fresh prices should treat a stale `last_error` as "no price".

Full record of both: [`live_run.json`](live_run.json). Studio is a shared sandbox that is
reset from time to time; redeploy with `deploy_studio.py` if an address is gone.
