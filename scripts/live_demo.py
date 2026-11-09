#!/usr/bin/env python3
"""
live_demo.py - one full PriceCourt deal on GenLayer studionet, end to end, with the two
agents acting through AgentWallet contracts so the escrow payout is credited on-chain.

    python scripts/live_demo.py [--contract 0x...] [--minutes 12] [--out docs/live_run_wallets.json]

Steps (every transaction hash, including the messages each one triggers, is recorded):
  1. two throwaway agent keys, funded for free on studionet (sim_fundAccount)
  2. PriceCourt (reused via --contract, or freshly deployed) + one AgentWallet per agent
  3. each agent deposits into its wallet
  4. wallet A -> propose_deal(wallet B, terms)   [LLM terms parse under consensus]
  5. wallet B -> accept_deal(id)                  [matching stake]
  6. after settle_at: anyone -> settle(id)        [Pyth evidence under consensus]
  7. the pot is transferred to the winner's wallet; balances and reputation verified

Nothing is faked: the script reports exactly what the chain returned. Keys are in-memory
only. Studio is a shared sandbox that is periodically reset.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

from genlayer_py import create_account, create_client
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus as S

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
WEI = 10**18
STAKE = 5 * WEI
LOG = {"network": "studionet (chain 61999, https://studio.genlayer.com/api)", "steps": []}


def say(msg):
    print(msg, flush=True)


def hexs(tx):
    h = tx.hex() if hasattr(tx, "hex") else str(tx)
    return h if h.startswith("0x") else "0x" + h


def submit(send, attempts=5):
    """Retry transient RPC failures on submission only (Studio sometimes answers 502)."""
    for i in range(attempts):
        try:
            return send()
        except Exception as e:
            if i == attempts - 1:
                raise
            say(f"  submit failed ({str(e)[:100]}...); retrying in 10s")
            time.sleep(10)


def finalize(client, tx, label, follow=True):
    """Wait for FINALIZED, then follow triggered messages to FINALIZED too."""
    tx = hexs(tx)
    client.wait_for_transaction_receipt(transaction_hash=tx, status=S.FINALIZED, interval=5000, retries=180)
    t = client.get_transaction(tx)
    votes = sorted((t.get("consensus_data") or {}).get("votes", {}).values())
    step = {"step": label, "tx": tx, "status": t.get("status_name"), "result": t.get("result_name"),
            "votes": votes, "triggered": []}
    if follow:
        for child in t.get("triggered_transactions") or []:
            for _ in range(120):
                c = client.get_transaction(child)
                if c.get("status_name") == "FINALIZED":
                    break
                time.sleep(5)
            cvotes = sorted((c.get("consensus_data") or {}).get("votes", {}).values())
            step["triggered"].append({"tx": child, "status": c.get("status_name"), "result": c.get("result_name"),
                                      "to": c.get("to_address"), "value": str(c.get("value")),
                                      "value_credited": c.get("value_credited"), "votes": cvotes})
    LOG["steps"].append(step)
    say(f"  {label}: {tx} {step['status']} {step['result']} votes={votes}")
    for ch in step["triggered"]:
        say(f"    -> triggered {ch['tx']} {ch['status']} {ch['result']} value={ch['value']} credited={ch['value_credited']}")
    return step


def deploy(client, path, args, label):
    with open(path, "r", encoding="utf-8") as fh:
        tx = client.deploy_contract(code=fh.read(), args=args)
    r = client.wait_for_transaction_receipt(transaction_hash=tx, status=S.ACCEPTED, interval=3000, retries=100)
    addr = (r.get("data") or {}).get("contract_address") or (r.get("tx_data_decoded") or {}).get("contract_address")
    finalize(client, tx, label, follow=False)
    LOG["steps"][-1]["contract"] = addr
    return addr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract", help="reuse an existing PriceCourt deployment")
    ap.add_argument("--minutes", type=int, default=12, help="settlement time = now + this many minutes")
    ap.add_argument("--feed", default="ETH/USD")
    ap.add_argument("--terms", default="Agent A pays Agent B the pot if {feed} is below 2,000 at {t}, otherwise B pays A.")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "live_run_wallets.json"))
    a = ap.parse_args()

    owner_a, owner_b = create_account(), create_account()
    ca, cb = create_client(chain=studionet, account=owner_a), create_client(chain=studionet, account=owner_b)
    reader = create_client(chain=studionet, account=create_account())
    for c, acct in ((ca, owner_a), (cb, owner_b)):
        c.fund_account(acct.address, 100 * WEI)
    LOG["owner_a"], LOG["owner_b"] = owner_a.address, owner_b.address
    say(f"agent A key {owner_a.address}\nagent B key {owner_b.address}")

    court = a.contract or deploy(ca, os.path.join(ROOT, "contracts", "price_court.py"), [600], "deploy PriceCourt")
    wallet_a = deploy(ca, os.path.join(ROOT, "contracts", "agent_wallet.py"), [], "deploy AgentWallet A")
    wallet_b = deploy(cb, os.path.join(ROOT, "contracts", "agent_wallet.py"), [], "deploy AgentWallet B")
    LOG.update(price_court=court, wallet_a=wallet_a, wallet_b=wallet_b)
    say(f"PriceCourt {court}\nwallet A {wallet_a}\nwallet B {wallet_b}")

    finalize(ca, submit(lambda: ca.write_contract(address=wallet_a, function_name="deposit", args=[], value=10 * WEI)), "A deposits 10 GEN")
    finalize(cb, submit(lambda: cb.write_contract(address=wallet_b, function_name="deposit", args=[], value=10 * WEI)), "B deposits 10 GEN")

    t = (int(time.time()) // 60 + a.minutes) * 60
    t_iso = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    terms = a.terms.format(feed=a.feed, t=t_iso)
    LOG["terms_text"] = terms
    say(f"terms: {terms}")

    before = int(reader.read_contract(address=court, function_name="get_config")["deal_count"])
    finalize(ca, submit(lambda: ca.write_contract(address=wallet_a, function_name="call",
                                   args=[court, "propose_deal", json.dumps([wallet_b, terms]), STAKE])),
             "wallet A -> propose_deal")
    deals = reader.read_contract(address=court, function_name="list_deals", args=[before, 1])
    if not deals:
        LOG["error"] = "propose_deal created no deal"
        return done(a.out, 1)
    deal_id = deals[0]["id"]
    LOG["deal_id"] = deal_id
    LOG["deal_after_propose"] = deals[0]
    say(f"deal {deal_id}: {deals[0]['summary']}")

    finalize(cb, submit(lambda: cb.write_contract(address=wallet_b, function_name="call",
                                   args=[court, "accept_deal", json.dumps([deal_id]), STAKE])),
             "wallet B -> accept_deal")
    d = reader.read_contract(address=court, function_name="get_deal", args=[deal_id])
    if d["status"] != "accepted":
        LOG["error"] = f"deal not accepted: {d['status']}"
        return done(a.out, 1)

    bal_before = {"wallet_a": str(reader.get_balance(wallet_a)), "wallet_b": str(reader.get_balance(wallet_b)),
                  "price_court": str(reader.get_balance(court))}
    LOG["balances_before_settle"] = bal_before
    say(f"balances before settle: {bal_before}")

    delay = t - time.time() + 30     # Pyth account is read at `finalized` commitment
    if delay > 0:
        say(f"waiting {int(delay)}s for {t_iso}")
        time.sleep(delay)
    window_end = t + int(reader.read_contract(address=court, function_name="get_config")["price_window_seconds"])
    while True:
        finalize(ca, submit(lambda: ca.write_contract(address=court, function_name="settle", args=[deal_id])), "settle")
        d = reader.read_contract(address=court, function_name="get_deal", args=[deal_id])
        LOG["steps"][-1]["deal_status"] = d["status"]
        LOG["steps"][-1]["last_error"] = d["last_error"]
        if d["status"] != "accepted" or time.time() > window_end:
            break
        say(f"  not settled yet: {d['last_error']}; retrying")
        time.sleep(10)

    LOG["deal_final"] = d
    LOG["balances_after_settle"] = {"wallet_a": str(reader.get_balance(wallet_a)),
                                    "wallet_b": str(reader.get_balance(wallet_b)),
                                    "price_court": str(reader.get_balance(court))}
    LOG["reputation"] = {w: reader.read_contract(address=court, function_name="get_reputation", args=[w])
                         for w in (wallet_a, wallet_b)}
    say(json.dumps({k: LOG[k] for k in ("balances_after_settle", "reputation")}, indent=2))
    say(f"verdict: winner {d['winner']} condition_met={d['condition_met']} evidence={d['evidence']}")
    return done(a.out, 0 if d["status"] == "settled" else 1)


def done(path, code):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(LOG, fh, indent=2, default=str)
    say(f"wrote {path}")
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:   # keep every hash recorded so far, then fail loudly
        LOG["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        done(os.path.join(ROOT, "docs", "live_run_wallets.json"), 1)
