"""Manual Mininet-WiFi smoke check for UdpTransport (WP4.7).

Skipped unless running on Linux with LIFELINE_EMULATION=1. Run it by hand
inside two stations; see the commands in the notes below.

Environment:
  LIFELINE_SMOKE_ROLE       "send" or "recv" (default recv)
  LIFELINE_SMOKE_OWN        this station's IP, e.g. 10.0.0.1 (required)
  LIFELINE_SMOKE_BROADCAST  subnet broadcast, default 10.0.0.255
  LIFELINE_SMOKE_PORT       default 5005
"""

from __future__ import annotations

import asyncio
import os
import platform
import time

import pytest

from lifeline.clock import SystemClock
from lifeline.rng import Rng
from lifeline.transport.udp import UdpTransport

ENABLED = platform.system() == "Linux" and os.environ.get("LIFELINE_EMULATION") == "1"
pytestmark = pytest.mark.skipif(
    not ENABLED, reason="set LIFELINE_EMULATION=1 on Linux inside Mininet-WiFi"
)

ROLE = os.environ.get("LIFELINE_SMOKE_ROLE", "recv")
OWN = os.environ.get("LIFELINE_SMOKE_OWN", "")
BROADCAST = os.environ.get("LIFELINE_SMOKE_BROADCAST", "10.0.0.255")
PORT = int(os.environ.get("LIFELINE_SMOKE_PORT", "5005"))
TOKEN = b"LIFELINE-SMOKE:"


async def test_station_broadcast_round_trip():
    assert OWN, "set LIFELINE_SMOKE_OWN to this station's IP"
    t = UdpTransport("0.0.0.0", PORT, SystemClock(), Rng(1).child("smoke"),
                     broadcast_addr=BROADCAST, own_addrs=(OWN,))
    await t.start()
    try:
        if ROLE == "send":
            for i in range(20):
                t.send(TOKEN + str(i).encode())
                await asyncio.sleep(0.1)
            assert t.counters["sent"] == 20, t.counters
            assert t.counters["send_errors"] == 0, t.counters
        else:
            deadline = time.monotonic() + 20
            got = []
            while time.monotonic() < deadline and len(got) < 5:
                try:
                    frame, src = await asyncio.wait_for(t.recv(), 1.0)
                except asyncio.TimeoutError:
                    continue
                if frame.startswith(TOKEN):
                    got.append((frame, src))
            assert len(got) >= 5, f"received {len(got)} smoke frames; counters={t.counters}"
            assert all(src != OWN for _, src in got)
    finally:
        t.close()