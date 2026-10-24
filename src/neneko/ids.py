"""Deterministic identifiers for Kobo books and collections.

A book's Kobo ID is a UUIDv8 whose first 8 bytes identify the Nextcloud account and
whose last 8 bytes hold the News item ID. Any ID the Kobo sends back can therefore be
mapped to a News item without consulting the database, and re-delivering an article
after the database is lost reuses the ID the Kobo already has.
"""

import hashlib
import uuid

_ITEM_ID_MASK = (1 << 62) - 1  # the top two bits of byte 8 hold the UUID variant


def instance_prefix(nc_url: str, nc_user: str) -> bytes:
    return hashlib.sha256(f"{nc_url.rstrip('/')}|{nc_user}".encode()).digest()[:8]


def item_uuid(prefix: bytes, item_id: int) -> str:
    if not 0 <= item_id <= _ITEM_ID_MASK:
        raise ValueError(f"item id out of range: {item_id}")
    raw = bytearray(prefix + item_id.to_bytes(8, "big"))
    raw[6] = (raw[6] & 0x0F) | 0x80  # version 8
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 9562 variant
    return str(uuid.UUID(bytes=bytes(raw)))


def item_id_from_uuid(prefix: bytes, value: str) -> int | None:
    """Return the News item ID encoded in a Kobo book ID, or None if it isn't ours."""
    try:
        raw = uuid.UUID(value).bytes
    except (ValueError, AttributeError, TypeError):
        return None
    if raw[:6] != prefix[:6] or raw[7] != prefix[7] or raw[6] != (prefix[6] & 0x0F) | 0x80:
        return None
    if raw[8] & 0xC0 != 0x80:
        return None
    return int.from_bytes(raw[8:], "big") & _ITEM_ID_MASK


def tag_uuid(prefix: bytes, kind: str, ref: int | str) -> str:
    return str(uuid.uuid5(uuid.UUID(bytes=prefix * 2), f"neneko-tag:{kind}:{ref}"))
