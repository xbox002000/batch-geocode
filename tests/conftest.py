import os
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(__file__))

import pytest


@pytest.fixture(autouse=True)
def _no_test_base(monkeypatch):
    """Unit tests use httpx.MockTransport against the real hostnames; ignore any E2E mock routing."""
    monkeypatch.delenv("GEOCODE_TEST_BASE", raising=False)
