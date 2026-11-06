"""Deterministic hashing helpers.

Integrity rule 2 says every AI decision stores a hash of its prompt and input.
Rule 1 says every claim traces to a stored record. Both need hashes that are
stable across runs and machines, so everything here canonicalises before
hashing: JSON is dumped with sorted keys and no insignificant whitespace, and
text is normalised to NFC before encoding as UTF-8.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any

__all__ = [
    "sha256_text",
    "sha256_bytes",
    "sha256_file",
    "canonical_json",
    "sha256_json",
    "short_hash",
]


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def sha256_text(text: str) -> str:
    """Hash a string after Unicode NFC normalisation."""
    return hashlib.sha256(_nfc(text).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    """Hash a file's contents without loading it all into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    """Serialise to the one JSON form we hash and store.

    Sorted keys, compact separators, non-ASCII preserved. Two structurally
    equal objects always produce the same string, which is what makes the
    response cache and the audit chain reproducible.
    """
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def short_hash(digest: str, length: int = 12) -> str:
    """First *length* characters of a digest, for display only."""
    return digest[:length]
