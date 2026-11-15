"""Shared fixtures. Each test gets a fresh FakeClock and seeded Rng."""

import pytest

from lifeline.clock import FakeClock
from lifeline.rng import Rng


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=0.0)


@pytest.fixture
def rng() -> Rng:
    return Rng(seed=12345)