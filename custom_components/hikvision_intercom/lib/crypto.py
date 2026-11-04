#!/usr/bin/env python3
"""
CPD7/HP7 pure-Python ECDH + ChaCha20 key derivation.

Implements the KDF discovered from reversing libezstreamclient.so:
  ECDH P-256  → shared_secret (32 B)
  AES-256-ECB(key=shared_secret) decrypt inner_payload → ChaCha20 key (32 B)
  Nonce per-packet: 4 B from wire offset 0x07, swapped, padded to 12 B.

See docs/cpd7-stream-recipe/07-CRYPTO-INTERNALS.md for the full algorithm.
"""

import base64
import struct

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization


# ---------------------------------------------------------------------------
# ECDH P-256 keypair generation
# ---------------------------------------------------------------------------

def generate_ecdh_keypair():
    """Generate ephemeral ECDH P-256 keypair.

    Returns (priv, pub_b64) where:
      priv   — cryptography ec.EllipticCurvePrivateKey object (keep in memory)
      pub_b64 — base64 DER SubjectPublicKeyInfo, ready for <PublicKey> XML.
    """
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_der = priv.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_b64 = base64.b64encode(pub_der).decode()
    return priv, pub_b64


# ---------------------------------------------------------------------------
# ECDH packet parsing  ($ \x01 and $ \x02)
# ---------------------------------------------------------------------------

ECDH_MAGIC = 0x24  # '$'
ECDH_TYPE_REQ = 0x01   # handshake (carries camera pubkey + encrypted session key)
ECDH_TYPE_DATA = 0x02  # data (ChaCha20-encrypted payload)

# Offsets within the packet (relative to first byte after '$')
# These match the layout emitted by encECDHReqPackage / encECDHDataPackage.
OFF_TYPE = 1
OFF_HEADER_LEN = 2
OFF_PAYLOAD_LEN = 3    # 2 bytes BE-on-wire (after byteswap via enc function)
OFF_MARKER = 5
OFF_SUBTYPE = 6
OFF_NONCE_RAW = 7      # 4 bytes
OFF_ENCRYPTED_KEY = 0x0B   # 32 bytes (two AES-256-ECB blocks)
OFF_PEER_PUBKEY = 0x2B     # 91 bytes (DER SubjectPublicKeyInfo P-256)
HEADER_FIXED = 0x86        # end of fixed header area

# Trailer layout (after optional payload at offset HEADER_FIXED):
#   CRC32(0x00..HEADER_FIXED)  : 4 bytes
#   CRC32(payload)             : 4 bytes
#   HMAC-SHA256                : 32 bytes
TRAILER_SIZE = 4 + 4 + 32  # 40 bytes


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Nonce transformation
# ---------------------------------------------------------------------------

def transform_nonce(nonce_raw_4b: bytes) -> bytes:
    """Apply the two swap operations to convert wire nonce → ChaCha20 nonce.

    From decompiled code (decECDHReqPackage lines 870338-870340):
      uVar5 = *(uint*)(packet + 7)  // LE read
      uVar5 = (uVar5 & 0xff00ff00) >> 8 | (uVar5 & 0xff00ff) << 8  // swap16 in u32
      local_2dc = uVar5 >> 16 | uVar5 << 16  // ROL16

    The net result on ARM LE: wire bytes AA BB CC DD → nonce AA BB CC DD.
    (The two swaps cancel out for normal wire order.)

    Returns 4-byte bytes object suitable for ChaCha20 nonce prefix.
    """
    u32 = struct.unpack('<I', nonce_raw_4b)[0]
    u32 = ((u32 & 0xFF00FF00) >> 8) | ((u32 & 0x00FF00FF) << 8)
    u32 = (u32 >> 16) | ((u32 & 0xFFFF) << 16) & 0xFFFFFFFF
    return struct.pack('<I', u32)


# ---------------------------------------------------------------------------
# Stream decryption
# ---------------------------------------------------------------------------
