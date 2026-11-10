"""Byte-exact mocks of the Solana JSON-RPC getAccountInfo reply for a Pyth
PriceUpdateV2 account, plus a consensus-clock helper."""

import base64
import datetime as _dt
import json
import os
import struct

CONTRACT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pyth_reader.py")

FEED_IDS = {
    "BTC/USD": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
    "ETH/USD": "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "SOL/USD": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
}
ACCOUNTS = {
    "BTC/USD": "4cSM2e6rvbGQUFiJbqytoVMi5GgghSMr8LwVrT9VPSPo",
    "ETH/USD": "42amVS4KgzR9rA28tkVYqVXjq9Qa8dcZQMbH5EYFX6XC",
    "SOL/USD": "7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE",
}
RECEIVER = "rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ"
DISCRIMINATOR = bytes.fromhex("22f123639d7ef4cd")
SOLANA_RPC_RE = r"api\.mainnet-beta\.solana\.com|solana-rpc\.publicnode\.com"
SOLANA_PRIMARY_RE = r"api\.mainnet-beta\.solana\.com"
SOLANA_FALLBACK_RE = r"solana-rpc\.publicnode\.com"


def iso(epoch: int) -> str:
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(iso_str: str) -> int:
    return int(_dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp())


def set_chain_time(iso_str: str) -> None:
    """Set the consensus transaction clock the contract reads (gl.message_raw['datetime'])."""
    import genlayer.gl as _g

    _g.message_raw["datetime"] = iso_str


def price_account(feed="BTC/USD", price=9_600_000_000_000, expo=-8, publish_time=0, conf=1_000_000,
                  verification=1, feed_id=None, discriminator=DISCRIMINATOR) -> bytes:
    """Byte-exact PriceUpdateV2 account as written by the Pyth Solana Receiver."""
    fid = bytes.fromhex(feed_id or FEED_IDS[feed])
    b = discriminator + bytes(32)                        # write_authority
    b += bytes([verification]) if verification == 1 else bytes([0, 3])
    b += fid + struct.pack("<qQiqqqQ", price, conf, expo, publish_time, publish_time - 1, price, conf)
    b += struct.pack("<Q", 123456789) + b"\x00"
    return b


def rpc_body(data: bytes, owner=RECEIVER) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
        "context": {"slot": 1},
        "value": {"data": [base64.b64encode(data).decode(), "base64"], "owner": owner,
                  "executable": False, "lamports": 1, "space": len(data)}}})


def mock_pyth(vm, owner=RECEIVER, status=200, url_re=SOLANA_RPC_RE, **kw) -> None:
    body = rpc_body(price_account(**kw), owner=owner) if status == 200 else "upstream error"
    vm.mock_web(url_re, {"method": "POST", "status": status, "body": body})


def mock_missing_account(vm) -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": None}})
    vm.mock_web(SOLANA_RPC_RE, {"method": "POST", "status": 200, "body": body})


def addr(a) -> str:
    if hasattr(a, "as_hex"):
        return a.as_hex
    if isinstance(a, (bytes, bytearray)):
        return "0x" + bytes(a).hex()
    return str(a)
