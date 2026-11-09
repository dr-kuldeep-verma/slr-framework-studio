"""API keys, kept in the operating system's credential store.

On Windows that is the Credential Manager. A key is never written to the
project database, to a configuration file, or to the audit log -- the log
records which provider and model answered, never what authorised it.

An environment variable is read as a fallback so a reviewer on a locked-down
machine, or a continuous-integration run, still works.
"""

from __future__ import annotations

import os

__all__ = [
    "get_key",
    "set_key",
    "delete_key",
    "has_key",
    "get_secret",
    "set_secret",
    "delete_secret",
    "ENV_VARS",
    "KeyringUnavailable",
]

SERVICE = "SLR Framework Studio"

#: Checked before the credential store, so an existing setup keeps working.
ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai_compatible": "OPENAI_API_KEY",
    "ollama": "",  # a local model needs no key
}


class KeyringUnavailable(RuntimeError):
    """No usable credential store on this machine."""


def _keyring():
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - keyring ships with the app
        raise KeyringUnavailable(
            "The keyring package is not installed, so API keys cannot be "
            "stored securely. Set the API key as an environment variable "
            "instead."
        ) from exc
    return keyring


def get_key(provider: str) -> str | None:
    """The stored key for a provider, or None.

    Order: environment variable, then credential store. A failure to reach the
    credential store is treated as 'no key' rather than raised, so a missing
    backend degrades to a clear 'add your API key' message instead of a crash
    on startup.
    """
    env_name = ENV_VARS.get(provider, "")
    if env_name:
        value = os.environ.get(env_name)
        if value:
            return value.strip()
    try:
        return _keyring().get_password(SERVICE, provider)
    except Exception:
        return None


def set_key(provider: str, key: str) -> None:
    if not key or not key.strip():
        raise ValueError("An API key cannot be blank.")
    try:
        _keyring().set_password(SERVICE, provider, key.strip())
    except KeyringUnavailable:
        raise
    except Exception as exc:
        raise KeyringUnavailable(
            f"Could not write to the credential store: {exc}"
        ) from exc


def delete_key(provider: str) -> None:
    try:
        _keyring().delete_password(SERVICE, provider)
    except Exception:
        # Deleting a key that is not there is not an error worth reporting.
        pass


def has_key(provider: str) -> bool:
    return bool(get_key(provider))


# -- longer secrets ------------------------------------------------------------

#: Windows Credential Manager holds at most 2,560 bytes per entry, stored as
#: UTF-16, so about 1,280 characters. A sign-in token is longer than that, so
#: long secrets are written in pieces with a count beside them.
CHUNK = 1000


def set_secret(name: str, value: str) -> None:
    """Store a secret of any length under ``name``."""
    kr = _keyring()
    delete_secret(name)
    pieces = [value[i : i + CHUNK] for i in range(0, len(value), CHUNK)] or [""]
    try:
        for index, piece in enumerate(pieces):
            kr.set_password(SERVICE, f"{name}#{index}", piece)
        kr.set_password(SERVICE, f"{name}#n", str(len(pieces)))
    except Exception as exc:
        raise KeyringUnavailable(
            f"Could not write to the credential store: {exc}"
        ) from exc


def get_secret(name: str) -> str | None:
    """A secret written by :func:`set_secret`, or None."""
    try:
        kr = _keyring()
        count = kr.get_password(SERVICE, f"{name}#n")
        if not count:
            return None
        pieces = []
        for index in range(int(count)):
            piece = kr.get_password(SERVICE, f"{name}#{index}")
            if piece is None:
                return None  # half-written: treat as absent
            pieces.append(piece)
        return "".join(pieces)
    except Exception:
        return None


def delete_secret(name: str) -> None:
    try:
        kr = _keyring()
        count = kr.get_password(SERVICE, f"{name}#n")
    except Exception:
        return
    for key in [f"{name}#n"] + [f"{name}#{i}" for i in range(int(count or 0))]:
        try:
            kr.delete_password(SERVICE, key)
        except Exception:
            pass
