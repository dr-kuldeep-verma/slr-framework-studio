"""The checks behind 'free, on this computer'."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from slr_studio.ai.ollama import RECOMMENDED_MODELS, ollama_status, pull_model


class Reply:
    def __init__(self, status=200, body=None, lines=()):
        self.status_code = status
        self._body = body
        self._lines = list(lines)

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body

    def read(self):
        return b""

    def iter_lines(self):
        yield from self._lines


class Http:
    def __init__(self, tags=None, fail=False, pull_lines=(), pull_status=200):
        self.tags, self.fail = tags, fail
        self.pull_lines, self.pull_status = pull_lines, pull_status
        self.pulled = None

    def get(self, url, timeout=None):
        if self.fail:
            raise ConnectionError("refused")
        assert url.endswith("/api/tags")
        return Reply(body=self.tags)

    @contextmanager
    def stream(self, method, url, json=None, timeout=None):
        self.pulled = json
        yield Reply(self.pull_status, lines=self.pull_lines)


def test_not_running_says_how_to_install():
    status = ollama_status(http=Http(fail=True))
    assert not status.running
    assert "ollama.com/download" in status.describe()


def test_running_without_a_model_says_to_download_one():
    status = ollama_status(http=Http(tags={"models": []}))
    assert status.running and status.models == []
    assert "no model is downloaded" in status.describe()


def test_installed_models_are_listed():
    status = ollama_status(http=Http(tags={"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.1:8b"}]}))
    assert status.models == ["llama3.1:8b", "qwen2.5:7b"]
    assert "2 models ready" in status.describe()


def test_a_download_reports_progress():
    lines = [json.dumps(e) for e in (
        {"status": "pulling manifest"},
        {"status": "downloading", "total": 100, "completed": 40},
        {"status": "success"},
    )]
    http = Http(pull_lines=lines)
    seen = []
    pull_model("llama3.1:8b", http=http, on_progress=lambda d, t, s: seen.append((d, t, s)))
    assert http.pulled == {"model": "llama3.1:8b", "stream": True}
    assert (40, 100, "downloading") in seen


def test_a_failed_download_says_so():
    http = Http(pull_lines=[json.dumps({"error": "file does not exist"})])
    with pytest.raises(RuntimeError, match="file does not exist"):
        pull_model("nonsense", http=http)


def test_the_recommended_default_is_the_registry_default():
    from slr_studio.ai.providers import PROVIDERS

    assert RECOMMENDED_MODELS[0][0] == PROVIDERS["ollama"].default_model
