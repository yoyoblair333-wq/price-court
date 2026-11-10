"""Fixtures for the PythReader direct-mode suite. The GenVM SDK release is pinned so the
suite runs against the runner the contract's Depends header targets (override with
GENVM_VERSION)."""

import os

import pytest

GENVM_VERSION = os.environ.get("GENVM_VERSION") or "v0.2.16"


@pytest.fixture
def direct_deploy(direct_deploy):
    def _deploy(contract_path, *args, sdk_version=None, **kwargs):
        return direct_deploy(contract_path, *args, sdk_version=sdk_version or GENVM_VERSION, **kwargs)

    return _deploy
