#!/usr/bin/env python3
"""
deploy_studio.py - deploy PythReader to GenLayer studionet and run one live refresh.

    python primitives/pyth_reader/deploy_studio.py [--contract 0x...] [--feed BTC/USD ...]
                                                   [--out primitives/pyth_reader/live_run.json]

Uses a throwaway in-memory key funded for free on studionet (sim_fundAccount). Reports
exactly what the chain returned; nothing is faked.
"""

import argparse
import json
import os
import sys
import time

from genlayer_py import create_account, create_client
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus as S

HERE = os.path.dirname(os.path.abspath(__file__))
WEI = 10**18
EXPLORER = "https://explorer-studio.genlayer.com"


def hexs(tx):
    h = tx.hex() if hasattr(tx, "hex") else str(tx)
    return h if h.startswith("0x") else "0x" + h


def submit(send, attempts=5):
    for i in range(attempts):
        try:
            return send()
        except Exception as e:  # Studio sometimes answers 502 on submission
            if i == attempts - 1:
                raise
            print(f"  submit failed ({str(e)[:100]}); retrying in 10s", flush=True)
            time.sleep(10)


def finalize(client, tx):
    tx = hexs(tx)
    client.wait_for_transaction_receipt(transaction_hash=tx, status=S.FINALIZED, interval=5000, retries=180)
    t = client.get_transaction(tx)
    votes = sorted((t.get("consensus_data") or {}).get("votes", {}).values())
    return tx, {"tx": tx, "status": t.get("status_name"), "result": t.get("result_name"), "votes": votes}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract", help="reuse an existing PythReader deployment")
    ap.add_argument("--feed", action="append", help="feed to refresh (repeatable); default BTC/USD and ETH/USD")
    ap.add_argument("--staleness", type=int, default=300)
    ap.add_argument("--out", default=os.path.join(HERE, "live_run.json"))
    a = ap.parse_args()
    feeds = a.feed or ["BTC/USD", "ETH/USD"]

    acct = create_account()
    client = create_client(chain=studionet, account=acct)
    client.fund_account(acct.address, 10 * WEI)
    log = {"network": "studionet (chain 61999, https://studio.genlayer.com/api)",
           "caller": acct.address, "steps": []}

    addr = a.contract
    if not addr:
        with open(os.path.join(HERE, "pyth_reader.py"), "r", encoding="utf-8") as fh:
            tx = submit(lambda: client.deploy_contract(code=fh.read(), args=[a.staleness]))
        r = client.wait_for_transaction_receipt(transaction_hash=tx, status=S.ACCEPTED, interval=3000, retries=100)
        addr = (r.get("data") or {}).get("contract_address") or (r.get("tx_data_decoded") or {}).get("contract_address")
        _, step = finalize(client, tx)
        step.update(step="deploy PythReader", contract=addr)
        log["steps"].append(step)
        print(f"deployed {addr} tx {step['tx']} {step['status']}", flush=True)
    log["contract"] = addr
    log["explorer"] = f"{EXPLORER}/address/{addr}"

    for feed in feeds:
        _, step = finalize(client, submit(lambda: client.write_contract(address=addr, function_name="refresh",
                                                                        args=[feed])))
        rec = client.read_contract(address=addr, function_name="latest", args=[feed])
        step.update(step=f"refresh {feed}", record=rec)
        log["steps"].append(step)
        print(f"refresh {feed}: tx {step['tx']} {step['status']} {step['result']} votes={step['votes']}", flush=True)
        print(f"  latest: price={rec['price']} conf={rec['conf']} publish_time={rec['publish_time']} "
              f"error={rec['last_error']!r}", flush=True)
        if rec["available"]:
            log.setdefault("get_price", {})[feed] = client.read_contract(address=addr, function_name="get_price",
                                                                         args=[feed])

    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(log, fh, indent=2, default=str)
    print(f"wrote {a.out}", flush=True)
    ok = all(s.get("record", {}).get("available") for s in log["steps"] if s["step"].startswith("refresh"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
