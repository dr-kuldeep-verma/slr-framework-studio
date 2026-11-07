"""Running on a rate-limited free tier.

Free API tiers cap requests per minute and per day. A benchmark makes
thousands of calls, so a run on one has to go slowly, wait out the
per-minute limit, stop cleanly at the daily one, and carry on the next day
without paying twice for anything already answered.
"""

from __future__ import annotations

import csv

import pytest

from slr_studio.ai import AIClient, MockProvider
from slr_studio.ai.base import RateLimitError
from slr_studio.ai.throttle import ThrottledProvider
from slr_studio.evaluation.benchmark import run_benchmark
from slr_studio.evaluation.datasets import load_csv

PROTOCOL = (
    "Include empirical studies of resident participation in tourism "
    "planning. Exclude studies of tourists."
)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class DailyCapProvider(MockProvider):
    """The mock model with a quota: after *cap* calls, every call is refused."""

    def __init__(self, cap):
        super().__init__()
        self.cap = cap

    def complete(self, **kwargs):
        if self.n_calls >= self.cap:
            raise RateLimitError("Quota exceeded for requests per day.")
        return super().complete(**kwargs)


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "d.csv"
    rows = [
        ("Resident participation in tourism planning in Kerala", "Residents.", 1),
        ("Community involvement in sustainable tourism", "Residents took part.", 1),
        ("Residents and tourism planning: a case study", "", 1),
        ("Soil chemistry of upland pasture", "Nitrogen.", 0),
        ("A review of turbine blade fatigue", "Metallurgy.", 0),
        ("Tourist satisfaction with hotel breakfasts", "Visitors.", 0),
    ]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["title", "abstract", "label_abstract_included"])
        w.writerows(rows)
    return load_csv(path, name="d")


class TestThrottle:
    def test_calls_are_spaced_to_the_limit(self):
        clock = FakeClock()
        provider = ThrottledProvider(
            MockProvider(), per_minute=10, clock=clock, sleep=clock.sleep
        )
        for _ in range(3):
            provider.complete(system="s", user="u")
        # The first goes at once; each later one waits out six seconds.
        assert clock.slept == [6.0, 6.0]

    def test_it_looks_like_the_provider_it_wraps(self):
        inner = MockProvider()
        provider = ThrottledProvider(inner, per_minute=10)
        assert provider.model == inner.model
        assert provider.name == inner.name
        assert provider.sends_data_off_device == inner.sends_data_off_device

    def test_a_limit_must_be_positive(self):
        with pytest.raises(ValueError):
            ThrottledProvider(MockProvider(), per_minute=0)


class TestWaitingOutAPerMinuteLimit:
    def test_a_rate_limit_waits_long_enough_for_the_minute_to_turn(self, tmp_path):
        from slr_studio.core.project import Project
        from slr_studio.screening.schemas import ScreeningAnswer

        class OnceLimited(MockProvider):
            def complete(self, **kwargs):
                if self.n_calls == 0:
                    self.n_calls += 1
                    raise RateLimitError("Rate limited by the provider.")
                return super().complete(**kwargs)

        slept = []
        project = Project.create(tmp_path / "p", "t")
        try:
            client = AIClient(project, OnceLimited(), stage="screen",
                              sleep=slept.append)
            client.ask(
                "screen",
                system="Screen against: " + PROTOCOL,
                user="Title: Resident participation in tourism planning",
                schema=ScreeningAnswer,
            )
        finally:
            project.close()
        assert slept and slept[0] >= 20


