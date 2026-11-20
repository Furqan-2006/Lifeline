# line 120 ; what is the usage of derive funciton 
import hashlib
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature

import os
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives import serialization

from cryptography.exceptions import InvalidTag


# In WP3, an "identity" is just a private key object that can sign things.
# The public key is derived from it.

# ========================================================== #
# Curve25519 prime: 2^255 - 19
_CURVE25519_P = (1 << 255) - 19


def _ed25519_pub_to_x25519(ed_pub_bytes: bytes) -> x25519.X25519PublicKey:
    """Converts a 32-byte Ed25519 public key (Edwards y) to X25519 (Montgomery u)."""
    if len(ed_pub_bytes) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    y = int.from_bytes(ed_pub_bytes, "little")
    y &= (1 << 255) - 1  # Clear the sign bit (bit 255)
    denom = (1 - y) % _CURVE25519_P
    if denom == 0:
        raise ValueError("Invalid Ed25519 public key")
    # Modular inverse via Fermat's Little Theorem: a^-1 = a^(p-2) mod p
    u = ((1 + y) * pow(denom, _CURVE25519_P - 2, _CURVE25519_P)) % _CURVE25519_P
    return x25519.X25519PublicKey.from_public_bytes(u.to_bytes(32, "little"))


def _ed25519_priv_to_x25519(ed_priv: ed25519.Ed25519PrivateKey) -> x25519.X25519PrivateKey:
    """Converts an Ed25519 private key seed to its corresponding X25519 private scalar."""
    seed = ed_priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    h = bytearray(hashlib.sha512(seed).digest()[:32])
    # Standard Curve25519 bit-clamping
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return x25519.X25519PrivateKey.from_private_bytes(bytes(h))
# ========================================================== #

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
    try:
        pub_key = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
        pub_key.verify(signature, data)
        return True   # Only reached if verify() doesn't throw an exception
    except (InvalidSignature, ValueError):
        return False  # Caught a bad signature

def seal(recipient_ed25519_pub_bytes: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes, bytes]:
    """
    Encrypts a TEXT payload for the recipient. 
    Returns: (ephemeral_pubkey, nonce, ciphertext)
    """
    # 1. Generate an ephemeral X25519 keypair for Forward Secrecy
    ephemeral_priv = x25519.X25519PrivateKey.generate()
    ephemeral_pub_bytes = ephemeral_priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )
    
    # 2. Convert recipient's Ed25519 pubkey to X25519 pubkey for Key Exchange
    recipient_ed_pub = ed25519.Ed25519PublicKey.from_public_bytes(recipient_ed25519_pub_bytes)
    
    #  The cryptography library requires a private method to convert Ed25519 to X25519 public keys.
    # but for this project, we can generate a temporary X25519 key for the recipient directly 
    # to simulate the capability (or we can alternatively use a mathematical wrapper).
    # For now, we are directly using X25519 keys for the exchange.
    recipient_x25519_pub = _ed25519_pub_to_x25519(recipient_ed25519_pub_bytes)    
    # 3. Derive the Shared Secret
    shared_secret = ephemeral_priv.exchange(recipient_x25519_pub)
    
    # 4. HKDF Key Derivation (Contract 1.5)
    salt = ephemeral_pub_bytes + recipient_ed25519_pub_bytes
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"lifeline-text-v1"
    )
    symmetric_key = hkdf.derive(shared_secret)
    
    # 5. Encrypt with ChaCha20-Poly1305
    chacha = ChaCha20Poly1305(symmetric_key)
    nonce = os.urandom(12)  # 12 bytes of crypto-secure randomness
    
    ciphertext = chacha.encrypt(nonce, plaintext, aad)
    
    return ephemeral_pub_bytes, nonce, ciphertext

def open_msg(my_ed25519_priv: ed25519.Ed25519PrivateKey, ephemeral_pub_bytes: bytes, 
             nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    """
    Decrypts a TEXT payload.
    Returns the plaintext bytes, or None if decryption/authentication fails.
    """
    try:
        # 1. We must convert our Ed25519 signature key into an X25519 key for the math exchange.
        my_x25519_priv = _ed25519_priv_to_x25519(my_ed25519_priv)
        
        # 2. Load the sender's throwaway (ephemeral) public key from the packet bytes
        ephemeral_pub = x25519.X25519PublicKey.from_public_bytes(ephemeral_pub_bytes)
        
        # 3. Do the Diffie-Hellman math! 
        # (My Private) + (Their Ephemeral Public) = Shared Secret
        shared_secret = my_x25519_priv.exchange(ephemeral_pub)
        
        # 4. Put it in the HKDF blender with the exact same salt and info as the sender
        my_ed25519_pub_bytes = my_ed25519_priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        salt = ephemeral_pub_bytes + my_ed25519_pub_bytes
        
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=b"lifeline-text-v1"
        )
        symmetric_key = hkdf.derive(shared_secret)
        
        # 5. Open the ChaCha20 padlock using the derived key, nonce, and AAD (header)
        chacha = ChaCha20Poly1305(symmetric_key)
        
        # If the AAD was tampered with by a relay node, or the ciphertext is corrupted,
        # this will throw an InvalidTag exception.
        plaintext = chacha.decrypt(nonce, ciphertext, aad)
        
        return plaintext
        
    except Exception as e:
        # If absolutely anything goes wrong (bad key, tampered data, invalid math),
        # we return None. In systems programming, a failed decryption must never crash the node!
        return None