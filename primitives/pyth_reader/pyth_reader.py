# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
PythReader - a keyless, consensus-verified Pyth price oracle for GenLayer.
==========================================================================

Since Pyth's Core upgrade (Aug 2026) the Hermes and Benchmarks HTTP APIs answer 401
without an API key, and a key cannot be kept secret in public contract storage. This
contract reads the keyless, on-chain form of the same Pyth feed instead: the sponsored
PriceUpdateV2 account that the Pyth Solana Receiver program keeps updated for each feed,
fetched from public Solana JSON-RPC (`getAccountInfo`, `finalized` commitment).

`refresh(feed)` runs one consensus read. Every validator fetches the account itself and
checks, byte by byte:

  * the account is owned by the Pyth Solana Receiver program,
  * the first 8 bytes are the PriceUpdateV2 Anchor discriminator,
  * the update is FULLY Wormhole-verified (not Partial),
  * the embedded 32-byte feed id equals the whitelisted id for that feed,
  * the price is positive.

The leader's reading is accepted only if each validator's own reading is within
`PRICE_TOLERANCE_BPS` of it and the publish times are at most
`MAX_PUBLISH_SKEW_SECONDS` apart. A leader claiming "unavailable" is accepted only if
the validator cannot read a usable price either. The agreed price is stored, so any
other contract or client can read it with a plain view (`latest`, `get_price`) at no
consensus cost.

Stored prices only move forward: a reading whose publish_time is not newer than the
stored one, or that is older than `max_staleness_seconds` at the transaction's consensus
time, is not stored and is reported in `last_error`.

