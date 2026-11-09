"""The provider layer: validation, caching, retries, logging and cost."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from slr_studio.ai import AIClient, MockProvider, ScriptedProvider
from slr_studio.ai.base import AuthenticationError, ProviderError, estimate_tokens
from slr_studio.ai.client import BudgetExceeded
from slr_studio.ai.pricing import DEFAULT_RATES, Rate, estimate_cost, rate_for
from slr_studio.ai.providers import PROVIDERS, build_provider, parse_json_object
from slr_studio.ai.settings import ModelSettings, load_settings, save_settings
from slr_studio.screening.schemas import AdjudicationAnswer, ScreeningAnswer


class Answer(BaseModel):
    decision: str
    reason: str = ""


def client(project, provider=None, **kwargs):
    kwargs.setdefault("sleep", lambda _: None)
    return AIClient(project, provider or MockProvider(), stage="screen", **kwargs)


class TestJsonParsing:
    def test_plain_json(self):
        assert parse_json_object('{"a": 1}') == {"a": 1}

    def test_fenced_json_block(self):
        assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_commentary_around_it(self):
        assert parse_json_object('Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}

    def test_no_json_at_all_raises(self):
        with pytest.raises(ValueError, match="No JSON object"):
            parse_json_object("I would rather not.")


class TestValidation:
    def test_a_valid_answer_comes_back_as_a_model(self, project):
        result = client(project, ScriptedProvider(['{"decision":"include","reason":"ok"}'])).ask(
            "t", system="s", user="u", schema=Answer
        )
        assert result.value.decision == "include"

    def test_an_exclusion_without_a_code_is_rejected(self):
        with pytest.raises(ValueError, match="exclusion code"):
            ScreeningAnswer(decision="exclude", reason="off topic")

    def test_an_exclusion_with_a_code_is_accepted(self):
        answer = ScreeningAnswer(decision="exclude", exclusion_code="E4", reason="wrong people")
        assert answer.exclusion_code == "E4"

    def test_a_code_on_an_inclusion_is_dropped(self):
        answer = ScreeningAnswer(decision="include", exclusion_code="E4", reason="on topic")
        assert answer.exclusion_code == ""

    def test_decisions_are_case_insensitive(self):
        assert ScreeningAnswer(decision="INCLUDE", reason="x").decision == "include"
        assert ScreeningAnswer(decision="Excluded", exclusion_code="E1",
                               reason="x").decision == "exclude"

    def test_an_invented_category_is_rejected(self):
        with pytest.raises(ValueError):
            ScreeningAnswer(decision="maybe", reason="x")

    def test_a_missing_reason_is_rejected(self):
        with pytest.raises(ValueError, match="reason is required"):
            ScreeningAnswer(decision="include", reason="")

    def test_an_overlong_reason_is_trimmed_not_rejected(self):
        answer = ScreeningAnswer(decision="include", reason="x" * 900)
        assert len(answer.reason) <= 400

    def test_the_adjudicator_records_which_side_it_took(self):
        ruling = AdjudicationAnswer(decision="include", reason="r", sided_with="A")
        assert ruling.sided_with == "screener_a"


class TestRetries:
    def test_a_malformed_answer_is_retried_with_the_problem_named(self, project):
        provider = ScriptedProvider(["nonsense", '{"decision":"include","reason":"ok"}'])
        result = client(project, provider).ask("t", system="s", user="u", schema=Answer)
        assert result.attempts == 2
        assert "could not be used" in provider.calls[1]["user"]
        assert "s" == provider.calls[1]["system"]

    def test_the_original_prompt_survives_the_repair_attempt(self, project):
        provider = ScriptedProvider(["nonsense", '{"decision":"include","reason":"ok"}'])
        client(project, provider).ask("t", system="s", user="THE RECORD", schema=Answer)
        assert "THE RECORD" in provider.calls[1]["user"]

    def test_giving_up_after_the_retry_limit(self, project):
        provider = ScriptedProvider(default="still not json")
        with pytest.raises(Exception):
            client(project, provider, max_retries=3).ask(
                "t", system="s", user="u", schema=Answer
            )
        assert len(provider.calls) == 3

    def test_an_authentication_failure_is_not_retried(self, project):
        class Failing(ScriptedProvider):
            def complete(self, **kwargs):
                self.calls.append(kwargs)
                raise AuthenticationError("no key")

        provider = Failing()
        with pytest.raises(AuthenticationError):
            client(project, provider).ask("t", system="s", user="u", schema=Answer)
        assert len(provider.calls) == 1


class TestCaching:
    def test_an_identical_prompt_is_answered_from_the_cache(self, project):
        c = client(project)
        first = c.ask("t", system="s", user="u", schema=Answer)
        second = c.ask("t", system="s", user="u", schema=Answer)
        assert first.cached is False and second.cached is True

    def test_a_cached_answer_is_still_logged(self, project):
        c = client(project)
        c.ask("t", system="s", user="u", schema=Answer)
        c.ask("t", system="s", user="u", schema=Answer)
        assert project.db.scalar("SELECT COUNT(*) FROM ai_call") == 2
        assert project.db.scalar("SELECT COUNT(*) FROM ai_call WHERE cached = 1") == 1

    def test_a_different_system_prompt_is_a_different_cache_entry(self, project):
        c = client(project)
        c.ask("t", system="screener a", user="u", schema=Answer)
        second = c.ask("t", system="screener b", user="u", schema=Answer)
        assert second.cached is False

    def test_caching_can_be_turned_off(self, project):
        c = client(project, use_cache=False)
        c.ask("t", system="s", user="u", schema=Answer)
        assert c.ask("t", system="s", user="u", schema=Answer).cached is False

    def test_clearing_the_cache_is_logged(self, project):
        c = client(project)
        c.ask("t", system="s", user="u", schema=Answer)
        assert c.clear_cache() == 1
        assert any(e.action == "ai_cache_cleared" for e in project.audit.entries())


class TestLogging:
    def test_every_call_records_what_the_integrity_rules_require(self, project):
        c = client(project)
        c.ask("screen:a", system="SYSTEM", user="USER", schema=Answer,
              entity="record", entity_id=7)
        row = project.db.query_one("SELECT * FROM ai_call ORDER BY id DESC LIMIT 1")
        assert row["stage"] == "screen"
        assert row["task"] == "screen:a"
        assert row["model"] and row["provider"] == "mock"
        assert row["temperature"] == 0.0
        assert len(row["prompt_hash"]) == 64 and len(row["input_hash"]) == 64
        assert row["entity"] == "record" and row["entity_id"] == "7"
        assert row["created_at"]

    def test_the_prompt_itself_is_not_stored_only_its_hash(self, project):
        c = client(project)
        c.ask("t", system="a secret system prompt", user="u", schema=Answer)
        row = project.db.query_one("SELECT * FROM ai_call ORDER BY id DESC LIMIT 1")
        assert "secret system prompt" not in json.dumps(dict(row))

    def test_usage_is_summarised_for_the_ai_use_statement(self, project):
        c = client(project)
        c.ask("t", system="s", user="u1", schema=Answer)
        c.ask("t", system="s", user="u2", schema=Answer)
        summary = c.usage_summary()
        assert summary["n_calls"] == 2
        assert summary["prompt_tokens"] > 0
        assert c.models_used()[0]["model"].startswith("mock")


class TestCost:
    def test_a_known_model_prices_its_tokens(self):
        cost, known = estimate_cost("claude-sonnet-5-5", 1_000_000, 0)
        assert known is True and cost == pytest.approx(DEFAULT_RATES["claude-sonnet-5-5"].input_per_million)

    def test_an_unknown_model_says_so_rather_than_claiming_zero(self):
        cost, known = estimate_cost("some-new-model", 1_000_000, 1_000_000)
        assert known is False and cost == 0.0

    def test_a_reviewer_override_wins(self):
        rate, known = rate_for("claude-sonnet-5-5", {"claude-sonnet": Rate(1.0, 2.0)})
        assert known and rate.input_per_million == 1.0

    def test_locally_run_models_cost_nothing(self):
        _, known = estimate_cost("llama3.1:8b", 1_000, 1_000)
        assert known is True

    def test_an_estimate_is_shown_before_a_bulk_run(self, project):
        c = client(project)
        estimate = c.estimate([("system", "record " * 100)] * 20, schema=Answer)
        assert estimate.n_calls == 20
        assert estimate.total_tokens > 0
        assert "20 calls" in estimate.describe()

    def test_already_cached_prompts_are_priced_at_zero(self, project):
        c = client(project)
        c.ask("t", system="s", user="u", schema=Answer)
        estimate = c.estimate([("s", "u"), ("s", "other")], schema=Answer)
        assert estimate.n_cached == 1 and estimate.n_calls == 1

    def test_an_unpriced_model_is_described_honestly(self, project):
        c = client(project, MockProvider(model="brand-new-model"))
        estimate = c.estimate([("s", "u " * 50)], schema=Answer)
        assert "cannot be estimated" in estimate.describe()

    def test_a_budget_stops_the_next_call(self, project):
        c = client(project, MockProvider(model="claude-opus-test"))
        c.set_budget(0.0000001)
        c.ask("t", system="s", user="u1", schema=Answer)
        with pytest.raises(BudgetExceeded, match="reaches the limit"):
            c.ask("t", system="s", user="u2", schema=Answer)

    def test_token_estimation_is_proportional_to_length(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens("x" * 400) == 100


class TestProviderRegistry:
    def test_the_four_backends_are_registered(self):
        assert set(PROVIDERS) == {
            "anthropic", "openai_compatible", "ollama", "chatgpt_subscription"
        }

    def test_only_the_pay_per_use_backends_need_a_key(self):
        assert PROVIDERS["anthropic"].needs_key is True
        assert PROVIDERS["ollama"].needs_key is False
        assert PROVIDERS["chatgpt_subscription"].needs_key is False

    def test_a_local_model_is_marked_as_keeping_data_on_the_machine(self):
        assert PROVIDERS["ollama"].sends_data_off_device is False
        assert PROVIDERS["anthropic"].sends_data_off_device is True

    def test_building_an_unknown_provider_lists_the_known_ones(self):
        with pytest.raises(ProviderError, match="anthropic"):
            build_provider("telepathy")

    def test_a_provider_without_a_key_refuses_before_making_a_request(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        provider = build_provider("anthropic", api_key=None)
        provider.api_key = None
        with pytest.raises(AuthenticationError, match="No Anthropic API key"):
            provider.complete(system="s", user="u")


class TestSettings:
    def test_settings_default_to_a_free_local_model_without_a_paid_account(
        self, project, monkeypatch
    ):
        from slr_studio.ai import keys

        monkeypatch.setattr(keys, "has_key", lambda provider: False)
        monkeypatch.setattr(
            "slr_studio.ai.chatgpt.ChatGPTSession.signed_in", lambda self: False
        )
        settings = load_settings(project)
        assert settings.provider == "ollama"
        assert settings.resolved_model()

    def test_settings_default_to_anthropic_when_its_key_is_stored(self, project, monkeypatch):
        from slr_studio.ai import keys

        monkeypatch.setattr(keys, "has_key", lambda provider: provider == "anthropic")
        assert load_settings(project).provider == "anthropic"

    def test_settings_default_to_the_chatgpt_plan_when_signed_in(self, project, monkeypatch):
        from slr_studio.ai import keys

        monkeypatch.setattr(keys, "has_key", lambda provider: False)
        monkeypatch.setattr(
            "slr_studio.ai.chatgpt.ChatGPTSession.signed_in", lambda self: True
        )
        assert load_settings(project).provider == "chatgpt_subscription"

    def test_settings_round_trip_through_the_project(self, project):
        save_settings(project, ModelSettings(provider="ollama", model="llama3.1:8b",
                                             budget_usd=5.0, offline=True))
        settings = load_settings(project)
        assert settings.provider == "ollama"
        assert settings.budget_usd == 5.0
        assert settings.offline is True

    def test_changing_the_model_is_logged(self, project):
        save_settings(project, ModelSettings(provider="ollama"), actor="human:test")
        assert any(
            e.action == "model_settings_changed" for e in project.audit.entries()
        )

    def test_an_api_key_is_never_written_to_the_project(self, project):
        save_settings(project, ModelSettings(provider="anthropic", model="m"))
        rows = project.db.query("SELECT key, value FROM meta")
        assert not any("key" in str(r["value"]).casefold() for r in rows)


def test_current_small_models_are_priced_at_their_own_rate_not_the_familys():
    """Haiku 5.5 costs an eighth of the old Haiku family rate. Pricing it at
    the family rate would make every estimate, and every budget stop, wrong."""
    from slr_studio.ai.pricing import rate_for

    rate, known = rate_for("claude-haiku-5-5")
    assert known and (rate.input_per_million, rate.output_per_million) == (0.10, 0.50)
    rate, known = rate_for("claude-haiku-4-5")
    assert known and rate.input_per_million == 0.80
