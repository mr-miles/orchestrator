"""Suite-wide test settings, and each test's kind (a marker: ``-m unit``, ``-m scenario``,
``-m e2e``; ``mise run test:fast`` runs all but e2e):

* ``e2e`` – the real controller over HTTP with fake agents (``test_e2e.py``) and the telemetry
  acceptance tests (``acceptance/``);
* ``scenario`` – a change driven through the engine by the harness (``@scenario``);
* ``unit`` – everything else."""
from __future__ import annotations

import itertools

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from legion_orch.identity.spiffe import DevSpire

_POOL = 4      # keys per worker: consecutive dev SPIREs (a forger, a restart) still differ
_keys: list = []
_turn = itertools.count()


def _pooled_key(self: DevSpire) -> None:
    i = next(_turn) % _POOL
    if i == len(_keys):
        _keys.append(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    self._key = _keys[i]


@pytest.fixture(autouse=True)
def _dev_spire_keys(monkeypatch):
    """Every controller's dev SPIRE makes a 2048-bit RSA key (~35 ms): draw them from a small
    pool instead. Any two dev SPIREs made one after the other still sign with different keys."""
    monkeypatch.setattr(DevSpire, "__post_init__", _pooled_key)


E2E = ("test_e2e.py", "acceptance")


def pytest_collection_modifyitems(items):
    for item in items:
        if item.get_closest_marker("scenario") or item.get_closest_marker("e2e"):
            continue
        path = item.path.relative_to(item.config.rootpath / "tests").parts
        item.add_marker(pytest.mark.e2e if path[0] in E2E else pytest.mark.unit)


@pytest.fixture
def anyio_backend():
    """Async tests (``@pytest.mark.anyio``) run on asyncio, as the controller does."""
    return "asyncio"
