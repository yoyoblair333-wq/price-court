"""
Direct-mode tests for PythReader.

The real GenVM Python SDK runs in-process. The Solana JSON-RPC getAccountInfo call is
mocked by URL with byte-exact PriceUpdateV2 accounts (mock_web). The validator captured
by gl.vm.run_nondet_unsafe is replayed with direct_vm.run_validator() after swapping
mocks, to model what an independent validator would see.

Run from the repo root:  pytest primitives/pyth_reader/tests -v
"""

import pytest

from pyth_mocks import (ACCOUNTS, CONTRACT, FEED_IDS, SOLANA_FALLBACK_RE, SOLANA_PRIMARY_RE, addr,
                        epoch, iso, mock_missing_account, mock_pyth, set_chain_time)

NOW = epoch("2026-11-10T00:00:00Z")


@pytest.fixture
def reader(direct_vm, direct_deploy, direct_owner):
    direct_vm.sender = direct_owner
    c = direct_deploy(CONTRACT, 300)
    set_chain_time(iso(NOW))
    return c


def refresh(vm, c, feed="BTC/USD", at=NOW, **pyth):
    vm.clear_mocks()
    set_chain_time(iso(at))
    pyth.setdefault("publish_time", at - 5)
    pyth.setdefault("feed", feed if feed in FEED_IDS else "BTC/USD")
    mock_pyth(vm, **pyth)
    return c.refresh(feed)


# ------------------------------------------------------------------ happy path

class TestRefresh:
    def test_refresh_stores_verified_price(self, direct_vm, reader):
        out = refresh(direct_vm, reader, price=9_612_345_000_000, conf=4_200_000_000)
        assert out["available"] is True and out["last_error"] == ""
        assert out["price"] == "96123.45" and out["price_raw"] == "9612345000000"
        assert out["conf"] == "42" and out["expo"] == -8
        assert out["publish_time"] == NOW - 5
        assert out["feed_id"] == "0x" + FEED_IDS["BTC/USD"]
        assert out["source"] == "pyth-solana-receiver:" + ACCOUNTS["BTC/USD"]
        assert out["refreshes"] == 1 and out["attempts"] == 1
        assert reader.get_price("BTC/USD") == {
            "feed": "BTC/USD", "price": "96123.45", "price_raw": "9612345000000",
            "conf_raw": "4200000000", "expo": -8, "publish_time": NOW - 5}

    @pytest.mark.parametrize("feed", ["ETH/USD", "SOL/USD"])
    def test_every_default_feed_refreshes(self, direct_vm, reader, feed):
        out = refresh(direct_vm, reader, feed=feed, price=250_000_000_000)
        assert out["feed"] == feed and out["price"] == "2500"

    @pytest.mark.parametrize("spelling", ["btc/usd", "BTC-USD", " BTCUSD ", "btc_usd"])
    def test_feed_name_is_normalised(self, direct_vm, reader, spelling):
        out = refresh(direct_vm, reader, feed=spelling)
        assert out["feed"] == "BTC/USD" and out["available"] is True
        assert reader.latest(spelling)["feed"] == "BTC/USD"

    def test_newer_price_replaces_older(self, direct_vm, reader):
        refresh(direct_vm, reader, price=9_600_000_000_000)
        out = refresh(direct_vm, reader, at=NOW + 60, price=9_700_000_000_000)
        assert out["price"] == "97000" and out["publish_time"] == NOW + 55 and out["refreshes"] == 2

    def test_same_or_older_price_is_not_stored(self, direct_vm, reader):
        refresh(direct_vm, reader, price=9_600_000_000_000)
        out = refresh(direct_vm, reader, at=NOW + 10, publish_time=NOW - 5, price=1_000_000_000_000)
        assert out["price"] == "96000" and out["last_error"] == "no newer Pyth price than the stored one"
        assert out["refreshes"] == 1 and out["attempts"] == 2

    def test_stale_price_is_not_stored(self, direct_vm, reader):
        out = refresh(direct_vm, reader, publish_time=NOW - 301)
        assert out["available"] is False and "older than max_staleness_seconds" in out["last_error"]
        out = refresh(direct_vm, reader, publish_time=NOW - 300)
        assert out["available"] is True

    def test_success_clears_previous_error(self, direct_vm, reader):
        refresh(direct_vm, reader, status=503)
        assert reader.latest("BTC/USD")["last_error"].startswith("price unavailable")
        out = refresh(direct_vm, reader, at=NOW + 5)
        assert out["last_error"] == "" and out["available"] is True

    def test_fallback_rpc_is_used_when_primary_fails(self, direct_vm, reader):
        direct_vm.clear_mocks()
        mock_pyth(direct_vm, status=500, url_re=SOLANA_PRIMARY_RE)
        mock_pyth(direct_vm, url_re=SOLANA_FALLBACK_RE, publish_time=NOW - 1)
        out = reader.refresh("BTC/USD")
        assert out["available"] is True and out["publish_time"] == NOW - 1

    def test_positive_exponent_formats_exactly(self, direct_vm, reader):
        out = refresh(direct_vm, reader, price=12, expo=3)
        assert out["price"] == "12000"