class TestADailyCap:
    def _factory(self, provider):
        return lambda project: AIClient(
            project, provider, stage="screen", sleep=lambda _: None
        )

    def test_hitting_it_pauses_the_run_rather_than_crashing(self, dataset, tmp_path):
        result = run_benchmark(
            dataset, protocol=PROTOCOL,
            client_factory=self._factory(DailyCapProvider(cap=5)),
            workspace=tmp_path / "w",
        )
        assert result.complete is False
        assert any("rate limit" in n.lower() for n in result.notes)

    def test_the_next_day_it_carries_on_without_paying_twice(self, dataset, tmp_path):
        first = DailyCapProvider(cap=5)
        run_benchmark(dataset, protocol=PROTOCOL,
                      client_factory=self._factory(first),
                      workspace=tmp_path / "w")

        second = MockProvider()
        result = run_benchmark(dataset, protocol=PROTOCOL,
                               client_factory=self._factory(second),
                               workspace=tmp_path / "w")
        assert result.complete is True
        assert sum(result.score.counts.values()) == 6
        # Six records, two screeners each: twelve calls in all. The first day
        # answered some of them, and those are not bought again.
        assert second.n_calls < 12

    def test_agreement_covers_every_record_not_just_the_last_day(
        self, dataset, tmp_path
    ):
        run_benchmark(dataset, protocol=PROTOCOL,
                      client_factory=self._factory(DailyCapProvider(cap=5)),
                      workspace=tmp_path / "w")
        result = run_benchmark(dataset, protocol=PROTOCOL,
                               client_factory=self._factory(MockProvider()),
                               workspace=tmp_path / "w")
        assert result.agreement["n"] == 6

    def test_resuming_with_a_different_dataset_is_refused(self, dataset, tmp_path):
        from slr_studio.evaluation.benchmark import BenchmarkError

        run_benchmark(dataset, protocol=PROTOCOL,
                      client_factory=self._factory(DailyCapProvider(cap=5)),
                      workspace=tmp_path / "w")
        with pytest.raises(BenchmarkError, match="different"):
            run_benchmark(dataset, protocol=PROTOCOL + " Changed.",
                          client_factory=self._factory(MockProvider()),
                          workspace=tmp_path / "w")


