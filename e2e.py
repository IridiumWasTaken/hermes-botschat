"""Byte-exact port of BotsChat's E2E crypto (packages/e2e-crypto/e2e-crypto.ts).

- Key derivation: PBKDF2-HMAC-SHA256, 310,000 iterations, 32-byte key,
  salt = UTF-8 "botschat-e2e:" + userId   (deterministic, domain-prefixed)
- Cipher: AES-256-CTR, zero overhead (no tag, no padding)
- Nonce/IV: derived, not random — HKDF-SHA256 expand-only, single HMAC round:
    nonce = HMAC-SHA256(key, "nonce-" + contextId + 0x01)[0..16]
  contextId is a globally-unique string used ONCE per key (the message id,
  "msgId:media", a chunk id, an activity id, a job id, ...).

AES-CTR has NO authentication: a wrong key or contextId silently yields
garbage — callers must handle decryption errors gracefully.
"""

import base64
import hashlib
import hmac

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PBKDF2_ITERATIONS = 310_000
KEY_LENGTH = 32  # 256 bits
NONCE_LENGTH = 16  # AES-CTR counter block
SALT_PREFIX = "botschat-e2e:"


def derive_key(password: str, user_id: str) -> bytes:
    """Derive the 256-bit master key from the E2E password and userId."""
    salt = (SALT_PREFIX + user_id).encode("utf-8")
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, KEY_LENGTH
    )


def _hkdf_nonce(key: bytes, context_id: str) -> bytes:
    """HKDF-SHA256 expand-only, single step: T(1) = HMAC(PRK, info || 0x01)."""
    info = b"nonce-" + context_id.encode("utf-8") + b"\x01"
    return hmac.new(key, info, hashlib.sha256).digest()[:NONCE_LENGTH]


def _encrypt(key: bytes, plaintext: bytes, context_id: str) -> bytes:
    counter = _hkdf_nonce(key, context_id)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(counter)).encryptor()
    return encryptor.update(plaintext) + encryptor.finalize()


def _decrypt(key: bytes, ciphertext: bytes, context_id: str) -> bytes:
    counter = _hkdf_nonce(key, context_id)
    decryptor = Cipher(algorithms.AES(key), modes.CTR(counter)).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def encrypt_text(key: bytes, plaintext: str, context_id: str) -> bytes:
    """Encrypt a UTF-8 string. Returns raw ciphertext bytes (same length)."""
    return _encrypt(key, plaintext.encode("utf-8"), context_id)


def decrypt_text(key: bytes, ciphertext: bytes, context_id: str) -> str:
    """Decrypt ciphertext bytes back to a UTF-8 string.

    Lenient UTF-8 (errors="replace"), matching the TS implementation: both the
    Web TextDecoder (fatal: false) and Node's Buffer.toString("utf8") replace
    invalid bytes with U+FFFD instead of throwing — so a wrong key or contextId
    yields garbled text, never an exception.
    """
    return _decrypt(key, ciphertext, context_id).decode("utf-8", errors="replace")


def encrypt_bytes(key: bytes, data: bytes, context_id: str) -> bytes:
    return _encrypt(key, data, context_id)


def decrypt_bytes(key: bytes, ciphertext: bytes, context_id: str) -> bytes:
    return _decrypt(key, ciphertext, context_id)


def to_base64(data: bytes) -> str:
    """Standard base64 (matches the Node implementation's Buffer.toString('base64'))."""
    return base64.b64encode(data).decode("ascii")


def from_base64(b64: str) -> bytes:
    return base64.b64decode(b64)