# ------------------------------------------------------------------ verification

class TestVerification:
    @pytest.mark.parametrize("kw,reason", [
        ({"owner": "11111111111111111111111111111111"}, "not owned by the Pyth receiver"),
        ({"feed_id": FEED_IDS["ETH/USD"]}, "different feed"),
        ({"verification": 0}, "not fully verified"),
        ({"discriminator": bytes(8)}, "not a Pyth PriceUpdateV2 account"),
        ({"price": 0}, "non-positive price"),
        ({"price": -5}, "non-positive price"),
        ({"status": 503}, "rpc http 503"),
    ])
    def test_bad_account_stores_nothing(self, direct_vm, reader, kw, reason):
        out = refresh(direct_vm, reader, **kw)
        assert out["available"] is False and out["price"] == ""
        assert reason in out["last_error"] and out["attempts"] == 1 and out["refreshes"] == 0
        with direct_vm.expect_revert("no verified price stored"):
            reader.get_price("BTC/USD")

    def test_missing_account(self, direct_vm, reader):
        direct_vm.clear_mocks()
        mock_missing_account(direct_vm)
        out = reader.refresh("BTC/USD")
        assert "price account not found" in out["last_error"]

    def test_unreachable_rpc(self, direct_vm, reader):
        direct_vm.clear_mocks()                 # no mock at all: the request itself fails
        out = reader.refresh("BTC/USD")
        assert out["available"] is False and out["last_error"].startswith("price unavailable")

    def test_bad_account_does_not_overwrite_good_price(self, direct_vm, reader):
        refresh(direct_vm, reader, price=9_600_000_000_000)
        out = refresh(direct_vm, reader, at=NOW + 30, verification=0)
        assert out["price"] == "96000" and "not fully verified" in out["last_error"]

    def test_unknown_feed_reverts(self, direct_vm, reader):
        with direct_vm.expect_revert("not on the whitelist"):
            reader.refresh("DOGE/USD")
        with direct_vm.expect_revert("BASE/QUOTE"):
            reader.latest("not a feed!")


# ------------------------------------------------------------------ consensus

class TestConsensus:
    def _leader(self, vm, reader, **kw):
        refresh(vm, reader, **kw)
        vm.clear_mocks()

    def test_identical_reading_agrees(self, direct_vm, reader):
        self._leader(direct_vm, reader)
        mock_pyth(direct_vm, publish_time=NOW - 5)
        assert direct_vm.run_validator() is True

    def test_within_tolerance_agrees(self, direct_vm, reader):
        self._leader(direct_vm, reader)
        mock_pyth(direct_vm, price=9_620_000_000_000, publish_time=NOW + 20)   # +0.21%
        assert direct_vm.run_validator() is True

    def test_beyond_tolerance_disagrees(self, direct_vm, reader):
        self._leader(direct_vm, reader)
        mock_pyth(direct_vm, price=9_630_000_000_000, publish_time=NOW + 20)   # +0.31%
        assert direct_vm.run_validator() is False

    def test_different_exponent_same_value_agrees(self, direct_vm, reader):
        self._leader(direct_vm, reader)
        mock_pyth(direct_vm, price=96_000_000, expo=-3, publish_time=NOW - 5)
        assert direct_vm.run_validator() is True

    def test_publish_time_skew_disagrees(self, direct_vm, reader):
        self._leader(direct_vm, reader)
        mock_pyth(direct_vm, publish_time=NOW + 200)
        assert direct_vm.run_validator() is False

    def test_validator_cannot_read_but_leader_did_disagrees(self, direct_vm, reader):
        self._leader(direct_vm, reader)
        mock_pyth(direct_vm, status=500)
        assert direct_vm.run_validator() is False

    def test_leader_unavailable_accepted_only_if_validator_also_fails(self, direct_vm, reader):
        self._leader(direct_vm, reader, status=503)
        mock_pyth(direct_vm, status=503)
        assert direct_vm.run_validator() is True
        direct_vm.clear_mocks()
        mock_pyth(direct_vm, publish_time=NOW - 5)
        assert direct_vm.run_validator() is False

    def test_forged_leader_price_rejected(self, direct_vm, reader):
        self._leader(direct_vm, reader)
        mock_pyth(direct_vm, publish_time=NOW - 5)
        forged = {"ok": True, "price": "9000000000000", "conf": "1", "expo": -8, "publish_time": NOW - 5}
        assert direct_vm.run_validator(leader_result=forged) is False

    def test_leader_error_rejected(self, direct_vm, reader):
        self._leader(direct_vm, reader)
        mock_pyth(direct_vm, publish_time=NOW - 5)
        assert direct_vm.run_validator(leader_error=Exception("boom")) is False


