from __future__ import annotations

import asyncio
from typing import Callable, Optional

from lifeline.clock import Clock
from lifeline.config import NodeConfig
from lifeline.envelope import Envelope, MsgType
from lifeline.eventlog import EventLogger
from lifeline.store import MessageKey, MessageStore

_MSG_TYPE_NAMES = {
    MsgType.HELLO: "HELLO",
    MsgType.TEXT: "TEXT",
    MsgType.SOS: "SOS",
    MsgType.ACK: "ACK",
    MsgType.KEY_REQUEST: "KEY_REQUEST",
    MsgType.KEY_REPLY: "KEY_REPLY",
}


class ReofferScheduler:
    """
    WP7 Re-offer Scheduler (Architecture Section 5.4, Decision D4).
    Drives periodic exponential-backoff retransmissions, new-neighbor
    Store-Carry-Forward encounters, ACK buffer purges, and D4 backoff resets.
    """

    def __init__(
        self,
        config: NodeConfig,
        clock: Clock,
        store: MessageStore,
        logger: EventLogger,
        submit_to_flood: Optional[Callable[[Envelope], None]] = None,
        on_expire_check: Optional[Callable[[float], None]] = None,
        tick_interval_s: float = 1.0,
    ) -> None:
        self.config = config
        self.clock = clock
        self.store = store
        self.logger = logger
        self.submit_to_flood = submit_to_flood
        self.on_expire_check = on_expire_check
        self.tick_interval_s = tick_interval_s
        self._running = False

    def _next_delay(self, attempt: int) -> float:
        """Exponential backoff: initial * (factor ** attempt)."""
        return self.config.reoffer_initial_s * (self.config.reoffer_factor ** attempt)

    def _schedule_next(self, attempts: int, now: float) -> float:
        if attempts >= self.config.reoffer_max_attempts:
            return float("inf")
        return now + self._next_delay(attempts)

    def step(self) -> int:
        """
        Runs one deterministic scheduler tick at clock.now().
        Returns the number of messages re-offered on this tick.
        """
        now = self.clock.now()

        # 1. Purge entries past retention_s
        expired = self.store.purge_expired(now)
        for env in expired:
            self.logger.log(
                "BUF_EVICT",
                mid=env.msg_id,
                type=_MSG_TYPE_NAMES.get(env.msg_type, "UNKNOWN"),
                buffer_size=len(self.store.messages),
            )

        if self.on_expire_check is not None:
            self.on_expire_check(now)

        # 2. Re-offer any due buffered messages
        reoffered_count = 0
        for record in self.store.due(now):
            env = record.envelope
            if env.msg_type == MsgType.HELLO:
                record.next_reoffer_time = float("inf")
                continue

            if record.reoffer_attempts >= self.config.reoffer_max_attempts:
                record.next_reoffer_time = float("inf")
                continue

            record.reoffer_attempts += 1
            record.next_reoffer_time = self._schedule_next(record.reoffer_attempts, now)

            if self.submit_to_flood is not None:
                self.submit_to_flood(env)

            self.logger.log(
                "REOFFER",
                mid=env.msg_id,
                attempt=record.reoffer_attempts,
                trigger="timer",
            )
            reoffered_count += 1

        return reoffered_count

    async def run(self) -> None:
        """Background asyncio loop driven by Clock.sleep()."""
        self._running = True
        try:
            while self._running:
                await self.clock.sleep(self.tick_interval_s)
                if not self._running:
                    break
                self.step()
        except asyncio.CancelledError:
            self._running = False

    def stop(self) -> None:
        self._running = False

    def on_new_neighbor(self, peer_node_id: bytes) -> int:
        """
        Contract C2.6 encounter callback (Store-Carry-Forward).
        Resets per-encounter attempts and immediately re-offers buffered messages.
        """
        _ = peer_node_id  # Neighbor identity logged by NeighborManager
        now = self.clock.now()
        if self.config.reoffer_max_attempts <= 0:
            return 0

        reoffered_count = 0
        # Snapshot values in case submit_to_flood mutates store
        for record in list(self.store.messages.values()):
            env = record.envelope
            if env.msg_type == MsgType.HELLO:
                continue

            record.reoffer_attempts = 1
            record.next_reoffer_time = self._schedule_next(record.reoffer_attempts, now)

            if self.submit_to_flood is not None:
                self.submit_to_flood(env)

            self.logger.log(
                "REOFFER",
                mid=env.msg_id,
                attempt=record.reoffer_attempts,
                trigger="encounter",
            )
            reoffered_count += 1

        return reoffered_count

    def on_ack_overheard(self, acked_mid: bytes) -> bool:
        """
        Purges a buffered message when an end-to-end ACK is overheard on the medium.
        Returns True if a message was removed from the store.
        """
        removed = self.store.remove_by_mid(acked_mid)
        if removed is None:
            return False

        self.logger.log(
            "BUF_EVICT",
            mid=removed.msg_id,
            type=_MSG_TYPE_NAMES.get(removed.msg_type, "UNKNOWN"),
            buffer_size=len(self.store.messages),
        )
        return True

    def on_neighbor_rebroadcast(self, sender_pubkey: bytes, seq_num: int) -> bool:
        """
        Decision D4: Overhearing a neighbor rebroadcast counts as hop-level
        confirmation and resets the backoff timer.
        """
        key: MessageKey = (sender_pubkey, seq_num)
        record = self.store.get_record(key)
        if record is None:
            return False

        now = self.clock.now()
        record.reoffer_attempts = 0
        record.next_reoffer_time = now + self.config.reoffer_initial_s
        return True