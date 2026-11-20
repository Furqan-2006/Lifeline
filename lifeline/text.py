import hashlib
import struct
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from lifeline.clock import Clock
from lifeline.config import NodeConfig
from lifeline.crypto import node_id_of, open_msg, seal, sign
from lifeline.envelope import (
    BASE_HEADER_FORMAT,
    LIFELINE_VERSION,
    MAX_PAYLOAD_LEN,
    SIGNED_BYTES_PREFIX,
    TEXT_EXT_FORMAT,
    Envelope,
    MsgType,
)
from lifeline.eventlog import EventLogger
from lifeline.store import MessageStore

# Contract 1.5: Header bytes 0..64 with TTL (byte 3) removed -> 64 bytes total
AAD_FORMAT = struct.Struct(">B B B 8s I 32s 16s B")
POLY1305_TAG_LEN = 16

# Priorities from Contract 1.3
PRIO_ACK = 1
PRIO_TEXT = 2


@dataclass(slots=True)
class RetainedText:
    msg_id: bytes
    seq_num: int
    recipient_node_id: bytes
    created_at: float
    envelope: Envelope


def build_aad(
    version: int,
    msg_type: int,
    priority: int,
    msg_id: bytes,
    seq_num: int,
    sender_pubkey: bytes,
    recipient_node_id: bytes,
    payload_len: int,
) -> bytes:
    """Builds the 64-byte Associated Data (base header without TTL) per Contract 1.5."""
    return AAD_FORMAT.pack(
        version,
        msg_type,
        priority,
        msg_id,
        seq_num,
        sender_pubkey,
        recipient_node_id,
        payload_len,
    )


def compute_signed_bytes_from_envelope(env: Envelope) -> bytes:
    """
    Contract 1.5: 'LIFELINE-v1' + bytes 0..2 + bytes 4..(start of signature).
    Computes the signed bytes directly from an Envelope before signing.
    """
    base = BASE_HEADER_FORMAT.pack(
        env.version,
        env.msg_type,
        env.priority,
        env.ttl,
        env.msg_id,
        env.seq_num,
        env.sender_pubkey,
        env.recipient_node_id,
        env.payload_len,
    )
    ext = b""
    if env.msg_type == MsgType.TEXT:
        if env.ephemeral_pubkey is None or env.nonce is None:
            raise ValueError("TEXT envelope missing ephemeral_pubkey or nonce")
        ext = TEXT_EXT_FORMAT.pack(env.ephemeral_pubkey, env.nonce)

    # Strip TTL (byte 3): base[0:3] + base[4:]
    return SIGNED_BYTES_PREFIX + base[0:3] + base[4:] + ext + env.payload


