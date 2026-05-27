"""
TikTok X-Bogus Signature Generator — Pure Python Reimplementation

Reverse-engineered from TikTok's client-side JavaScript (byted_acrawler / frontierSign).

Algorithm:
  1. Double-MD5 hash the URL query parameters
  2. Double-MD5 hash the POST body (or empty string)
  3. RC4-encrypt the User-Agent with key [0x00, 0x01, 0x0E], Base64 encode, then MD5
  4. Build a 19-byte payload: [0x40, ua_key(3), params_md5(2), body_md5(2), ua_md5(2), timestamp(4), magic(4), xor_checksum(1)]
  5. RC4-encrypt the payload with key [0xFF]
  6. Prepend [0x02, 0xFF]
  7. Custom Base64 encode with shifted alphabet
"""

import hashlib
import base64
import time
import struct
from urllib.parse import urlencode, urlparse, parse_qs, urljoin

CUSTOM_B64_ALPHABET = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
STANDARD_B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="

UA_RC4_KEY = bytes([0x00, 0x01, 0x0E])
PAYLOAD_RC4_KEY = bytes([0xFF])
MAGIC_CONSTANT = 0x4A41279F
LEADING_BYTE = 0x40


def _rc4(key: bytes, data: bytes) -> bytes:
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) % 256
        S[i], S[j] = S[j], S[i]

    result = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + S[i]) % 256
        S[i], S[j] = S[j], S[i]
        result.append(byte ^ S[(S[i] + S[j]) % 256])
    return bytes(result)


def _md5_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.md5(data).hexdigest()


def _hex_to_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


def _double_md5(data: bytes | str) -> bytes:
    first_hex = _md5_hex(data)
    first_bytes = _hex_to_bytes(first_hex)
    second_hex = _md5_hex(first_bytes)
    return _hex_to_bytes(second_hex)


def _md5_user_agent(user_agent: str) -> bytes:
    ua_bytes = user_agent.encode("utf-8")
    rc4_encrypted = _rc4(UA_RC4_KEY, ua_bytes)
    b64_encoded = base64.b64encode(rc4_encrypted).decode("iso-8859-1")
    md5_hex = _md5_hex(b64_encoded)
    return _hex_to_bytes(md5_hex)


def _custom_base64_encode(data: bytes) -> str:
    standard = base64.b64encode(data).decode("ascii")
    trans = str.maketrans(STANDARD_B64_ALPHABET, CUSTOM_B64_ALPHABET)
    return standard.translate(trans)


def generate_xbogus(
    query_string: str,
    user_agent: str,
    body: str = "",
    timestamp: int | None = None,
) -> str:
    if timestamp is None:
        timestamp = int(time.time())

    params_md5 = _double_md5(query_string)
    body_md5 = _double_md5(body if body else "")
    ua_md5 = _md5_user_agent(user_agent)

    payload = bytearray()
    payload.append(LEADING_BYTE)
    payload.extend(UA_RC4_KEY)
    payload.extend(params_md5[14:16])
    payload.extend(body_md5[14:16])
    payload.extend(ua_md5[14:16])
    payload.extend(struct.pack(">I", timestamp))
    payload.extend(struct.pack(">I", MAGIC_CONSTANT))

    xor_check = 0
    for b in payload:
        xor_check ^= b
    payload.append(xor_check & 0xFF)

    encrypted = _rc4(PAYLOAD_RC4_KEY, bytes(payload))
    final = bytes([0x02, 0xFF]) + encrypted

    return _custom_base64_encode(final)


def sign_url(url: str, user_agent: str, body: str = "") -> str:
    parsed = urlparse(url)
    query_string = parsed.query
    xbogus = generate_xbogus(query_string, user_agent, body)
    separator = "&" if query_string else "?"
    return f"{url}{separator}X-Bogus={xbogus}"