# ------------------------------------------------------------------ admin + views

class TestAdmin:
    NEW_ID = "ab" * 32
    NEW_ACCT = "7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiF"

    def test_owner_adds_feed_and_it_refreshes(self, direct_vm, reader, direct_owner):
        direct_vm.sender = direct_owner
        out = reader.add_feed("xau-usd", "0x" + self.NEW_ID, self.NEW_ACCT)
        assert out["feed"] == "XAU/USD" and out["available"] is False
        assert reader.get_config()["feeds"]["XAU/USD"] == {"feed_id": "0x" + self.NEW_ID,
                                                           "solana_account": self.NEW_ACCT}
        out = refresh(direct_vm, reader, feed="XAU/USD", feed_id=self.NEW_ID, price=270_000_000_000)
        assert out["price"] == "2700"
        assert [f["feed"] for f in reader.list_feeds()] == ["BTC/USD", "ETH/USD", "SOL/USD", "XAU/USD"]

    def test_non_owner_cannot_add_feed(self, direct_vm, reader, direct_alice):
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("only the owner"):
            reader.add_feed("XAU/USD", self.NEW_ID, self.NEW_ACCT)

    @pytest.mark.parametrize("fid,acct,msg", [("12", NEW_ACCT, "32 bytes of hex"),
                                              (NEW_ID, "0OIl" * 10, "base58"),
                                              (NEW_ID, "short", "base58")])
    def test_add_feed_validates_inputs(self, direct_vm, reader, direct_owner, fid, acct, msg):
        direct_vm.sender = direct_owner
        with direct_vm.expect_revert(msg):
            reader.add_feed("XAU/USD", fid, acct)

    def test_two_step_ownership(self, direct_vm, reader, direct_owner, direct_alice, direct_bob):
        direct_vm.sender = direct_owner
        reader.transfer_ownership(addr(direct_alice))
        direct_vm.sender = direct_bob
        with direct_vm.expect_revert("pending owner"):
            reader.accept_ownership()
        direct_vm.sender = direct_alice
        reader.accept_ownership()
        assert reader.get_config()["owner"].lower() == addr(direct_alice).lower()
        direct_vm.sender = direct_owner
        with direct_vm.expect_revert("only the owner"):
            reader.add_feed("XAU/USD", self.NEW_ID, self.NEW_ACCT)

    def test_config_and_empty_views(self, direct_vm, reader):
        cfg = reader.get_config()
        assert set(cfg["feeds"]) == {"BTC/USD", "ETH/USD", "SOL/USD"}
        assert cfg["max_staleness_seconds"] == 300 and cfg["price_tolerance_bps"] == 25
        assert reader.latest("ETH/USD")["available"] is False
        with direct_vm.expect_revert("call refresh first"):
            reader.get_price("ETH/USD")

    def test_constructor_bounds(self, direct_vm, direct_deploy, direct_owner):
        direct_vm.sender = direct_owner
        with direct_vm.expect_revert("max_staleness_seconds"):
            direct_deploy(CONTRACT, 5)
