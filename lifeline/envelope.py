import struct
from dataclasses import dataclass
from typing import Optional

# Pre-compiled struct layouts for O(1) CPU serialization
# >  : Big-endian
# B  : unsigned char (1 byte)
# 8s : char[] (8 bytes)
# I  : unsigned int (4 bytes)
BASE_HEADER_FORMAT = struct.Struct(">B B B B 8s I 32s 16s B")
TEXT_EXT_FORMAT = struct.Struct(">32s 12s")

BASE_HEADER_LEN = BASE_HEADER_FORMAT.size  # Should be 65
TEXT_EXT_LEN = TEXT_EXT_FORMAT.size        # Should be 44
SIGNATURE_LEN = 64
MAX_PAYLOAD_LEN = 160

# Protocol Constants
LIFELINE_VERSION = 1
SIGNED_BYTES_PREFIX = b"LIFELINE-v1"

class MsgType:
    HELLO = 1
    TEXT = 2
    SOS = 3
    ACK = 4
    KEY_REQUEST = 5
    KEY_REPLY = 6

class DecodeError(Exception):
    """Raised on any validation failure. Handled by the flood engine (dropped)."""
    pass

@dataclass(slots=True)
class Envelope:
    version: int
    msg_type: int
    priority: int
    ttl: int
    msg_id: bytes             # 8 bytes
    seq_num: int              # 4 bytes unsigned
    sender_pubkey: bytes      # 32 bytes
    recipient_node_id: bytes  # 16 bytes
    payload_len: int
    
    # Optional extensions for TEXT (Type 2)
    ephemeral_pubkey: Optional[bytes] = None # 32 bytes
    nonce: Optional[bytes] = None            # 12 bytes
    
    payload: bytes = b""      # up to 160 bytes
    signature: bytes = b""    # 64 bytes

def signed_bytes(raw_frame: bytes) -> bytes:
    """
    Contract 1.5: The signed bytes are the ASCII prefix 'LIFELINE-v1', 
    then bytes 0 to 2 of the frame, then bytes 4 up to the start of the signature.
    (TTL at byte 3 and the 64-byte signature are excluded).
    """
    if len(raw_frame) < BASE_HEADER_LEN + SIGNATURE_LEN:
        raise DecodeError("Frame too small to contain signature.")
    
    # C-style memory slicing. O(1) byte extraction.
    prefix = SIGNED_BYTES_PREFIX
    bytes_0_to_2 = raw_frame[0:3]
    bytes_4_to_sig = raw_frame[4:-SIGNATURE_LEN]
    
    return prefix + bytes_0_to_2 + bytes_4_to_sig

def decode(raw_frame: bytes) -> Envelope:
    """
    Decodes raw bytes into an Envelope.
    Must enforce Contract 1.6 validation rules.
    """
    # Guard 1: Absolute minimum size (65 header + 0 payload + 64 sig)
    if len(raw_frame) < BASE_HEADER_LEN + SIGNATURE_LEN:
        raise DecodeError("Frame smaller than absolute minimum size (129 bytes).")

    # Unpack the fixed base header
    (version, msg_type, priority, ttl, msg_id, seq_num, 
     sender_pubkey, recipient, payload_len) = BASE_HEADER_FORMAT.unpack_from(raw_frame, 0)
    
    # Guard 2: Version
    if version != LIFELINE_VERSION:
        raise DecodeError(f"Unsupported protocol version: {version}")
        
    # Guard 3: Message Type Valid Range
    if not (MsgType.HELLO <= msg_type <= MsgType.KEY_REPLY):
        raise DecodeError(f"Unknown message type: {msg_type}")
        
    # Guard 4: Payload limits
    if payload_len > MAX_PAYLOAD_LEN:
        raise DecodeError(f"Payload length {payload_len} exceeds maximum 160.")
        
    # Guard 5: Frame Length match
    expected_len = BASE_HEADER_LEN + payload_len + SIGNATURE_LEN
    if msg_type == MsgType.TEXT:
        expected_len += TEXT_EXT_LEN
        
    if len(raw_frame) != expected_len:
        raise DecodeError(f"Frame length {len(raw_frame)} != expected {expected_len}.")

    # Guard 6: TTL constraints
    if msg_type == MsgType.HELLO and ttl != 1:
        raise DecodeError("HELLO message must have TTL of exactly 1.")
    if msg_type != MsgType.HELLO and ttl == 0:
        raise DecodeError("Non-HELLO message cannot have TTL of 0.")
        
    # Guard 7: TEXT specific constraints
    if msg_type == MsgType.TEXT and recipient == (b'\x00' * 16):
        raise DecodeError("TEXT message cannot have a broadcast recipient NodeID.")

    # --- Validation Passed! Now we slice the bytes ---
    
    ephemeral_pubkey = None
    nonce = None
    payload_start = BASE_HEADER_LEN

    # If it's a TEXT message, unpack the 44-byte extension
    if msg_type == MsgType.TEXT:
        ephemeral_pubkey, nonce = TEXT_EXT_FORMAT.unpack_from(raw_frame, payload_start)
        payload_start += TEXT_EXT_LEN

    # Slice the payload explicitly based on known offsets
    payload = raw_frame[payload_start : payload_start + payload_len]
    
    # Slice the signature (last 64 bytes)
    signature = raw_frame[-SIGNATURE_LEN:]

    return Envelope(
        version=version,
        msg_type=msg_type,
        priority=priority,
        ttl=ttl,
        msg_id=msg_id,
        seq_num=seq_num,
        sender_pubkey=sender_pubkey,
        recipient_node_id=recipient,
        payload_len=payload_len,
        ephemeral_pubkey=ephemeral_pubkey,
        nonce=nonce,
        payload=payload,
        signature=signature
    )