class TestJsonModeFallback:
    """Endpoints that speak the OpenAI API do not all accept its JSON mode.
    One that refuses it is asked again without it, once, and remembers."""

    def test_a_refused_json_mode_is_dropped_and_remembered(self, monkeypatch):
        from slr_studio.ai import providers
        from slr_studio.ai.base import ProviderError

        payloads = []

        def fake_post(url, headers, payload, timeout):
            payloads.append(dict(payload))
            if "response_format" in payload:
                error = ProviderError(
                    'The provider rejected the request: {"error": "Invalid '
                    'value at response_format"}'
                )
                error.retryable = False
                raise error
            return {"model": "gemini-x", "choices": [
                {"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}
            ], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}

        monkeypatch.setattr(providers, "_post", fake_post)
        p = providers.OpenAICompatibleProvider("gemini-x", api_key="k")
        assert p.complete(system="s", user="u").text == '{"ok": true}'
        p.complete(system="s", user="u")
        assert ["response_format" in x for x in payloads] == [True, False, False]

    def test_other_refusals_are_not_swallowed(self, monkeypatch):
        from slr_studio.ai import providers
        from slr_studio.ai.base import ProviderError

        def fake_post(url, headers, payload, timeout):
            error = ProviderError("The provider rejected the request: bad model")
            error.retryable = False
            raise error

        monkeypatch.setattr(providers, "_post", fake_post)
        p = providers.OpenAICompatibleProvider("gemini-x", api_key="k")
        with pytest.raises(ProviderError, match="bad model"):
            p.complete(system="s", user="u")


class TestSystemInstructionFallback:
    """Some open models served by Google (Gemma) refuse a separate system
    message. One that does is sent the same words in the user message."""

    def test_a_refused_system_message_is_folded_into_the_user_message(
        self, monkeypatch
    ):
        from slr_studio.ai import providers
        from slr_studio.ai.base import ProviderError

        sent = []

        def fake_post(url, headers, payload, timeout):
            sent.append([m["role"] for m in payload["messages"]])
            if payload["messages"][0]["role"] == "system":
                error = ProviderError(
                    "The provider rejected the request: Developer instruction "
                    "is not enabled for models/gemma-4-31b-it"
                )
                error.retryable = False
                raise error
            assert payload["messages"][0]["content"].startswith("RULES")
            assert payload["messages"][0]["content"].endswith("RECORD")
            return {"model": "gemma-4-31b-it", "choices": [
                {"message": {"content": "{}"}, "finish_reason": "stop"}]}

        monkeypatch.setattr(providers, "_post", fake_post)
        p = providers.OpenAICompatibleProvider(
            "gemma-4-31b-it", api_key="k", json_mode=False
        )
        p.complete(system="RULES", user="RECORD")
        p.complete(system="RULES", user="RECORD")
        assert sent == [["system", "user"], ["user"], ["user"]]


class TestServerErrors:
    """A provider's own failures (HTTP 5xx) are usually brief. They are
    waited out, and if they persist the run pauses -- resumably -- rather
    than ending with a traceback and a lost evening."""

    def test_a_server_error_is_waited_out_not_hammered(self, tmp_path):
        from slr_studio.ai.base import ProviderError
        from slr_studio.core.project import Project
        from slr_studio.screening.schemas import ScreeningAnswer

        class Flaky(MockProvider):
            def complete(self, **kwargs):
                if self.n_calls == 0:
                    self.n_calls += 1
                    raise ProviderError("The provider returned 500.")
                return super().complete(**kwargs)

        slept = []
        project = Project.create(tmp_path / "p", "t")
        try:
            client = AIClient(project, Flaky(), stage="screen", sleep=slept.append)
            client.ask("screen", system="Screen against: " + PROTOCOL,
                       user="Title: Resident participation", schema=ScreeningAnswer)
        finally:
            project.close()
        assert slept and slept[0] >= 10

    def test_a_persistent_server_error_pauses_the_run(self, dataset, tmp_path):
        from slr_studio.ai.base import ProviderError

        class Down(MockProvider):
            def complete(self, **kwargs):
                if self.n_calls >= 4:
                    raise ProviderError("The provider returned 500.")
                return super().complete(**kwargs)

        result = run_benchmark(
            dataset, protocol=PROTOCOL,
            client_factory=lambda project: AIClient(
                project, Down(), stage="screen", sleep=lambda _: None),
            workspace=tmp_path / "w",
        )
        assert result.complete is False
        assert any("500" in n for n in result.notes)

    def test_a_refused_key_is_not_retried_as_if_it_were_transient(self, dataset,
                                                                  tmp_path):
        from slr_studio.ai.base import AuthenticationError

        class BadKey(MockProvider):
            def complete(self, **kwargs):
                raise AuthenticationError("The API key was rejected.")

        with pytest.raises(AuthenticationError):
            run_benchmark(
                dataset, protocol=PROTOCOL,
                client_factory=lambda project: AIClient(
                    project, BadKey(), stage="screen", sleep=lambda _: None),
                workspace=tmp_path / "w",
            )


def test_the_server_error_message_carries_the_providers_words(monkeypatch):
    from slr_studio.ai import providers
    from slr_studio.ai.base import ProviderError

    class Response:
        status_code = 503
        text = '{"error": {"message": "The model is overloaded."}}'

    class FakeHttpx:
        TimeoutException = HTTPError = Exception

        @staticmethod
        def post(*a, **k):
            return Response()

    monkeypatch.setattr(providers, "_httpx", lambda: FakeHttpx)
    with pytest.raises(ProviderError, match="overloaded"):
        providers._post("u", {}, {}, 1)


class TestOneModelPerRun:
    """A result is a property of one model. Resuming with another would mix
    two models' decisions in one score and report it under one name."""

    def test_resuming_with_another_model_is_refused(self, dataset, tmp_path):
        from slr_studio.evaluation.benchmark import BenchmarkError

        first = DailyCapProvider(cap=5)
        first.model = "model-one"
        run_benchmark(dataset, protocol=PROTOCOL,
                      client_factory=lambda p: AIClient(p, first, stage="screen",
                                                        sleep=lambda _: None),
                      workspace=tmp_path / "w")
        second = MockProvider()
        second.model = "model-two"
        with pytest.raises(BenchmarkError, match="model-one"):
            run_benchmark(dataset, protocol=PROTOCOL,
                          client_factory=lambda p: AIClient(p, second, stage="screen",
                                                            sleep=lambda _: None),
                          workspace=tmp_path / "w")

    def test_resuming_with_the_same_model_is_allowed(self, dataset, tmp_path):
        first = DailyCapProvider(cap=5)
        first.model = "model-one"
        run_benchmark(dataset, protocol=PROTOCOL,
                      client_factory=lambda p: AIClient(p, first, stage="screen",
                                                        sleep=lambda _: None),
                      workspace=tmp_path / "w")
        again = MockProvider()
        again.model = "model-one"
        result = run_benchmark(dataset, protocol=PROTOCOL,
                               client_factory=lambda p: AIClient(p, again, stage="screen",
                                                                 sleep=lambda _: None),
                               workspace=tmp_path / "w")
        assert result.complete


class TestModelsThatRefuseTemperature:
    """Newer models refuse a temperature setting. They are asked without it,
    and the log says it was not sent -- never that it was 0."""

    @staticmethod
    def _refusal():
        from slr_studio.ai.base import ProviderError

        error = ProviderError(
            'The provider rejected the request: {"type":"error","error":'
            '{"type":"invalid_request_error","message":"`temperature` is '
            'deprecated for this model."}}'
        )
        error.retryable = False
        return error

    def test_anthropic_retries_without_it_and_remembers(self, monkeypatch):
        from slr_studio.ai import providers

        sent = []

        def fake_post(url, headers, payload, timeout):
            sent.append("temperature" in payload)
            if "temperature" in payload:
                raise self._refusal()
            return {"model": "claude-haiku-5-5", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "{}"}],
                    "usage": {"input_tokens": 5, "output_tokens": 2}}

        monkeypatch.setattr(providers, "_post", fake_post)
        p = providers.AnthropicProvider("claude-haiku-5-5", api_key="k")
        p.complete(system="s", user="u")
        p.complete(system="s", user="u")
        assert sent == [True, False, False]
        assert p.sends_temperature is False

    def test_the_openai_compatible_route_does_the_same(self, monkeypatch):
        from slr_studio.ai import providers

        sent = []

        def fake_post(url, headers, payload, timeout):
            sent.append("temperature" in payload)
            if "temperature" in payload:
                raise self._refusal()
            return {"model": "m", "choices": [
                {"message": {"content": "{}"}, "finish_reason": "stop"}]}

        monkeypatch.setattr(providers, "_post", fake_post)
        p = providers.OpenAICompatibleProvider("m", api_key="k", json_mode=False)
        p.complete(system="s", user="u")
        assert sent == [True, False]

    def test_the_log_records_that_it_was_not_sent(self, tmp_path):
        from slr_studio.ai.base import TEMPERATURE_NOT_SENT
        from slr_studio.core.project import Project
        from slr_studio.screening.schemas import ScreeningAnswer

        provider = MockProvider()
        provider.sends_temperature = False
        project = Project.create(tmp_path / "p", "t")
        try:
            client = AIClient(project, provider, stage="screen", sleep=lambda _: None)
            client.ask("screen", system="Screen against: " + PROTOCOL,
                       user="Title: Resident participation", schema=ScreeningAnswer)
            logged = project.db.scalar("SELECT temperature FROM ai_call")
        finally:
            project.close()
        assert logged == TEMPERATURE_NOT_SENT

    def test_the_exported_log_says_so_in_words(self):
        from slr_studio.ai.base import TEMPERATURE_NOT_SENT
        from slr_studio.exports.methods import _temperature_label

        assert _temperature_label(0.0) == 0.0
        assert "not sent" in _temperature_label(TEMPERATURE_NOT_SENT)


