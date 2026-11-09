"""Direct-mode tests for AgentWallet (the optional smart-account wrapper agents use on
studionet so contract-to-contract payouts are credited). Emitted messages are no-ops in
direct mode, so these cover the access and input guards."""

import json

import pytest

from conftest import addr

WALLET = "contracts/agent_wallet.py"
TARGET = "0x" + "11" * 20


@pytest.fixture
def wallet(direct_vm, direct_deploy, direct_alice):
    direct_vm.sender = direct_alice
    return direct_deploy(WALLET)


def test_owner_is_deployer(wallet, direct_alice):
    info = wallet.get_info()
    assert info["owner"].lower() == addr(direct_alice).lower() and info["calls"] == 0


def test_only_owner_can_call_deposit_or_withdraw(direct_vm, wallet, direct_bob):
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("only the wallet owner"):
        wallet.call(TARGET, "accept_deal", json.dumps(["1"]), 0)
    with direct_vm.expect_revert("only the wallet owner"):
        wallet.deposit()
    with direct_vm.expect_revert("only the wallet owner"):
        wallet.withdraw(1)


def test_owner_call_is_forwarded(direct_vm, wallet, direct_alice):
    direct_vm.sender = direct_alice
    wallet.call(TARGET, "settle", json.dumps(["1"]), 0)
    assert wallet.get_info()["calls"] == 1


def test_call_input_guards(direct_vm, wallet, direct_alice):
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("invalid method name"):
        wallet.call(TARGET, "__init__", "[]", 0)
    with direct_vm.expect_revert("invalid method name"):
        wallet.call(TARGET, "settle(1)", "[]", 0)
    with direct_vm.expect_revert("must be a JSON list"):
        wallet.call(TARGET, "settle", json.dumps({"id": "1"}), 0)
    with direct_vm.expect_revert("exceeds the wallet balance"):
        wallet.call(TARGET, "accept_deal", json.dumps(["1"]), 10**30)
    with direct_vm.expect_revert("amount must be positive"):
        wallet.withdraw(0)
