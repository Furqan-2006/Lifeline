import hashlib
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature

# In WP3, an "identity" is just a private key object that can sign things.
# The public key is derived from it.

def generate_identity() -> ed25519.Ed25519PrivateKey:
    """Generates a new Ed25519 key pair."""
    return ed25519.Ed25519PrivateKey.generate()

def node_id_of(public_key_bytes: bytes) -> bytes:
    """
    Contract: NodeID is the first 16 bytes of SHA-256 of the sender public key.
    """
    digest = hashlib.sha256(public_key_bytes).digest()
    return digest[0:16]

def sign(private_key: ed25519.Ed25519PrivateKey, data: bytes) -> bytes:
    """Returns a 64-byte Ed25519 signature."""
    return private_key.sign(data)

def verify(public_key_bytes: bytes, data: bytes, signature: bytes) -> bool:
    """Verifies the signature. Returns True if valid, False otherwise."""
    pub_key = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
    try:
        pub_key.verify(signature, data)
        return True   # Only reached if verify() doesn't throw an exception
    except InvalidSignature:
        return False  # Caught a bad signature