def test_a_throttled_provider_reports_the_real_temperature_behaviour():
    """The pacer wraps the provider; the log must see the wrapped one's
    answer, or a model that refused temperature is logged as having had 0."""
    inner = MockProvider()
    inner.sends_temperature = False
    assert ThrottledProvider(inner, per_minute=10).sends_temperature is False


class TestAnAnswerCutOffAtTheLimit:
    """A verbose model can run past the token limit mid-JSON. The error
    says so, and the retry asks for a shorter answer rather than the same."""

    def test_the_retry_asks_for_brevity_and_the_error_names_the_limit(self, tmp_path):
        from slr_studio.ai.base import LLMResponse, ResponseFormatError, Usage
        from slr_studio.core.project import Project
        from slr_studio.screening.schemas import ScreeningAnswer

        prompts = []

        class Verbose(MockProvider):
            def complete(self, *, system, user, max_tokens=1024, **kw):
                prompts.append(user)
                return LLMResponse(text='{"decision": "exclude", "reason": "Because',
                                   model="m", model_version="m",
                                   usage=Usage(10, max_tokens), stop_reason="max_tokens")

        project = Project.create(tmp_path / "p", "t")
        try:
            client = AIClient(project, Verbose(), stage="screen", sleep=lambda _: None)
            with pytest.raises(ResponseFormatError, match="700"):
                client.ask("screen", system="s", user="Title: X",
                           schema=ScreeningAnswer, max_tokens=700)
        finally:
            project.close()
        assert "cut off" in prompts[1] and "shorter" in prompts[1]


def test_the_report_names_the_real_provider_not_the_pacer(dataset, tmp_path):
    inner = MockProvider()
    result = run_benchmark(
        dataset, protocol=PROTOCOL,
        client_factory=lambda p: AIClient(p, ThrottledProvider(inner, 6000,
                                          sleep=lambda s: None),
                                          stage="screen", sleep=lambda _: None),
        workspace=tmp_path / "w",
    )
    assert result.provider == inner.name
