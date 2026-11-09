"""Finding out whether Ollama is ready, and getting a model for it.

Ollama is the free choice: it runs the model on the reviewer's own computer,
so nothing costs money and no record leaves the machine. What stands between
a new reviewer and that is two steps they may not know about -- installing
Ollama and downloading a model -- so the settings window checks both and
says which one is missing.

Downloading a model goes to Ollama's own library and sends nothing about
the review.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "DEFAULT_URL",
    "INSTALL_PAGE",
    "RECOMMENDED_MODELS",
    "OllamaStatus",
    "ollama_status",
    "pull_model",
]

DEFAULT_URL = "http://localhost:11434"
INSTALL_PAGE = "https://ollama.com/download"

#: Offered when nothing is installed yet: (name, size to download, note).
#: Small enough for an ordinary laptop. Larger models screen better, and a
#: reviewer can type any other name.
RECOMMENDED_MODELS: list[tuple[str, str, str]] = [
    ("llama3.1:8b", "4.9 GB", "a good all-round start; needs about 8 GB of memory"),
    ("qwen2.5:7b", "4.7 GB", "strong at following JSON instructions"),
    ("gemma2:2b", "1.6 GB", "for a laptop with little memory; less accurate"),
]


@dataclass
class OllamaStatus:
    running: bool
    models: list[str] = field(default_factory=list)
    error: str = ""

    def describe(self) -> str:
        if not self.running:
            return (
                "Ollama is not running on this computer. Install it (free) from "
                f"{INSTALL_PAGE}, start it, and come back here."
            )
        if not self.models:
            return "Ollama is running, but no model is downloaded yet. Download one below."
        n = len(self.models)
        return f"Ollama is running, with {n} model{'s' if n != 1 else ''} ready."


def _httpx():
    import httpx

    return httpx


def ollama_status(base_url: str = DEFAULT_URL, *, http: Any = None, timeout: float = 2.0) -> OllamaStatus:
    """Whether Ollama answers, and which models it has. Never raises."""
    http = http if http is not None else _httpx()
    try:
        response = http.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
    except Exception as exc:
        return OllamaStatus(running=False, error=str(exc))
    if response.status_code != 200:
        return OllamaStatus(running=False, error=f"HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        return OllamaStatus(running=False, error="not Ollama")
    names = [m.get("name") or m.get("model") for m in data.get("models", [])]
    return OllamaStatus(running=True, models=sorted(n for n in names if n))


def pull_model(
    name: str,
    base_url: str = DEFAULT_URL,
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
    http: Any = None,
) -> None:
    """Download a model through Ollama, reporting progress as it goes."""
    http = http if http is not None else _httpx()
    with http.stream(
        "POST",
        f"{base_url.rstrip('/')}/api/pull",
        json={"model": name, "stream": True},
        timeout=None,
    ) as response:
        if response.status_code != 200:
            response.read()
            raise RuntimeError(f"Ollama could not download {name} (HTTP {response.status_code}).")
        for line in response.iter_lines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("error"):
                raise RuntimeError(f"Ollama could not download {name}: {event['error']}")
            if on_progress:
                total = int(event.get("total") or 0)
                done = int(event.get("completed") or 0)
                on_progress(done, total, str(event.get("status", "")))
