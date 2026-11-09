#!/usr/bin/env python3
"""
pricecourt - a small CLI for the PriceCourt Intelligent Contract on GenLayer studionet.

    python scripts/pricecourt.py account new alice          # create + fund a test account
    python scripts/pricecourt.py deploy --as alice           # deploy, remember the address
    python scripts/pricecourt.py propose --as alice --counterparty bob --stake 5 \\
        --terms "Agent A pays Agent B the pot if BTC/USD is above 95000 at 2026-11-10T00:00:00Z, otherwise B pays A."
    python scripts/pricecourt.py deal 1                      # read the structured terms
    python scripts/pricecourt.py accept --as bob 1           # stake is read from the deal
    python scripts/pricecourt.py settle --as alice 1 --wait  # waits for settle_at, retries in the window
    python scripts/pricecourt.py reputation bob

Accounts are throwaway keys kept in $PRICECOURT_HOME/accounts/<name>.key (default
./.pricecourt, git-ignored). On studionet they are funded for free with the Studio
`sim_fundAccount` RPC: no faucet captcha, no real value. The contract address is kept in
$PRICECOURT_HOME/contract.json or taken from --contract / $PRICECOURT_CONTRACT.

Every write prints its transaction hash. Outcomes are verified by reading contract
state afterwards (the ground truth), not inferred from the receipt alone.
"""

import argparse
import json
import os
import sys
import time

from genlayer_py import create_account, create_client
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus

WEI = 10**18
HOME = os.environ.get("PRICECOURT_HOME", os.path.join(os.getcwd(), ".pricecourt"))
CONTRACT_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "contracts", "price_court.py")


# ----------------------------------------------------------------- plumbing

def out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str), flush=True)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def key_path(name: str) -> str:
    return os.path.join(HOME, "accounts", f"{name}.key")


def load_account(name: str):
    path = key_path(name)
    if not os.path.exists(path):
        sys.exit(f"unknown account '{name}' (create it with: account new {name})")
    with open(path, "r", encoding="utf-8") as fh:
        return create_account(fh.read().strip())


def resolve_address(value: str) -> str:
    """Accept a 0x address or the name of a local account."""
    if value.startswith("0x") and len(value) == 42:
        return value
    return load_account(value).address


def client_for(account=None):
    return create_client(chain=studionet, account=account)


