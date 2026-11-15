"""Seeded randomness. The only module in ``lifeline/`` allowed to import ``random``.

Test setup::

    from lifeline.rng import Rng
    root = Rng(seed=42)
    node_rng = root.child("n01")   # independent of how many draws root made

A child stream depends only on the parent's seed and the child name, never
on the parent's draw history.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import MutableSequence, Sequence
from typing import TypeVar

T = TypeVar("T")


def _derive_seed(parent_seed: int, name: str) -> int:
    """Derive a child seed from (parent seed, name) with SHA-256.

    Proposed default for contract question 2; isolated here so changing the
    derivation is a one-line edit.
    """
    material = parent_seed.to_bytes(8, "big", signed=False) + name.encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


class Rng:
    """Deterministic random source with independent named children."""

    def __init__(self, seed: int) -> None:
        if not isinstance(seed, int) or seed < 0 or seed >= 2**64:
            raise ValueError("seed must be an integer in [0, 2**64)")
        self._seed = seed
        self._rng = random.Random(seed)

    @property
    def seed(self) -> int:
        return self._seed

    def random(self) -> float:
        """Float in [0.0, 1.0)."""
        return self._rng.random()

    def uniform(self, a: float, b: float) -> float:
        return self._rng.uniform(a, b)

    def randint(self, a: int, b: int) -> int:
        """Integer in [a, b], inclusive."""
        return self._rng.randint(a, b)

    def choice(self, seq: Sequence[T]) -> T:
        return self._rng.choice(seq)

    def shuffle(self, seq: MutableSequence[T]) -> None:
        self._rng.shuffle(seq)

    def child(self, name: str) -> "Rng":
        """Independent stream named ``name``, derived from this seed only."""
        return Rng(_derive_seed(self._seed, name))