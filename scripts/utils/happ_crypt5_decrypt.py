#!/usr/bin/env python3
"""Decrypt happ://crypt5/ deep links (verified algorithm from
loss-and-quick/happ-sub docs/05-url-encryption.md, keys from KernelEditor/HappDecrypt).
Usage: happ_crypt5_decrypt.py <payload-file-or-link> [url-keys.json]
Prints the decrypted subscription URL.
"""
import base64
import json
import sys

from cryptography.hazmat.primitives import hashes  # noqa: F401
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_der_private_key
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305


def half_swap(data: bytes) -> bytes:
    """ABCD -> CDAB per 4-byte block, trailing bytes untouched."""
    out = bytearray()
    for i in range(0, len(data) - len(data) % 4, 4):
        out += data[i + 2:i + 4] + data[i:i + 2]
    rem = len(data) % 4
    if rem:
        out += data[len(data) - rem:]
    return bytes(out)


def swap_adjacent(data: bytes) -> bytes:
    """ABCD... -> BADC..., trailing odd byte stays."""
    b = bytearray(data)
    for i in range(0, len(b) - 1, 2):
        b[i], b[i + 1] = b[i + 1], b[i]
    return bytes(b)


def load_keyring(path: str) -> dict:
    raw = json.load(open(path))
    ring = {}
    for name, b64 in raw.items():
        der = base64.b64decode(b64)
        ring[name] = load_der_private_key(der, password=None)
    return ring


def parse_body(body: bytes):
    """Return (nonce, salt, chacha_key_material) per salted/unsalted layout."""
    # salted: nonce[12] filler[2] salt[8] digits...
    # unsalted: nonce[12] digits...  (detect: body[12] is ASCII digit)
    if len(body) > 12 and body[12:13].isdigit():
        # unsalted
        nonce = body[0:12]
        salt = None
        digits_start = 12
    else:
        nonce = body[0:12]
        salt = body[14:22]
        digits_start = 22
    i = digits_start
    while i < len(body) and body[i:i + 1].isdigit():
        i += 1
    seg_len = int(body[digits_start:i])
    # 1 filler byte after digits
    enc_url = body[i + 1:i + 1 + seg_len]
    enc_skey = body[i + 1 + seg_len:]
    return nonce, salt, enc_url, enc_skey


def decrypt(payload: str, keyring: dict) -> str:
    s = half_swap(payload.encode("ascii"))
    marker = s[0:4] + s[len(s) - 4:]
    body = s[4:len(s) - 4]
    if marker.decode("ascii") not in keyring:
        raise SystemExit(f"unknown marker {marker!r} — key not in keyring")
    nonce, salt, enc_url, enc_skey = parse_body(body)

    rsa_plain = keyring[marker.decode("ascii")].decrypt(
        base64.b64decode(enc_skey), padding.PKCS1v15())
    session_key = base64.b64decode(swap_adjacent(rsa_plain))  # 32 bytes
    if salt is not None:
        chacha_key = bytes(
            session_key[i] ^ salt[i % 8] for i in range(32))
    else:
        chacha_key = session_key
    inner = ChaCha20Poly1305(chacha_key).decrypt(
        nonce, base64.b64decode(enc_url), None)
    url = base64.b64decode(swap_adjacent(inner)).decode("utf-8")
    return url


def main():
    arg = sys.argv[1]
    keys_path = sys.argv[2] if len(sys.argv) > 2 else "happ_url_keys.json"
    link = open(arg).read().strip() if not arg.startswith("happ://") else arg
    payload = link.replace("happ://crypt5/", "")
    keyring = load_keyring(keys_path)
    print(decrypt(payload, keyring))


if __name__ == "__main__":
    main()
