"""Crypto and identity (contract C1.5 and C2.4).

An ``Identity`` holds the Ed25519 private key and exposes ``public_key``
(32 bytes) and ``node_id`` (16 bytes), as C2.4 requires.

TEXT encryption is one-shot ECIES-style: ephemeral X25519, HKDF-SHA256,
ChaCha20-Poly1305. Ed25519 keys are not X25519 keys, so both sides convert:
  * public key:  u = (1 + y) / (1 - y) mod 2**255-19, y from the Ed25519 point
  * private key: first 32 bytes of SHA-512(seed), the same scalar Ed25519 uses
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NODE_ID_LEN = 16
_HKDF_INFO = b"lifeline-text-v1"
_P = 2**255 - 19


@dataclass(frozen=True)
class Identity:
    private_key: ed25519.Ed25519PrivateKey = field(repr=False)
    public_key: bytes   # 32 bytes
    node_id: bytes      # 16 bytes

    def sign(self, data: bytes) -> bytes:
        return self.private_key.sign(data)


#: Anything that can sign or open: an Identity, or a raw Ed25519 private key.
IdentityLike = Identity | ed25519.Ed25519PrivateKey


def _private_key_of(identity: IdentityLike) -> ed25519.Ed25519PrivateKey:
    if isinstance(identity, Identity):
        return identity.private_key
    if isinstance(identity, ed25519.Ed25519PrivateKey):
        return identity
    raise TypeError(f"expected an Identity or Ed25519PrivateKey, got {type(identity).__name__}")


def _public_bytes_of(identity: IdentityLike) -> bytes:
    if isinstance(identity, Identity):
        return identity.public_key
    return _private_key_of(identity).public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


def node_id_of(public_key: bytes) -> bytes:
    """C1.1: the first 16 bytes of SHA-256 of the public key."""
    return hashlib.sha256(public_key).digest()[:NODE_ID_LEN]


def identity_from_private_key(private_key: ed25519.Ed25519PrivateKey) -> Identity:
    public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return Identity(private_key=private_key, public_key=public, node_id=node_id_of(public))


def generate_identity() -> Identity:
    """New random Ed25519 identity."""
    return identity_from_private_key(ed25519.Ed25519PrivateKey.generate())


def identity_from_seed(seed: bytes) -> Identity:
    """Deterministic identity from a 32-byte seed (reproducible runs)."""
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    return identity_from_private_key(ed25519.Ed25519PrivateKey.from_private_bytes(seed))


def sign(identity: IdentityLike, data: bytes) -> bytes:
    """64-byte Ed25519 signature. Also accepts a raw Ed25519PrivateKey."""
    return _private_key_of(identity).sign(data)


def verify(public_key: bytes, data: bytes, signature: bytes) -> bool:
    """True if the signature is valid. Never raises on bad input."""
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(bytes(public_key)).verify(
            bytes(signature), bytes(data)
        )
        return True
    except (InvalidSignature, Exception):
        return False


# --- Ed25519 to X25519 conversion ------------------------------------------------

def _ed_public_to_x25519(ed_public: bytes) -> x25519.X25519PublicKey:
    if len(ed_public) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    y = int.from_bytes(ed_public, "little") & ((1 << 255) - 1)
    if y >= _P:
        raise ValueError("not a valid Ed25519 public key")
    denominator = (1 - y) % _P
    if denominator == 0:
        raise ValueError("not a valid Ed25519 public key")
    u = ((1 + y) * pow(denominator, -1, _P)) % _P
    return x25519.X25519PublicKey.from_public_bytes(u.to_bytes(32, "little"))


def _ed_private_to_x25519(identity: IdentityLike) -> x25519.X25519PrivateKey:
    seed = _private_key_of(identity).private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return x25519.X25519PrivateKey.from_private_bytes(hashlib.sha512(seed).digest()[:32])


def _derive_key(shared_secret: bytes, ephemeral_public: bytes, recipient_public: bytes) -> bytes:
    """C1.5: HKDF-SHA256, salt = ephemeral public key then recipient public key."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=ephemeral_public + recipient_public,
        info=_HKDF_INFO,
    ).derive(shared_secret)


def seal(recipient_public_key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes, bytes]:
    """Encrypt for the recipient. Returns (ephemeral_public_key, nonce, ciphertext).

    Raises ValueError if ``recipient_public_key`` is not a valid Ed25519 public key.
    """
    recipient_x = _ed_public_to_x25519(recipient_public_key)
    ephemeral_private = x25519.X25519PrivateKey.generate()
    ephemeral_public = ephemeral_private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    shared = ephemeral_private.exchange(recipient_x)
    key = _derive_key(shared, ephemeral_public, recipient_public_key)
    nonce = os.urandom(12)
    return ephemeral_public, nonce, ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)


def open_msg(identity: IdentityLike, ephemeral_public_key: bytes, nonce: bytes,
             ciphertext: bytes, aad: bytes) -> bytes | None:
    """Decrypt. Returns the plaintext, or None on any failure; never raises."""
    try:
        my_private = _ed_private_to_x25519(identity)
        shared = my_private.exchange(x25519.X25519PublicKey.from_public_bytes(ephemeral_public_key))
        key = _derive_key(shared, ephemeral_public_key, _public_bytes_of(identity))
        return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, aad)
    except Exception:
        return None