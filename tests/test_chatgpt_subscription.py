"""Sign in with ChatGPT: the sign-in, the tokens, and the provider.

No network: a fake stands in for OpenAI, and the ID tokens it issues are
signed with a real RSA key so the signature check is exercised, not skipped.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.request
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from slr_studio.ai import chatgpt, keys
from slr_studio.ai.base import (
    AuthenticationError,
    BudgetExceeded,
    PlanLimitReached,
    ProviderError,
)
from slr_studio.ai.chatgpt import (
    ChatGPTSession,
    ChatGPTSubscriptionProvider,
    SignInError,
    _b64url,
    pkce_pair,
)

CLIENT_ID = "oaiapp_test123"


class MemoryKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        # Windows Credential Manager's limit, as UTF-16.
        assert len(value) <= 1280, "a credential-store entry would be too long for Windows"
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


@pytest.fixture
def ring(monkeypatch):
    memory = MemoryKeyring()
    monkeypatch.setattr(keys, "_keyring", lambda: memory)
    return memory


class Response:
    def __init__(self, status: int = 200, body: Any = None, lines: list[str] | None = None):
        self.status_code = status
        self._body = body
        self._lines = lines or []

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body

    def read(self):
        return b""

    def iter_lines(self):
        yield from self._lines


class FakeOpenAI:
    """Just enough of auth.openai.com and api.openai.com."""

    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        public.update({"kid": "k1", "use": "sig", "alg": "RS256"})
        self.jwks = {"keys": [public]}
        self.posts: list[tuple[str, dict]] = []
        self.streams: list[dict] = []
        self.nonce = ""
        self.audience = CLIENT_ID
        self.scope = "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"
        self.refresh_error: str | None = None
        #: How far OpenAI's clock runs ahead of this computer's.
        self.clock_ahead = 0
        self.n_refresh = 0
        self.stream_replies: list[Response] = []
        self.models = {"models": [
            {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"},
            {"slug": "internal-x", "display_name": "Hidden", "visibility": "hide"},
        ]}

    def id_token(self, *, sub: str = "user-1", issuer: str = chatgpt.ISSUER) -> str:
        now = int(time.time()) + self.clock_ahead
        claims = {
            "iss": issuer, "aud": self.audience, "sub": sub, "email": "reviewer@example.org",
            "iat": now, "exp": now + 3600, "nonce": self.nonce,
        }
        return jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": "k1"})

    # httpx-shaped functions
    def get(self, url, headers=None, timeout=None):
        if url == chatgpt.OIDC_CONFIG_URL:
            return Response(body={
                "issuer": chatgpt.ISSUER,
                "jwks_uri": "https://auth.openai.com/.well-known/jwks.json",
                "revocation_endpoint": "https://auth.openai.com/oauth/revoke",
            })
        if url.endswith("jwks.json"):
            return Response(body=self.jwks)
        if url.endswith("/v1/models"):
            return Response(body=self.models)
        return Response(404, {})

    def post(self, url, data=None, timeout=None):
        self.posts.append((url, dict(data or {})))
        if url == chatgpt.TOKEN_URL and data["grant_type"] == "authorization_code":
            return Response(body={
                "access_token": "A" * 2000, "refresh_token": "R1", "id_token": self.id_token(),
                "expires_in": 3600, "scope": self.scope,
            })
        if url == chatgpt.TOKEN_URL and data["grant_type"] == "refresh_token":
            self.n_refresh += 1
            if self.refresh_error:
                return Response(400, {"error": self.refresh_error})
            return Response(body={
                "access_token": f"A-refreshed-{self.n_refresh}",
                "refresh_token": f"R{self.n_refresh + 1}", "expires_in": 3600,
            })
        if url.endswith("/oauth/revoke"):
            return Response(200, None)
        return Response(404, {})

    @contextmanager
    def stream(self, method, url, headers=None, json=None, timeout=None):
        self.streams.append({"url": url, "headers": headers, "json": json})
        yield self.stream_replies.pop(0)


def sse(*events: dict) -> list[str]:
    lines = []
    for event in events:
        lines += [f"event: {event['type']}", "data: " + json.dumps(event), ""]
    return lines


def completed(text: str, model: str = "gpt-6.1-sol") -> Response:
    half = len(text) // 2
    return Response(lines=sse(
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.output_text.delta", "delta": text[:half]},
        {"type": "response.output_text.delta", "delta": text[half:]},
        {"type": "response.completed", "response": {
            "id": "r1", "status": "completed", "model": model,
            "usage": {"input_tokens": 120, "output_tokens": 30},
        }},
    ))


@pytest.fixture
def openai():
    return FakeOpenAI()


@pytest.fixture
def clock():
    class Clock:
        now = 1_800_000_000.0

        def __call__(self):
            return self.now

    return Clock()


def signed_in_session(openai, ring, clock) -> ChatGPTSession:
    session = ChatGPTSession(http=openai, clock=clock, sleep=lambda s: None)
    session.host_id()  # made when the authorisation URL is built
    openai.nonce = "n-1"
    session.complete_sign_in(
        {"code": "c", "state": "s-1", "client_id": CLIENT_ID, "scope": openai.scope},
        state="s-1", nonce="n-1", verifier="v", redirect_uri="http://127.0.0.1:1455/auth/callback",
        account=None,
    )
    return session


# -- signing in ----------------------------------------------------------------


class TestSignIn:
    def test_pkce_challenge_is_the_s256_of_the_verifier(self):
        verifier, challenge = pkce_pair()
        assert 43 <= len(verifier) <= 128
        assert challenge == _b64url(hashlib.sha256(verifier.encode()).digest())
        assert "=" not in challenge

    def test_first_sign_in_asks_openai_to_register_this_copy(self, ring):
        session = ChatGPTSession(http=FakeOpenAI())
        url = session.authorization_url(
            redirect_uri="http://127.0.0.1:1455/auth/callback", state="s", nonce="n",
            challenge="c", account=None,
        )
        assert url.startswith(chatgpt.AUTHORIZE_URL + "?")
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert q["client_id"] == "dynamic_agent_client"
        assert q["agent_name_hint"] == "SLR Framework Studio"
        assert q["ext_agent_host_id"].startswith("urn:uuid:")
        assert q["code_challenge_method"] == "S256"
        assert q["resource"] == "https://api.openai.com/v1"
        assert set(q["scope"].split()) >= {"openid", "offline_access", "resource.invoke",
                                           "chatgpt.tokens.use.direct"}
        assert q["redirect_uri"].startswith("http://127.0.0.1:")

    def test_the_host_id_stays_the_same(self, ring):
        session = ChatGPTSession(http=FakeOpenAI())
        assert session.host_id() == session.host_id()

    def test_signing_in_again_reuses_the_issued_client_and_omits_the_name(self, openai, ring, clock):
        session = signed_in_session(openai, ring, clock)
        url = session.authorization_url(
            redirect_uri="http://127.0.0.1:5000/auth/callback", state="s", nonce="n",
            challenge="c", account=session.account(), id_token="idt",
        )
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert q["client_id"] == CLIENT_ID
        assert "agent_name_hint" not in q
        assert q["id_token_hint"] == "idt"
        assert q["login_hint"] == "reviewer@example.org"

    def test_a_completed_sign_in_is_verified_and_stored_outside_the_project(self, openai, ring, clock):
        session = signed_in_session(openai, ring, clock)
        account = session.account()
        assert account.client_id == CLIENT_ID
        assert account.email == "reviewer@example.org"
        assert account.plan_enabled
        assert session.signed_in()
        assert "reviewer@example.org" in session.status()
        # The code exchange used the issued client id, PKCE and the resource.
        url, form = openai.posts[0]
        assert url == chatgpt.TOKEN_URL
        assert form["client_id"] == CLIENT_ID and form["code_verifier"] == "v"
        assert form["resource"] == chatgpt.RESOURCE
        assert "client_secret" not in form
        # Tokens are not in the account record, and the long access token was
        # split to fit Windows' credential store.
        record = keys.get_secret("chatgpt_subscription:account")
        assert "A" * 50 not in record and "R1" not in record
        assert keys.get_secret("chatgpt_subscription:access") == "A" * 2000

    def test_a_computer_clock_a_little_behind_openai_is_tolerated(self, openai, ring, clock):
        # The first real sign-in failed with "The token is not yet valid (iat)".
        openai.clock_ahead = 45
        session = signed_in_session(openai, ring, clock)
        assert session.signed_in()

    def test_a_token_from_far_in_the_future_is_still_refused(self, openai, ring):
        openai.clock_ahead = 3600
        openai.nonce = "n"
        with pytest.raises(SignInError, match="did not verify"):
            ChatGPTSession(http=openai).complete_sign_in(
                {"code": "c", "state": "s", "client_id": CLIENT_ID},
                state="s", nonce="n", verifier="v", redirect_uri="r", account=None,
            )

    def test_a_reply_for_another_request_is_refused(self, openai, ring):
        session = ChatGPTSession(http=openai)
        with pytest.raises(SignInError, match="did not match"):
            session.complete_sign_in(
                {"code": "c", "state": "other", "client_id": CLIENT_ID},
                state="s", nonce="n", verifier="v", redirect_uri="r", account=None,
            )
        assert not session.signed_in()

    def test_an_id_token_for_another_sign_in_is_refused(self, openai, ring):
        session = ChatGPTSession(http=openai)
        openai.nonce = "someone-elses"
        with pytest.raises(SignInError, match="not issued for this sign-in"):
            session.complete_sign_in(
                {"code": "c", "state": "s", "client_id": CLIENT_ID},
                state="s", nonce="n", verifier="v", redirect_uri="r", account=None,
            )

    def test_an_id_token_for_another_application_is_refused(self, openai, ring):
        session = ChatGPTSession(http=openai)
        openai.nonce = "n"
        openai.audience = "oaiapp_somebody_else"
        with pytest.raises(SignInError, match="did not verify"):
            session.complete_sign_in(
                {"code": "c", "state": "s", "client_id": CLIENT_ID},
                state="s", nonce="n", verifier="v", redirect_uri="r", account=None,
            )

    def test_a_forged_id_token_is_refused(self, openai, ring):
        session = ChatGPTSession(http=openai)
        openai.nonce = "n"
        genuine_key = openai.private_key
        openai.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        try:
            with pytest.raises(SignInError, match="did not verify"):
                session.complete_sign_in(
                    {"code": "c", "state": "s", "client_id": CLIENT_ID},
                    state="s", nonce="n", verifier="v", redirect_uri="r", account=None,
                )
        finally:
            openai.private_key = genuine_key

    def test_a_sign_in_without_plan_use_cannot_run_the_review(self, openai, ring, clock):
        openai.scope = "openid profile email offline_access"
        session = signed_in_session(openai, ring, clock)
        assert "plan use was not granted" in session.status()
        with pytest.raises(AuthenticationError, match="did not allow plan use"):
            session.access_token()

    def test_the_whole_browser_round_trip_over_loopback(self, openai, ring):
        """The real callback server, with a 'browser' that follows the redirect."""
        session = ChatGPTSession(http=openai)

        def browser(url: str) -> None:
            q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            openai.nonce = q["nonce"]
            target = q["redirect_uri"] + "?" + urlencode(
                {"code": "c", "state": q["state"], "client_id": CLIENT_ID, "scope": openai.scope}
            )
            threading.Thread(
                target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True
            ).start()

        session._open_browser = browser
        account = session.sign_in(timeout=10)
        assert account.client_id == CLIENT_ID and session.signed_in()

    def test_a_cancelled_sign_in_changes_nothing(self, openai, ring):
        session = ChatGPTSession(http=openai, open_browser=lambda url: None)
        with pytest.raises(SignInError, match="cancelled"):
            session.sign_in(timeout=10, should_stop=lambda: True)
        assert not session.signed_in()


# -- tokens ----------------------------------------------------------------------


class TestTokens:
    def test_a_fresh_token_is_used_without_refreshing(self, openai, ring, clock):
        session = signed_in_session(openai, ring, clock)
        assert session.access_token() == "A" * 2000
        assert openai.n_refresh == 0

    def test_a_token_near_expiry_is_refreshed_and_both_tokens_replaced(self, openai, ring, clock):
        session = signed_in_session(openai, ring, clock)
        clock.now += 3600 - 60
        assert session.access_token() == "A-refreshed-1"
        url, form = openai.posts[-1]
        assert form["grant_type"] == "refresh_token"
        assert form["client_id"] == CLIENT_ID  # never dynamic_agent_client
        assert form["refresh_token"] == "R1"
        assert keys.get_secret("chatgpt_subscription:refresh") == "R2"
        # And the next refresh uses the replacement.
        clock.now += 3600
        session.access_token()
        assert openai.posts[-1][1]["refresh_token"] == "R2"

    def test_a_dead_refresh_token_asks_for_a_new_sign_in_and_keeps_the_registration(
        self, openai, ring, clock
    ):
        session = signed_in_session(openai, ring, clock)
        openai.refresh_error = "refresh_token_reused"
        clock.now += 4000
        with pytest.raises(AuthenticationError, match="Sign in again"):
            session.access_token()
        assert not session.signed_in()
        assert session.account().client_id == CLIENT_ID
        assert keys.get_secret("chatgpt_subscription:host")

    def test_signing_out_revokes_and_forgets_the_tokens(self, openai, ring, clock):
        session = signed_in_session(openai, ring, clock)
        host = session.host_id()
        assert session.sign_out() is True
        url, form = openai.posts[-1]
        assert url.endswith("/oauth/revoke")
        assert form == {"token": "R1", "token_type_hint": "refresh_token", "client_id": CLIENT_ID}
        assert not session.signed_in()
        assert keys.get_secret("chatgpt_subscription:access") is None
        assert session.host_id() == host
        assert session.account().client_id == CLIENT_ID

    def test_only_listed_models_are_offered(self, openai, ring, clock):
        session = signed_in_session(openai, ring, clock)
        assert [m["slug"] for m in session.list_models()] == ["gpt-6.1-sol"]

    def test_long_secrets_survive_the_round_trip(self, ring):
        value = "x" * 3001 + "end"
        keys.set_secret("long", value)
        assert keys.get_secret("long") == value
        keys.set_secret("long", "short")
        assert keys.get_secret("long") == "short"
        assert ("SLR Framework Studio", "long#3") not in ring.store
        keys.delete_secret("long")
        assert keys.get_secret("long") is None


# -- the provider -------------------------------------------------------------------


class TestProvider:
    def provider(self, openai, ring, clock) -> ChatGPTSubscriptionProvider:
        return ChatGPTSubscriptionProvider("gpt-6.1-sol", session=signed_in_session(openai, ring, clock))

    def test_an_answer_is_assembled_from_the_stream(self, openai, ring, clock):
        provider = self.provider(openai, ring, clock)
        openai.stream_replies.append(completed('{"decision": "include"}'))
        response = provider.complete(system="SYSTEM", user="RECORD", max_tokens=500, temperature=0.0)
        assert response.text == '{"decision": "include"}'
        assert response.model == "gpt-6.1-sol"
        assert response.usage.prompt_tokens == 120 and response.usage.completion_tokens == 30

    def test_the_request_keeps_to_what_plan_use_accepts(self, openai, ring, clock):
        provider = self.provider(openai, ring, clock)
        openai.stream_replies.append(completed("{}"))
        provider.complete(system="SYSTEM", user="RECORD", max_tokens=500, temperature=0.0)
        sent = openai.streams[0]
        body = sent["json"]
        assert sent["url"] == "https://api.openai.com/v1/responses"
        assert sent["headers"]["Authorization"] == "Bearer " + "A" * 2000
        assert body["store"] is False and body["stream"] is True
        assert body["instructions"] == "SYSTEM"
        assert body["input"] == [{"role": "user", "content": "RECORD"}]
        for refused in ("temperature", "max_output_tokens", "previous_response_id", "metadata", "user"):
            assert refused not in body
        assert not provider.sends_temperature
        assert provider.billing == "plan"

    def test_reaching_the_plan_limit_pauses_like_a_budget(self, openai, ring, clock):
        provider = self.provider(openai, ring, clock)
        openai.stream_replies.append(Response(429, {"error": {
            "code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}))
        with pytest.raises(PlanLimitReached, match="settings/usage") as caught:
            provider.complete(system="s", user="u")
        assert isinstance(caught.value, BudgetExceeded)
        assert caught.value.retryable is False

    def test_a_limit_reported_mid_stream_is_caught_too(self, openai, ring, clock):
        provider = self.provider(openai, ring, clock)
        openai.stream_replies.append(Response(lines=sse(
            {"type": "response.output_text.delta", "delta": "{"},
            {"type": "response.failed", "response": {"error": {
                "code": "subscription_sharing_usage_limit_exceeded", "message": "x"}}},
        )))
        with pytest.raises(PlanLimitReached):
            provider.complete(system="s", user="u")

    def test_an_ineligible_account_is_explained_not_retried(self, openai, ring, clock):
        provider = self.provider(openai, ring, clock)
        openai.stream_replies.append(Response(403, {"error": {
            "code": "subscription_sharing_user_not_eligible"}}))
        with pytest.raises(AuthenticationError, match="does not allow this ChatGPT account"):
            provider.complete(system="s", user="u")

    def test_json_mode_is_off_by_default(self, openai, ring, clock):
        provider = self.provider(openai, ring, clock)
        openai.stream_replies.append(completed("{}"))
        provider.complete(system="s", user="Connection test.")
        assert "text" not in openai.streams[0]["json"]

    def test_unsupported_json_mode_is_dropped_once(self, openai, ring, clock):
        provider = self.provider(openai, ring, clock)
        provider.json_mode = True
        openai.stream_replies += [
            Response(400, {"error": {"code": "subscription_sharing_unsupported_capability",
                                     "param": "text.format"}}),
            completed("{}"),
        ]
        provider.complete(system="s", user="u")
        assert "text" in openai.streams[0]["json"] and "text" not in openai.streams[1]["json"]
        assert provider.json_mode is False

    def test_json_mode_is_dropped_when_the_input_lacks_the_word_json(self, openai, ring, clock):
        # What the connection test met on the first real sign-in.
        provider = self.provider(openai, ring, clock)
        provider.json_mode = True
        openai.stream_replies += [
            Response(400, {"error": {"message": "Response input messages must contain the "
                                                "word 'json' in some form to use 'text.format' "
                                                "of type 'json_object'.", "param": "input"}}),
            completed("OK"),
        ]
        ok, message = provider.check()
        assert ok, message
        assert "text" not in openai.streams[1]["json"]
        assert provider.json_mode is False

    def test_an_interrupted_stream_is_retryable(self, openai, ring, clock):
        provider = self.provider(openai, ring, clock)
        openai.stream_replies.append(Response(lines=sse(
            {"type": "response.output_text.delta", "delta": "{"})))
        with pytest.raises(ProviderError, match="interrupted") as caught:
            provider.complete(system="s", user="u")
        assert caught.value.retryable

    def test_a_cut_off_answer_reports_the_length_limit(self, openai, ring, clock):
        provider = self.provider(openai, ring, clock)
        openai.stream_replies.append(Response(lines=sse(
            {"type": "response.output_text.delta", "delta": "{\"a\":"},
            {"type": "response.incomplete", "response": {
                "incomplete_details": {"reason": "max_output_tokens"}}},
        )))
        assert provider.complete(system="s", user="u").stop_reason == "length"

    def test_a_revoked_access_token_is_refreshed_once(self, openai, ring, clock):
        provider = self.provider(openai, ring, clock)
        openai.stream_replies += [Response(401, {"detail": "expired"}), completed("{}")]
        provider.complete(system="s", user="u")
        assert openai.n_refresh == 1
        assert openai.streams[1]["headers"]["Authorization"] == "Bearer A-refreshed-1"


# -- the rest of the application ----------------------------------------------------


class TestInTheApplication:
    def test_plan_calls_cost_nothing_and_the_estimate_shows_limits(self, project, openai, ring, clock):
        from pydantic import BaseModel

        from slr_studio.ai.client import AIClient

        class Answer(BaseModel):
            decision: str

        provider = ChatGPTSubscriptionProvider(
            "gpt-6.1-sol", session=signed_in_session(openai, ring, clock)
        )
        client = AIClient(project, provider, stage="screen", sleep=lambda s: None)
        estimate = client.estimate([("s", "u" * 400)] * 10, schema=Answer)
        assert estimate.billing == "plan"
        assert estimate.cost_usd == 0.0 and estimate.price_known
        assert "US$" not in estimate.describe()
        assert "usage limits" in estimate.describe()

        openai.stream_replies.append(completed('{"decision": "include"}'))
        result = client.ask("screen", system="s", user="u", schema=Answer)
        assert result.cost_usd == 0.0
        row = project.db.query_one("SELECT provider, temperature FROM ai_call")
        assert row["provider"] == "chatgpt_subscription"
        assert row["temperature"] == -1.0  # recorded as not sent

        later = client.estimate([("s", "v")], schema=Answer)
        assert later.plan_calls_5h == 1 and later.plan_calls_7d == 1
        assert any("settings/usage" in line for line in later.plan_limit_lines())

    def test_the_client_does_not_retry_into_a_spent_plan(self, project, openai, ring, clock):
        from pydantic import BaseModel

        from slr_studio.ai.client import AIClient

        class Answer(BaseModel):
            decision: str

        provider = ChatGPTSubscriptionProvider(
            "gpt-6.1-sol", session=signed_in_session(openai, ring, clock)
        )
        client = AIClient(project, provider, stage="screen", sleep=lambda s: None)
        openai.stream_replies.append(Response(429, {"error": {
            "code": "subscription_sharing_usage_limit_exceeded"}}))
        with pytest.raises(PlanLimitReached):
            client.ask("screen", system="s", user="u", schema=Answer)
        assert len(openai.streams) == 1

    def test_the_ai_use_statement_says_the_plan_was_used(self, project):
        from slr_studio.core.db import utc_now
        from slr_studio.exports.methods import ai_use_statement

        project.db.insert("ai_call", {
            "stage": "screen", "task": "screen:screener_a", "provider": "chatgpt_subscription",
            "model": "gpt-6.1-sol", "model_version": "gpt-6.1-sol", "temperature": -1.0,
            "prompt_hash": "p", "input_hash": "i", "output": "{}", "output_hash": "o",
            "prompt_tokens": 1, "completion_tokens": 1, "cost_usd": 0.0, "cached": 0,
            "created_at": utc_now(),
        })
        statement = ai_use_statement(project)
        assert "ChatGPT subscription (Sign in with ChatGPT)" in statement
        assert "usage limits rather than charging" in statement
        assert "chatgpt_subscription" not in statement

    def test_the_provider_is_registered_with_sign_in(self):
        from slr_studio.ai.providers import PROVIDERS

        spec = PROVIDERS["chatgpt_subscription"]
        assert spec.auth == "signin" and not spec.needs_key
        assert spec.billing == "plan" and spec.sends_data_off_device