Limitations: this is a LATEST-price reader. A PriceUpdateV2 account holds only the most
recent update, so historical prices cannot be fetched; record the price you need at the
time you need it.
"""

import base64
import json
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NoReturn

from genlayer import *

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

VERSION = "1.0.0"

# Default whitelist. feed -> (Pyth feed id, Solana PriceUpdateV2 account maintained by
# the Pyth Solana Receiver for that feed, shard 0). The owner may add more feeds.
DEFAULT_FEEDS = {
    "BTC/USD": ("e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
                "4cSM2e6rvbGQUFiJbqytoVMi5GgghSMr8LwVrT9VPSPo"),
    "ETH/USD": ("ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
                "42amVS4KgzR9rA28tkVYqVXjq9Qa8dcZQMbH5EYFX6XC"),
    "SOL/USD": ("ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
                "7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE"),
}
PYTH_RECEIVER_PROGRAM = "rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ"
PRICE_UPDATE_V2_DISCRIMINATOR = bytes.fromhex("22f123639d7ef4cd")
VERIFICATION_FULL = 1  # Anchor enum VerificationLevel { Partial{u8} = 0, Full = 1 }

# Public Solana RPC endpoints, tried in order by every validator.
SOLANA_RPCS = (
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
)

PRICE_TOLERANCE_BPS = 25        # validator vs leader price: max 0.25% apart
MAX_PUBLISH_SKEW_SECONDS = 120  # validator vs leader publish_time
MAX_FEEDS = 64
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

ERR = "[pyth-reader]"


# --------------------------------------------------------------------------- #
# Storage types
# --------------------------------------------------------------------------- #

@allow_storage
@dataclass
class Feed:
    feed_id: str        # 64 lowercase hex chars, no 0x
    account: str        # base58 Solana PriceUpdateV2 account


@allow_storage
@dataclass
class PriceRecord:
    price: str          # raw Pyth integer price ("" until the first good refresh)
    conf: str           # raw Pyth integer confidence interval
    expo: i64           # decimal exponent: real price = price * 10^expo
    publish_time: u64   # unix seconds, Pyth publish time
    updated_at: str     # consensus datetime of the refresh that stored it
    refreshes: u64      # successful (stored) refreshes
    attempts: u64       # all refresh calls
    last_error: str     # reason the latest attempt stored nothing ("" on success)


# --------------------------------------------------------------------------- #
# Pure helpers (deterministic)
# --------------------------------------------------------------------------- #

def _fail(msg: str) -> NoReturn:
    raise gl.vm.UserError(f"{ERR} {msg}")


def _now_iso() -> str:
    return str(gl.message_raw["datetime"])


def _now() -> int:
    s = _now_iso().strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _addr(raw) -> Address:
    v = raw.strip() if isinstance(raw, str) else ""
    if len(v) != 42 or v[:2].lower() != "0x" or any(c not in "0123456789abcdefABCDEF" for c in v[2:]):
        _fail("address must be 0x followed by 40 hex characters")
    return Address(v)


def _norm_feed(raw) -> str:
    """'btc/usd', 'BTC-USD', ' BTCUSD ' -> 'BTC/USD' style key (upper, single slash)."""
    if not isinstance(raw, str):
        _fail("feed must be text")
    s = raw.strip().upper().replace("-", "/").replace("_", "/")
    if "/" not in s and len(s) == 6 and s.isalnum():
        s = s[:3] + "/" + s[3:]
    parts = s.split("/")
    if len(parts) != 2 or not all(p.isalnum() and 1 <= len(p) <= 12 for p in parts):
        _fail("feed must look like BASE/QUOTE, e.g. BTC/USD")
    return parts[0] + "/" + parts[1]


def _norm_feed_id(raw) -> str:
    if not isinstance(raw, str):
        _fail("feed_id must be text")
    s = raw.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
        _fail("feed_id must be 32 bytes of hex")
    return s


def _norm_account(raw) -> str:
    if not isinstance(raw, str):
        _fail("account must be text")
    s = raw.strip()
    if not (32 <= len(s) <= 44) or any(c not in BASE58 for c in s):
        _fail("account must be a base58 Solana address")
    return s


def _fmt_price(price: int, expo: int) -> str:
    """Exact decimal string for price * 10^expo (no floats)."""
    if expo >= 0:
        return str(price * 10 ** expo)
    neg = price < 0
    digits = str(abs(price)).rjust(-expo + 1, "0")
    whole, frac = digits[:expo], digits[expo:].rstrip("0")
    out = f"{whole}.{frac}" if frac else whole
    return "-" + out if neg else out


def _within_bps(a_price: int, a_expo: int, b_price: int, b_expo: int, bps: int) -> bool:
    e = min(a_expo, b_expo)
    a = a_price * 10 ** (a_expo - e)
    b = b_price * 10 ** (b_expo - e)
    big = max(abs(a), abs(b))
    return abs(a - b) * 10000 <= bps * big


def decode_price_update_v2(raw: bytes, feed_id: str) -> dict:
    """Decode and verify a PriceUpdateV2 account body. Pure; never raises.

    Layout (Anchor, little-endian):
        [0:8]    discriminator 22f123639d7ef4cd
        [8:40]   write_authority (pubkey)
        [40]     verification_level: 1 = Full, 0 = Partial{num_signatures: u8}
        [41:73]  feed_id (32 bytes)
        [73:81]  price i64 | [81:89] conf u64 | [89:93] exponent i32
        [93:101] publish_time i64 | ... prev_publish_time, ema_price, ema_conf, posted_slot
    """
    if len(raw) < 133 or raw[:8] != PRICE_UPDATE_V2_DISCRIMINATOR:
        return {"ok": False, "reason": "not a Pyth PriceUpdateV2 account"}
    off = 8 + 32
    if raw[off] != VERIFICATION_FULL:
        return {"ok": False, "reason": "price update is not fully verified"}
    off += 1
    if raw[off:off + 32].hex() != feed_id:
        return {"ok": False, "reason": "price account holds a different feed"}
    off += 32
    price, conf, expo, publish_time = struct.unpack_from("<qQiq", raw, off)
    if price <= 0:
        return {"ok": False, "reason": "non-positive price"}
    return {"ok": True, "price": str(price), "conf": str(conf), "expo": int(expo),
            "publish_time": int(publish_time)}


def read_pyth_account(feed_id: str, account: str) -> dict:
    """Nondeterministic: fetch and verify the Pyth PriceUpdateV2 account for one feed.
    Returns {"ok": True, price fields} or {"ok": False, "reason": ...}; never raises."""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [account, {"encoding": "base64", "commitment": "finalized"}],
    })
    reason = "no RPC endpoint answered"
    for url in SOLANA_RPCS:
        try:
            res = gl.nondet.web.post(url, body=payload, headers={"Content-Type": "application/json"})
            if res.status != 200 or res.body is None:
                reason = f"rpc http {res.status}"
                continue
            value = json.loads(res.body.decode("utf-8"))["result"]["value"]
            if value is None:
                return {"ok": False, "reason": "price account not found"}
            if value.get("owner") != PYTH_RECEIVER_PROGRAM:
                return {"ok": False, "reason": "price account is not owned by the Pyth receiver"}
            return decode_price_update_v2(base64.b64decode(value["data"][0]), feed_id)
        except Exception:
            reason = "rpc request or decode failed"
            continue
    return {"ok": False, "reason": reason}


def _agree(lead: dict, mine: dict) -> bool:
    """Validator rule: both unavailable, or both ok, within tolerance and skew."""
    if not lead.get("ok"):
        return not mine.get("ok")
    if not mine.get("ok"):
        return False
    if abs(int(lead["publish_time"]) - int(mine["publish_time"])) > MAX_PUBLISH_SKEW_SECONDS:
        return False
    return _within_bps(int(lead["price"]), int(lead["expo"]), int(mine["price"]), int(mine["expo"]),
                       PRICE_TOLERANCE_BPS)


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #

class PythReader(gl.Contract):
    owner: Address
    pending_owner: Address
    feeds: TreeMap[str, Feed]
    feed_order: DynArray[str]
    prices: TreeMap[str, PriceRecord]
    max_staleness_seconds: u64

    def __init__(self, max_staleness_seconds: int = 300, owner: str = ""):
        if max_staleness_seconds < 10 or max_staleness_seconds > 7 * 24 * 3600:
            _fail("max_staleness_seconds must be between 10 seconds and 7 days")
        self.max_staleness_seconds = u64(max_staleness_seconds)
        o = gl.message.sender_address if owner == "" else _addr(owner)
        self.owner = o
        self.pending_owner = o
        for name, (fid, acct) in DEFAULT_FEEDS.items():
            self._put_feed(name, fid, acct)

    # ------------------------------------------------------------- internals
    def _put_feed(self, name: str, feed_id: str, account: str) -> None:
        if name not in self.feeds:
            if len(self.feed_order) >= MAX_FEEDS:
                _fail("feed whitelist is full")
            self.feed_order.append(name)
            self.prices[name] = PriceRecord(price="", conf="", expo=i64(0), publish_time=u64(0),
                                            updated_at="", refreshes=u64(0), attempts=u64(0),
                                            last_error="")
        self.feeds[name] = Feed(feed_id=feed_id, account=account)

    def _feed(self, feed: str) -> tuple[str, Feed]:
        name = _norm_feed(feed)
        f = self.feeds.get(name)
        if f is None:
            _fail(f"feed {name} is not on the whitelist")
        return name, f

    def _record(self, name: str) -> dict:
        f = self.feeds[name]
        r = self.prices[name]
        has = r.price != ""
        return {
            "feed": name, "feed_id": "0x" + f.feed_id,
            "source": "pyth-solana-receiver:" + f.account,
            "available": has,
            "price": _fmt_price(int(r.price), int(r.expo)) if has else "",
            "price_raw": r.price, "conf_raw": r.conf,
            "conf": _fmt_price(int(r.conf), int(r.expo)) if has else "",
            "expo": int(r.expo), "publish_time": int(r.publish_time),
            "updated_at": r.updated_at, "refreshes": int(r.refreshes),
            "attempts": int(r.attempts), "last_error": r.last_error,
        }

    def _observe(self, feed_id: str, account: str) -> dict:
        def leader_fn() -> dict:
            return read_pyth_account(feed_id, account)

        def validator_fn(leader_res) -> bool:
            if not isinstance(leader_res, gl.vm.Return):
                return False
            return _agree(leader_res.calldata, read_pyth_account(feed_id, account))

        return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

    def _require_owner(self) -> None:
        if gl.message.sender_address != self.owner:
            _fail("only the owner may call this")

    # ------------------------------------------------------------- writes
    @gl.public.write
    def refresh(self, feed: str) -> dict:
        """Anyone may call. Reads the Pyth price for a whitelisted feed under consensus
        and stores it if it is verified, fresh and newer than the stored price. Never
        reverts on an upstream problem: the reason is stored in `last_error`."""
        name, f = self._feed(feed)
        obs = self._observe(f.feed_id, f.account)
        r = self.prices[name]
        r.attempts = u64(int(r.attempts) + 1)
        if not obs.get("ok"):
            r.last_error = "price unavailable: " + str(obs.get("reason", "unknown"))
            return self._record(name)
        pt = int(obs["publish_time"])
        if pt + int(self.max_staleness_seconds) < _now():
            r.last_error = "Pyth price is older than max_staleness_seconds"
            return self._record(name)
        if pt <= int(r.publish_time):
            r.last_error = "no newer Pyth price than the stored one"
            return self._record(name)
        r.price, r.conf, r.expo = str(obs["price"]), str(obs["conf"]), i64(int(obs["expo"]))
        r.publish_time = u64(pt)
        r.updated_at = _now_iso()
        r.refreshes = u64(int(r.refreshes) + 1)
        r.last_error = ""
        return self._record(name)

    @gl.public.write
    def add_feed(self, feed: str, feed_id: str, account: str) -> dict:
        """Owner-only: whitelist (or re-point) a Pyth feed. The on-chain checks still
        apply on every read: the account must be owned by the Pyth receiver, fully
        verified, and carry exactly this feed id."""
        self._require_owner()
        name = _norm_feed(feed)
        self._put_feed(name, _norm_feed_id(feed_id), _norm_account(account))
        return self._record(name)

    @gl.public.write
    def transfer_ownership(self, new_owner: str) -> None:
        self._require_owner()
        self.pending_owner = _addr(new_owner)

    @gl.public.write
    def accept_ownership(self) -> None:
        if gl.message.sender_address != self.pending_owner or self.pending_owner == self.owner:
            _fail("only the nominated pending owner may accept ownership")
        self.owner = self.pending_owner

    # ------------------------------------------------------------- views
    @gl.public.view
    def latest(self, feed: str) -> dict:
        """Full stored record for a feed, including bookkeeping and last_error."""
        name, _ = self._feed(feed)
        return self._record(name)

    @gl.public.view
    def get_price(self, feed: str) -> dict:
        """Compact read for consumers. Reverts if no verified price has been stored yet,
        so a caller can never mistake "no data" for a price."""
        name, _ = self._feed(feed)
        r = self.prices[name]
        if r.price == "":
            _fail(f"no verified price stored for {name} yet; call refresh first")
        return {"feed": name, "price": _fmt_price(int(r.price), int(r.expo)),
                "price_raw": r.price, "conf_raw": r.conf, "expo": int(r.expo),
                "publish_time": int(r.publish_time)}

    @gl.public.view
    def list_feeds(self) -> list:
        return [self._record(n) for n in self.feed_order]

    @gl.public.view
    def get_config(self) -> dict:
        return {
            "version": VERSION,
            "owner": self.owner.as_hex, "pending_owner": self.pending_owner.as_hex,
            "feeds": {n: {"feed_id": "0x" + self.feeds[n].feed_id, "solana_account": self.feeds[n].account}
                      for n in self.feed_order},
            "pyth_receiver_program": PYTH_RECEIVER_PROGRAM,
            "solana_rpcs": list(SOLANA_RPCS),
            "price_tolerance_bps": PRICE_TOLERANCE_BPS,
            "max_publish_skew_seconds": MAX_PUBLISH_SKEW_SECONDS,
            "max_staleness_seconds": int(self.max_staleness_seconds),
        }
