"""The radio abstraction (contract C2.1).

A transport only moves opaque frames to every current neighbor. It never
parses a frame and never decides who a message is for; that is decided by
the envelope.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TransportClosed(Exception):
    """Raised by ``recv()`` once the transport has been closed."""


class Transport(ABC):
    """Broadcast medium as seen by one node."""

    @abstractmethod
    def send(self, frame: bytes) -> None:
        """Transmit ``frame`` to all current neighbors.

        Non-blocking. Never raises on loss; a lost frame is simply not
        delivered. A call after ``close()`` is a silent no-op.
        """

    @abstractmethod
    async def recv(self) -> tuple[bytes, str]:
        """Return the next received frame and an opaque transmitter label.

        Raises ``TransportClosed`` once the transport is closed.
        """

    @abstractmethod
    def close(self) -> None:
        """Stop the transport and release its resources. Idempotent."""