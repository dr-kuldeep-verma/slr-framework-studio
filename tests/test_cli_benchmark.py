"""`slr benchmark` against a real provider.

The mock path was the only one ever exercised, and the real one called a
method that did not exist. These tests run the real path with the network
replaced, so the wiring -- choosing the provider, checking the key, showing
the estimate, honouring a refusal -- is what is tested.
"""

from __future__ import annotations

import csv
import json

import pytest

from slr_studio import cli
from slr_studio.ai import MockProvider

PROTOCOL = "Include studies of resident participation in tourism planning."


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "d.csv"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["title", "abstract", "label_abstract_included", "label_included"])
        w.writerow(["Resident participation in tourism planning", "Residents.", 1, 1])
        w.writerow(["Soil chemistry of upland pasture", "Nitrogen.", 0, 0])
    protocol = tmp_path / "p.txt"
    protocol.write_text(PROTOCOL, encoding="utf-8")
    return path, protocol


class _Recorder(MockProvider):
    """The mock model, under a real-looking name, standing in for a provider."""

    def __init__(self, model):
        super().__init__()
        self.model = model


@pytest.fixture
def fake_network(monkeypatch):
    built = {}

    def fake_build(key, model=None, **options):
        built["key"], built["model"], built["options"] = key, model, options
        return _Recorder(model or "claude-sonnet-5-5")

    monkeypatch.setattr("slr_studio.ai.providers.build_provider", fake_build)
    monkeypatch.setattr("slr_studio.ai.settings.build_provider", fake_build)
    monkeypatch.setattr("slr_studio.ai.keys.get_key", lambda provider: "sk-test")
    return built


def _run(argv, monkeypatch, answer="y"):
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    return cli.main(["benchmark", *argv])


class TestTheRealPath:
    def test_it_runs_with_the_chosen_model(self, dataset, tmp_path, fake_network,
                                           monkeypatch, capsys):
        data, protocol = dataset
        out = tmp_path / "r.md"
        code = _run([str(data), "--protocol", str(protocol), "--provider",
                     "anthropic", "--model", "claude-haiku-4-5", "--out", str(out)],
                    monkeypatch)
        assert code == 0, capsys.readouterr().err
        assert fake_network["key"] == "anthropic"
        assert fake_network["model"] == "claude-haiku-4-5"
        result = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
        assert result["model"] == "claude-haiku-4-5"
        assert result["dataset"]["label_column"] == "label_abstract_included"

    def test_the_estimate_is_shown_in_tokens_and_money(self, dataset, tmp_path,
                                                       fake_network, monkeypatch,
                                                       capsys):
        data, protocol = dataset
        _run([str(data), "--protocol", str(protocol), "--model",
              "claude-sonnet-5-5", "--out", str(tmp_path / "r.md")], monkeypatch)
        printed = capsys.readouterr().out
        assert "tokens" in printed
        assert "US$" in printed

    def test_saying_no_spends_nothing_and_writes_nothing(self, dataset, tmp_path,
                                                        fake_network, monkeypatch,
                                                        capsys):
        data, protocol = dataset
        out = tmp_path / "r.md"
        code = _run([str(data), "--protocol", str(protocol), "--out", str(out)],
                    monkeypatch, answer="n")
        assert code == 1
        assert not out.exists()
        assert "Nothing was run" in capsys.readouterr().out

    def test_a_missing_key_is_reported_before_anything_starts(
        self, dataset, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr("slr_studio.ai.keys.get_key", lambda provider: None)
        data, protocol = dataset
        code = _run([str(data), "--protocol", str(protocol)], monkeypatch)
        assert code != 0
        err = capsys.readouterr().err
        assert "API key" in err and "Model settings" in err

    def test_a_budget_is_passed_to_the_run(self, dataset, tmp_path, fake_network,
                                           monkeypatch):
        seen = {}
        from slr_studio.evaluation import benchmark as bench

        real = bench.run_benchmark

        def spy(*args, **kwargs):
            seen["settings"] = kwargs.get("settings")
            return real(*args, **kwargs)

        monkeypatch.setattr(bench, "run_benchmark", spy)
        data, protocol = dataset
        _run([str(data), "--protocol", str(protocol), "--budget", "5",
              "--out", str(tmp_path / "r.md")], monkeypatch)
        assert seen["settings"].budget_usd == 5.0


class TestFreeTierOptions:
    def test_requests_per_minute_throttles_the_provider(self, dataset, tmp_path,
                                                       fake_network, monkeypatch):
        from slr_studio.ai.throttle import ThrottledProvider

        seen = []
        real_init = ThrottledProvider.__init__

        def spy(self, inner, per_minute, **kw):
            seen.append(per_minute)
            real_init(self, inner, per_minute, sleep=lambda s: None, **kw)

        monkeypatch.setattr(ThrottledProvider, "__init__", spy)
        data, protocol = dataset
        code = _run([str(data), "--protocol", str(protocol),
                     "--requests-per-minute", "8", "--out", str(tmp_path / "r.md")],
                    monkeypatch)
        assert code == 0
        assert seen and seen[0] == 8

    def test_a_paused_run_writes_no_report_and_says_how_to_continue(
        self, dataset, tmp_path, monkeypatch, capsys
    ):
        from slr_studio.ai.base import RateLimitError

        class Capped(MockProvider):
            def __init__(self, model):
                super().__init__()
                self.model = model

            def complete(self, **kwargs):
                if self.n_calls >= 1:
                    raise RateLimitError("Quota exceeded")
                return super().complete(**kwargs)

        monkeypatch.setattr("slr_studio.ai.settings.build_provider",
                            lambda key, model=None, **o: Capped(model or "m"))
        monkeypatch.setattr("slr_studio.ai.keys.get_key", lambda p: "k")
        monkeypatch.setattr("slr_studio.ai.client.time.sleep", lambda s: None,
                            raising=False)
        data, protocol = dataset
        out = tmp_path / "r.md"
        work = tmp_path / "work"
        code = _run([str(data), "--protocol", str(protocol), "--out", str(out),
                     "--workspace", str(work)], monkeypatch)
        printed = capsys.readouterr().out
        assert code == 3
        assert not out.exists()
        assert "run the same command again" in printed.lower()
        assert (work / "project.slr").exists() or any(work.iterdir())


def test_the_report_folder_is_created_if_missing(dataset, tmp_path, fake_network,
                                                 monkeypatch):
    data, protocol = dataset
    out = tmp_path / "not" / "yet" / "r.md"
    assert _run([str(data), "--protocol", str(protocol), "--out", str(out)],
                monkeypatch) == 0
    assert out.exists() and out.with_suffix(".json").exists()