def contract_address(args) -> str:
    addr = getattr(args, "contract", None) or os.environ.get("PRICECOURT_CONTRACT")
    if not addr:
        path = os.path.join(HOME, "contract.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                addr = json.load(fh)["address"]
    if not addr:
        sys.exit("no contract address: run `deploy`, pass --contract, or set PRICECOURT_CONTRACT")
    return addr


def wait(client, tx, retries=120):
    rcpt = client.wait_for_transaction_receipt(
        transaction_hash=tx, status=TransactionStatus.ACCEPTED, interval=5000, retries=retries)
    result = None
    if hasattr(rcpt, "get"):
        result = rcpt.get("tx_execution_result_name") or rcpt.get("tx_execution_result")
    return rcpt, result


def submit(send, attempts=5):
    """Submit a transaction, retrying transient RPC failures (Studio occasionally answers
    a 502 page instead of JSON-RPC). Only the submission is retried, never a mined tx."""
    for i in range(attempts):
        try:
            return send()
        except Exception as e:  # genlayer_py raises GenLayerError on a non-JSON reply
            if i == attempts - 1:
                raise
            log(f"submit failed ({str(e)[:120]}...); retrying in 10s")
            time.sleep(10)


def write(args, fn: str, fn_args: list, value: int = 0, retries=120):
    acct = load_account(args.as_)
    client = client_for(acct)
    addr = contract_address(args)
    tx = submit(lambda: client.write_contract(address=addr, function_name=fn, args=fn_args, value=value))
    tx_hex = tx.hex() if hasattr(tx, "hex") else str(tx)
    if not tx_hex.startswith("0x"):
        tx_hex = "0x" + tx_hex
    log(f"{fn}: tx {tx_hex} (waiting for ACCEPTED)")
    _, result = wait(client, tx, retries)
    log(f"{fn}: accepted, execution result: {result}")
    return client, addr, tx_hex, result


def read(args, fn: str, fn_args=None):
    # Reads are free and unsigned; genlayer_py still wants an account, so use a throwaway.
    return client_for(create_account()).read_contract(address=contract_address(args), function_name=fn, args=fn_args or [])


# ----------------------------------------------------------------- commands

def cmd_account(args):
    os.makedirs(os.path.join(HOME, "accounts"), exist_ok=True)
    if args.action == "new":
        path = key_path(args.name)
        if os.path.exists(path):
            sys.exit(f"account '{args.name}' already exists")
        acct = create_account()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(acct.key.hex())
        os.chmod(path, 0o600)
        client_for(acct).fund_account(acct.address, int(args.fund * WEI))
    acct = load_account(args.name)
    out({"name": args.name, "address": acct.address,
         "balance_wei": str(client_for(acct).get_balance(acct.address))})


def cmd_deploy(args):
    acct = load_account(args.as_)
    client = client_for(acct)
    with open(CONTRACT_SRC, "r", encoding="utf-8") as fh:
        code = fh.read()
    tx = client.deploy_contract(code=code, args=[args.window])
    tx_hex = tx.hex() if hasattr(tx, "hex") else str(tx)
    if not tx_hex.startswith("0x"):
        tx_hex = "0x" + tx_hex
    log(f"deploy: tx {tx_hex}")
    rcpt, _ = wait(client, tx)
    addr = (rcpt.get("data") or {}).get("contract_address") or (rcpt.get("tx_data_decoded") or {}).get("contract_address")
    if not addr:
        sys.exit(f"deploy did not return a contract address: {rcpt}")
    os.makedirs(HOME, exist_ok=True)
    with open(os.path.join(HOME, "contract.json"), "w", encoding="utf-8") as fh:
        json.dump({"address": addr, "deploy_tx": tx_hex, "network": "studionet"}, fh, indent=2)
    out({"contract": addr, "deploy_tx": tx_hex,
         "config": client.read_contract(address=addr, function_name="get_config")})


def cmd_propose(args):
    before = int(read(args, "get_config")["deal_count"])
    client, addr, tx, result = write(args, "propose_deal",
                                     [resolve_address(args.counterparty), args.terms],
                                     value=int(args.stake * WEI))
    deals = client.read_contract(address=addr, function_name="list_deals", args=[before, 1])
    if not deals:
        sys.exit(f"propose_deal did not create a deal (tx {tx}, result {result}): the terms were "
                 "rejected or validators could not agree on their meaning")
    out({"tx": tx, "deal": deals[0]})


def cmd_accept(args):
    deal = read(args, "get_deal", [args.deal_id])
    _, _, tx, result = write(args, "accept_deal", [args.deal_id], value=int(deal["stake"]))
    out({"tx": tx, "result": result, "deal": read(args, "get_deal", [args.deal_id])})


def cmd_settle(args):
    deal = read(args, "get_deal", [args.deal_id])
    window_end = deal["terms"]["settle_at"] + int(read(args, "get_config")["price_window_seconds"])
    if args.wait:
        # +30s: the Pyth account is read at `finalized` commitment, which trails real time.
        delay = deal["terms"]["settle_at"] - time.time() + 30
        if delay > 0:
            log(f"waiting {int(delay)}s for the settlement time {deal['terms']['settle_at_iso']}")
            time.sleep(delay)
    txs = []
    while True:
        _, _, tx, result = write(args, "settle", [args.deal_id])
        deal = read(args, "get_deal", [args.deal_id])
        txs.append({"tx": tx, "result": result, "status": deal["status"], "last_error": deal["last_error"]})
        if deal["status"] != "accepted" or not args.wait or time.time() > window_end:
            break
        log(f"not settled yet ({deal['last_error'] or result}); retrying in 15s")
        time.sleep(15)
    out({"attempts": txs, "deal": deal})


def cmd_simple(fn):
    def run(args):
        _, _, tx, result = write(args, fn, [args.deal_id])
        out({"tx": tx, "result": result, "deal": read(args, "get_deal", [args.deal_id])})
    return run


def cmd_deal(args):
    out(read(args, "get_deal", [args.deal_id]))


def cmd_deals(args):
    out(read(args, "list_deals", [args.offset, args.limit]))


def cmd_reputation(args):
    out(read(args, "get_reputation", [resolve_address(args.who)]))


def cmd_config(args):
    out(read(args, "get_config"))


def main(argv=None):
    p = argparse.ArgumentParser(prog="pricecourt", description=__doc__.split("\n\n")[0])
    p.add_argument("--contract", help="contract address (default: $PRICECOURT_CONTRACT or the last deploy)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("account", help="create or show a local studionet test account")
    a.add_argument("action", choices=["new", "show"])
    a.add_argument("name")
    a.add_argument("--fund", type=float, default=100.0, help="GEN to mint on studionet (new only)")
    a.set_defaults(fn=cmd_account)

    d = sub.add_parser("deploy", help="deploy contracts/price_court.py")
    d.add_argument("--as", dest="as_", required=True)
    d.add_argument("--window", type=int, default=300, help="price_window_seconds (default 300)")
    d.set_defaults(fn=cmd_deploy)

    pr = sub.add_parser("propose", help="propose a deal and lock your stake")
    pr.add_argument("--as", dest="as_", required=True)
    pr.add_argument("--counterparty", required=True, help="0x address or local account name")
    pr.add_argument("--stake", type=float, required=True, help="stake per side, in GEN")
    pr.add_argument("--terms", required=True, help="plain-English terms with an explicit UTC time")
    pr.set_defaults(fn=cmd_propose)

    for name, fn, help_ in (("accept", cmd_accept, "accept a deal, locking the matching stake"),
                            ("settle", cmd_settle, "settle a deal on the Pyth price"),
                            ("cancel", cmd_simple("cancel_proposal"), "proposer reclaims an unaccepted proposal"),
                            ("decline", cmd_simple("decline_deal"), "counterparty declines a proposal"),
                            ("refund", cmd_simple("refund_unsettled"), "refund both stakes after the window")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--as", dest="as_", required=True)
        s.add_argument("deal_id")
        if name == "settle":
            s.add_argument("--wait", action="store_true",
                           help="sleep until settle_at, then retry until settled or the window closes")
        s.set_defaults(fn=fn)

    g = sub.add_parser("deal", help="read one deal")
    g.add_argument("deal_id")
    g.set_defaults(fn=cmd_deal)

    ls = sub.add_parser("deals", help="list deals")
    ls.add_argument("--offset", type=int, default=0)
    ls.add_argument("--limit", type=int, default=20)
    ls.set_defaults(fn=cmd_deals)

    r = sub.add_parser("reputation", help="read an address's reputation record")
    r.add_argument("who", help="0x address or local account name")
    r.set_defaults(fn=cmd_reputation)

    c = sub.add_parser("config", help="read contract configuration")
    c.set_defaults(fn=cmd_config)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
