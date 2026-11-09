"""Shared fixtures and mock helpers for the PriceCourt direct-mode suite.

The GenVM SDK release is pinned so the suite runs against the runner the contract's
Depends header targets (override with GENVM_VERSION)."""

import base64
import datetime as _dt
import json
import os
import struct

import pytest

GENVM_VERSION = os.environ.get("GENVM_VERSION") or "v0.2.16"
CONTRACT = "contracts/price_court.py"

FEED_IDS = {
    "BTC/USD": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
    "ETH/USD": "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "SOL/USD": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
}
RECEIVER = "rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ"
DISCRIMINATOR = bytes.fromhex("22f123639d7ef4cd")
SOLANA_RPC_RE = r"api\.mainnet-beta\.solana\.com|solana-rpc\.publicnode\.com"
PARSER_RE = r"two-party price wager"


@pytest.fixture
def direct_deploy(direct_deploy):
    def _deploy(contract_path, *args, sdk_version=None, **kwargs):
        return direct_deploy(contract_path, *args, sdk_version=sdk_version or GENVM_VERSION, **kwargs)

    return _deploy


# ----------------------------------------------------------------- clock

def iso(epoch: int) -> str:
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(iso_str: str) -> int:
    return int(_dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp())


def set_chain_time(iso_str: str) -> None:
    """Set the consensus transaction clock the contract reads (gl.message_raw['datetime']).
    In direct mode that dict is injected once at deploy; vm.warp only moves
    datetime.now(), so set the field a real transaction carries directly."""
    import genlayer.gl as _g

    _g.message_raw["datetime"] = iso_str


# ----------------------------------------------------------------- LLM mocks

def terms_reply(feed="BTC/USD", when="2026-11-10T00:00:00Z", comparator=">", threshold="95000",
                winner_if_true="counterparty", supported=True) -> str:
    if not supported:
        return json.dumps({"supported": False})
    return json.dumps({"supported": True, "feed": feed, "settlement_time": when,
                       "comparator": comparator, "threshold": threshold,
                       "winner_if_true": winner_if_true})


def mock_parser(vm, **kw) -> None:
    vm.mock_llm(PARSER_RE, terms_reply(**kw))


# ----------------------------------------------------------------- Pyth mocks

def price_account(feed="BTC/USD", price=9_600_000_000_000, expo=-8, publish_time=0, conf=1_000_000,
                  verification=1, feed_id=None) -> bytes:
    """Byte-exact PriceUpdateV2 account as written by the Pyth Solana Receiver."""
    fid = bytes.fromhex(feed_id or FEED_IDS[feed])
    b = DISCRIMINATOR + bytes(32)                        # write_authority
    b += bytes([verification]) if verification == 1 else bytes([0, 3])
    b += fid + struct.pack("<qQiqqqQ", price, conf, expo, publish_time, publish_time - 1, price, conf)
    b += struct.pack("<Q", 123456789) + b"\x00"
    return b


def rpc_body(data: bytes, owner=RECEIVER) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
        "context": {"slot": 1},
        "value": {"data": [base64.b64encode(data).decode(), "base64"], "owner": owner,
                  "executable": False, "lamports": 1, "space": len(data)}}})


def mock_pyth(vm, owner=RECEIVER, status=200, **kw) -> None:
    body = rpc_body(price_account(**kw), owner=owner) if status == 200 else "upstream error"
    vm.mock_web(SOLANA_RPC_RE, {"method": "POST", "status": status, "body": body})


def addr(a) -> str:
    if hasattr(a, "as_hex"):
        return a.as_hex
    if isinstance(a, (bytes, bytearray)):
        return "0x" + bytes(a).hex()
    return str(a)
