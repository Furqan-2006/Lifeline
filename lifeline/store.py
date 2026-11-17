from typing import Dict, Tuple, List, Optional
from lifeline.envelope import Envelope

# Contract 2.5: Messages are keyed by (sender public key, sequence number)
MessageKey = Tuple[bytes, int]


class MessageRecord:
    __slots__ = ["envelope", "received_at", "reoffer_attempts", "next_reoffer_time"]

    def __init__(self, envelope: Envelope, now: float, next_reoffer_time: float):
        self.envelope = envelope
        self.received_at = now
        self.reoffer_attempts = 0
        self.next_reoffer_time = next_reoffer_time


class MessageStore:
    def __init__(
        self,
        capacity: int = 200,
        retention_s: float = 1800.0,
        use_priorities: bool = True,
        reoffer_initial_s: float = 5.0,
    ):
        self.capacity = capacity
        self.retention_s = retention_s
        self.use_priorities = use_priorities
        self.reoffer_initial_s = reoffer_initial_s

        # Primary O(1) store: (sender_pubkey, seq_num) -> MessageRecord
        self.messages: Dict[MessageKey, MessageRecord] = {}
        # Secondary O(1) index: 8-byte msg_id -> MessageKey (for ACK purging)
        self._by_mid: Dict[bytes, MessageKey] = {}

    def contains(self, key: MessageKey) -> bool:
        """O(1) check if we already have this message."""
        return key in self.messages

    def get(self, key: MessageKey) -> Optional[Envelope]:
        record = self.messages.get(key)
        return record.envelope if record else None

    def get_record(self, key: MessageKey) -> Optional[MessageRecord]:
        return self.messages.get(key)

    def remove(self, key: MessageKey) -> Optional[Envelope]:
        """Removes a message by (sender_pubkey, seq_num)."""
        record = self.messages.pop(key, None)
        if record is None:
            return None
        self._by_mid.pop(record.envelope.msg_id, None)
        return record.envelope

    def remove_by_mid(self, mid: bytes) -> Optional[Envelope]:
        """Removes a message by its 8-byte msg_id (used when an ACK arrives)."""
        key = self._by_mid.get(mid)
        if key is None:
            return None
        return self.remove(key)

    def put(self, envelope: Envelope, now: float) -> List[MessageKey]:
        """
        Stores the message.
        If at capacity:
          - If use_priorities=True (relay): evicts lowest priority (highest int), then oldest.
          - If use_priorities=False (baseline): evicts oldest regardless of priority.
        Returns a list of evicted MessageKeys.
        """
        key: MessageKey = (envelope.sender_pubkey, envelope.seq_num)

        # 1. Duplicate check
        if key in self.messages:
            return []

        first_reoffer = now + self.reoffer_initial_s

        # 2. Space available
        if len(self.messages) < self.capacity:
            self.messages[key] = MessageRecord(envelope, now, first_reoffer)
            self._by_mid[envelope.msg_id] = key
            return []

        if self.capacity <= 0:
            return []

        # 3. Buffer full: O(N) linear scan for the absolute worst candidate
        worst_key: Optional[MessageKey] = None
        worst_priority = -1
        oldest_time = float("inf")

        for k, record in self.messages.items():
            prio = record.envelope.priority if self.use_priorities else 0
            t = record.received_at

            if prio > worst_priority:
                worst_priority = prio
                oldest_time = t
                worst_key = k
            elif prio == worst_priority and t < oldest_time:
                oldest_time = t
                worst_key = k

        # 4. Evict or reject incoming
        if worst_key is not None:
            incoming_prio = envelope.priority if self.use_priorities else 0
            if incoming_prio > worst_priority:
                return []  # Incoming is strictly worse than everything in buffer

            self.remove(worst_key)
            self.messages[key] = MessageRecord(envelope, now, first_reoffer)
            self._by_mid[envelope.msg_id] = key
            return [worst_key]

        return []

    def purge_expired(self, now: float) -> List[Envelope]:
        """Removes entries past retention time and returns the expired envelopes."""
        expired_keys: List[MessageKey] = []
        for key, record in self.messages.items():
            if now - record.received_at > self.retention_s:
                expired_keys.append(key)

        expired_envelopes: List[Envelope] = []
        for key in expired_keys:
            env = self.remove(key)
            if env is not None:
                expired_envelopes.append(env)
        return expired_envelopes

    def due(self, now: float) -> List[MessageRecord]:
        """Returns messages whose re-offer time has arrived."""
        due_records: List[MessageRecord] = []
        for record in self.messages.values():
            if now >= record.next_reoffer_time:
                due_records.append(record)
        return due_records