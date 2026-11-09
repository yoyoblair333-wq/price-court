# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
AgentWallet - a minimal smart-account wallet an agent can act through.

An agent's key (the owner) funds the wallet and asks it to call another contract, with
native value attached. The wallet's address is the agent's on-chain identity for
PriceCourt: it proposes / accepts deals, and PriceCourt pays the pot back to it.

Why this exists: on GenLayer studionet, a value transfer emitted by a contract to an
externally-owned address is currently routed to Studio's contract_not_found_handler and
never credited, while contract-to-contract transfers are credited normally. Agents that
hold funds in a contract wallet (the usual pattern for autonomous agents anyway) receive
PriceCourt payouts on studionet today. PriceCourt itself is unchanged and pays any
address.
"""

import json

from genlayer import *


class AgentWallet(gl.Contract):
    owner: Address
    calls: u256

    def __init__(self):
        self.owner = gl.message.sender_address
        self.calls = u256(0)

    def _only_owner(self) -> None:
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError("[EXPECTED] only the wallet owner")

    @gl.public.write.payable
    def deposit(self) -> None:
        """Owner tops up the wallet (the attached value stays in the wallet)."""
        self._only_owner()

    @gl.public.write
    def call(self, target: str, method: str, args_json: str, value: int) -> None:
        """Emit `target.method(*args)` from this wallet, attaching `value` base units.
        Emitted on finalization of this transaction, as recommended for value moves."""
        self._only_owner()
        if not method.isidentifier() or method.startswith("_"):
            raise gl.vm.UserError("[EXPECTED] invalid method name")
        args = json.loads(args_json) if args_json else []
        if not isinstance(args, list):
            raise gl.vm.UserError("[EXPECTED] args_json must be a JSON list")
        if value < 0 or value > int(gl.get_contract_at(gl.message.contract_address).balance):
            raise gl.vm.UserError("[EXPECTED] value exceeds the wallet balance")
        proxy = gl.get_contract_at(Address(target)).emit(value=u256(value), on="finalized")
        getattr(proxy, method)(*args)
        self.calls = u256(int(self.calls) + 1)

    @gl.public.write
    def withdraw(self, amount: int) -> None:
        """Send funds back to the owner key. Note: studionet currently drops
        contract-to-EOA transfers (see module docstring); real networks credit them."""
        self._only_owner()
        if amount <= 0:
            raise gl.vm.UserError("[EXPECTED] amount must be positive")
        gl.get_contract_at(self.owner).emit_transfer(value=u256(amount), on="finalized")

    @gl.public.view
    def get_info(self) -> dict:
        return {"owner": self.owner.as_hex, "calls": int(self.calls),
                "balance": str(int(gl.get_contract_at(gl.message.contract_address).balance))}