class TextService:
    def __init__(
        self,
        identity: ed25519.Ed25519PrivateKey,
        config: NodeConfig,
        clock: Clock,
        store: MessageStore,
        logger: EventLogger,
        submit_to_flood: Optional[Callable[[Envelope], None]] = None,
        on_text_received: Optional[Callable[[bytes, str], None]] = None,
        resolve_pubkey: Optional[Callable[[bytes], Optional[bytes]]] = None,
    ) -> None:
        self.identity = identity
        self.my_pubkey = identity.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.my_node_id = node_id_of(self.my_pubkey)
        self.config = config
        self.clock = clock
        self.store = store
        self.logger = logger

        # Contract C2.6 & C2 callbacks (can be wired by node.py or stubbed in tests)
        self.submit_to_flood = submit_to_flood
        self.on_text_received = on_text_received  # callback(sender_node_id, plaintext_str)
        self.resolve_pubkey = resolve_pubkey      # callback(node_id) -> 32-byte pubkey or None

        self.seq_counter = 1
        self.retained: Dict[bytes, RetainedText] = {}  # msg_id -> RetainedText

    def _next_seq_and_mid(self) -> tuple[int, bytes]:
        seq = self.seq_counter
        self.seq_counter += 1
        seq_bytes = struct.pack(">I", seq)
        mid = hashlib.sha256(self.my_pubkey + seq_bytes).digest()[0:8]
        return seq, mid

    def send_text(self, recipient_pub_bytes: bytes, text: str) -> bytes:
        """
        Composes, seals, signs, stores, and submits an encrypted TEXT envelope.
        Returns the 8-byte msg_id.
        """
        plaintext = text.encode("utf-8")
        ciphertext_len = len(plaintext) + POLY1305_TAG_LEN
        if ciphertext_len > MAX_PAYLOAD_LEN:
            raise ValueError(
                f"Plaintext too large: encrypted size {ciphertext_len} > {MAX_PAYLOAD_LEN}"
            )

        recipient_node_id = node_id_of(recipient_pub_bytes)
        seq, msg_id = self._next_seq_and_mid()

        # 1. Build AAD (header without TTL) using the known ciphertext length
        aad = build_aad(
            LIFELINE_VERSION,
            MsgType.TEXT,
            PRIO_TEXT,
            msg_id,
            seq,
            self.my_pubkey,
            recipient_node_id,
            ciphertext_len,
        )

        # 2. Seal with ECIES (WP3)
        ephemeral_pub, nonce, ciphertext = seal(recipient_pub_bytes, plaintext, aad)

        # 3. Assemble & Sign Envelope
        env = Envelope(
            version=LIFELINE_VERSION,
            msg_type=MsgType.TEXT,
            priority=PRIO_TEXT,
            ttl=self.config.default_ttl,
            msg_id=msg_id,
            seq_num=seq,
            sender_pubkey=self.my_pubkey,
            recipient_node_id=recipient_node_id,
            payload_len=len(ciphertext),
            ephemeral_pubkey=ephemeral_pub,
            nonce=nonce,
            payload=ciphertext,
        )
        env.signature = sign(self.identity, compute_signed_bytes_from_envelope(env))

        # 4. Retain outgoing text & store in bounded buffer for re-offers
        now = self.clock.now()
        self.retained[msg_id] = RetainedText(
            msg_id=msg_id,
            seq_num=seq,
            recipient_node_id=recipient_node_id,
            created_at=now,
            envelope=env,
        )
        self._store_with_logging(env, now)

        # 5. Log TEXT_SEND (C3.2) and submit to flood engine
        self.logger.log(
            "TEXT_SEND",
            mid=msg_id,
            recipient=recipient_node_id,
            size=len(plaintext),
        )
        if self.submit_to_flood is not None:
            self.submit_to_flood(env)

        return msg_id

    def on_deliver(self, env: Envelope) -> None:
        """
        Called by Flood Engine (C2.6) when a verified TEXT or ACK for this node arrives.
        """
        if env.msg_type == MsgType.TEXT:
            self._handle_incoming_text(env)
        elif env.msg_type == MsgType.ACK:
            self._handle_incoming_ack(env)

    def _handle_incoming_text(self, env: Envelope) -> None:
        if env.ephemeral_pubkey is None or env.nonce is None:
            return

        aad = build_aad(
            env.version,
            env.msg_type,
            env.priority,
            env.msg_id,
            env.seq_num,
            env.sender_pubkey,
            env.recipient_node_id,
            env.payload_len,
        )
        plaintext_bytes = open_msg(
            self.identity, env.ephemeral_pubkey, env.nonce, env.payload, aad
        )
        if plaintext_bytes is None:
            return  # Decryption/auth failed; do not ACK

        # Deliver to application callback
        sender_node_id = node_id_of(env.sender_pubkey)
        if self.on_text_received is not None:
            self.on_text_received(sender_node_id, plaintext_bytes.decode("utf-8", errors="replace"))

        # Send End-to-End ACK back to original sender (Contract 1.4)
        self._send_ack(acked_mid=env.msg_id, recipient_node_id=sender_node_id)

    def _send_ack(self, acked_mid: bytes, recipient_node_id: bytes) -> None:
        if len(acked_mid) != 8:
            return

        seq, ack_mid = self._next_seq_and_mid()
        env = Envelope(
            version=LIFELINE_VERSION,
            msg_type=MsgType.ACK,
            priority=PRIO_ACK,
            ttl=self.config.default_ttl,
            msg_id=ack_mid,
            seq_num=seq,
            sender_pubkey=self.my_pubkey,
            recipient_node_id=recipient_node_id,
            payload_len=len(acked_mid),
            payload=acked_mid,
        )
        env.signature = sign(self.identity, compute_signed_bytes_from_envelope(env))

        if self.submit_to_flood is not None:
            self.submit_to_flood(env)

    def _handle_incoming_ack(self, env: Envelope) -> None:
        if len(env.payload) != 8:
            return

        acked_mid = env.payload
        if acked_mid not in self.retained:
            return

        # 1. Remove from retained outgoing texts & bounded store
        del self.retained[acked_mid]
        self.store.remove_by_mid(acked_mid)

        # 2. Log ACK_RX and TEXT_END (Contract 3.2)
        self.logger.log("ACK_RX", mid=acked_mid)
        self.logger.log("TEXT_END", mid=acked_mid, outcome="delivered")

    def expire_retained(self, now: float) -> None:
        """Marks retained outgoing texts past retention_s as expired (for metrics)."""
        expired_mids = [
            mid
            for mid, rec in self.retained.items()
            if now - rec.created_at > self.config.retention_s
        ]
        for mid in expired_mids:
            del self.retained[mid]
            self.logger.log("TEXT_END", mid=mid, outcome="expired")

    def _store_with_logging(self, env: Envelope, now: float) -> None:
        evicted_keys = self.store.put(env, now)
        for ev_key in evicted_keys:
            # ev_key is (sender_pubkey, seq_num); log BUF_EVICT per C3.2
            self.logger.log(
                "BUF_EVICT",
                mid=ev_key[0][:8],  # Note: if needed, store.put can return evicted Envelopes
                type="UNKNOWN",
                buffer_size=len(self.store.messages),
            )
        if self.store.contains((env.sender_pubkey, env.seq_num)):
            self.logger.log(
                "BUF_PUT",
                mid=env.msg_id,
                type="TEXT",
                buffer_size=len(self.store.messages),
            )