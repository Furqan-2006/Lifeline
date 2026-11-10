from typing import Dict, Tuple, List
from lifeline.envelope import Envelope, MsgType

# A unique identifier for a message: (Sender PubKey Bytes, Sequence Number)
MessageKey = Tuple[bytes, int]

class MessageRecord:
    __slots__ = ['envelope', 'received_at', 'reoffer_attempts', 'next_reoffer_time']
    
    def __init__(self, envelope: Envelope, now: float):
        self.envelope = envelope
        self.received_at = now
        self.reoffer_attempts = 0
        self.next_reoffer_time = 0.0  # Will be set by WP7 Re-offer scheduler

class MessageStore:
    def __init__(self, capacity: int = 200, retention_s: float = 1800.0):
        self.capacity = capacity
        self.retention_s = retention_s
        
        # Fast O(1) lookup dictionary. Key: (sender_pubkey, seq_num)
        self.messages: Dict[MessageKey, MessageRecord] = {}

    def contains(self, sender: bytes, seq: int) -> bool:
        """O(1) check if we already have this message (Duplicate suppression)."""
        return (sender, seq) in self.messages

    def get(self, sender: bytes, seq: int) -> Envelope:
        record = self.messages.get((sender, seq))
        return record.envelope if record else None

    def remove(self, sender: bytes, seq: int):
        """Used when an End-to-End ACK is received."""
        if (sender, seq) in self.messages:
            del self.messages[(sender, seq)]

    def put(self, envelope: Envelope, now: float) -> List[MessageKey]:
        """
        Stores the message. 
        If at capacity, evicts the lowest priority (highest integer) message,
        and the oldest within that priority.
        Returns a list of evicted MessageKeys.
        """
        key = (envelope.sender_pubkey, envelope.seq_num)
        
        # 1. If we already have it, do nothing (Duplicate suppression)
        if key in self.messages:
            return []

        # 2. If we have space, just add it!
        if len(self.messages) < self.capacity:
            self.messages[key] = MessageRecord(envelope, now)
            return []
            
        # 3. Buffer is full. Find the absolute WORST message in the store.
        # Worst = Highest priority integer. Tie-breaker = Oldest received_at time.
        worst_key = None
        worst_priority = -1
        oldest_time = float('inf')
        
        for k, record in self.messages.items():
            prio = record.envelope.priority
            time = record.received_at
            
            if prio > worst_priority:
                worst_priority = prio
                oldest_time = time
                worst_key = k
            elif prio == worst_priority and time < oldest_time:
                oldest_time = time
                worst_key = k
                
        # 4. Compare the worst stored message with the incoming message
        if worst_key is not None:
            # If incoming message is WORSE (higher int) than the worst thing in the buffer,
            # it gets rejected. (e.g., Buffer full of SOS, incoming is a HELLO).
            if envelope.priority > worst_priority:
                return [] # Incoming is dropped
                
            # If priorities are EQUAL, the incoming one is newer. 
            # We evict the older one from the buffer.
            
            # Evict the worst one and insert the new one
            del self.messages[worst_key]
            self.messages[key] = MessageRecord(envelope, now)
            return [worst_key]
            
        return []
    def purge_expired(self, now: float) -> None:
        """Removes entries past retention time."""
        # We must collect keys to delete first, because changing a dictionary 
        # while looping over it causes a RuntimeError in Python!
        expired_keys = []
        for key, record in self.messages.items():
            if now - record.received_at > self.retention_s:
                expired_keys.append(key)
                
        for key in expired_keys:
            del self.messages[key]

    def due(self, now: float) -> List[MessageRecord]:
        """Returns messages whose re-offer time has arrived."""
        due_records = []
        for record in self.messages.values():
            if now >= record.next_reoffer_time:
                due_records.append(record)
        return due_records