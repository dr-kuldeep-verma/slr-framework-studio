"""Sign in with ChatGPT: run the review on the reviewer's own ChatGPT plan.

A reviewer with a paid ChatGPT plan (confirmed on Go, Plus and Pro) can let this application make model calls
against their plan's usage limits instead of buying API credit. OpenAI calls
this "Sign in with ChatGPT" for open-source, locally run apps, which is what
this application is. There is no client secret and no API key:

1. The application opens the reviewer's browser at OpenAI's sign-in page,
   with a PKCE challenge and a one-off callback on 127.0.0.1.
2. The reviewer signs in and agrees. OpenAI sends a code back to that
   callback, and registers this copy of the application on first use.
3. The code is exchanged for an access token (one hour), a refresh token
   (thirty days, replaced on every refresh) and an ID token. The ID token's
   signature is checked against OpenAI's published keys.
4. Model calls go to the public Responses API with the access token.

Tokens are kept in the operating system's credential store, never in the
project or the log. The audit log records that a call was answered through
the plan (provider ``chatgpt_subscription``) and by which model, so the
AI-use statement says so.

Everything here follows OpenAI's developer documentation for this flow
(developers.openai.com/siwc, read 9 November 2026). The flow was a preview
then; the endpoints are constants at the top of this module so a change is
one edit.

Anthropic does not allow Claude subscriptions to be used this way by other
applications, so there is no equivalent for Claude: Claude needs an API key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

from . import keys
from .base import (
    AuthenticationError,
    LLMResponse,
    PlanLimitReached,
    Provider,
    ProviderError,
    RateLimitError,
    Usage,
)

__all__ = [
    "AUTHORIZE_URL",
    "TOKEN_URL",
    "RESOURCE",
    "SCOPES",
    "USAGE_PAGE",
    "ChatGPTAccount",
    "ChatGPTSession",
    "ChatGPTSubscriptionProvider",
    "SignInError",
    "pkce_pair",
]

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
OIDC_CONFIG_URL = "https://auth.openai.com/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
#: Identity scopes, then the three that let the app use the plan.
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
#: Where a reviewer sees how much of the plan's allowance is left. OpenAI
#: gives applications no way to read this themselves.
USAGE_PAGE = "https://chatgpt.com/settings/usage"
#: First registration uses this port in OpenAI's examples; later sign-ins may
#: use any free port. Only the port may vary in the callback address.
REGISTRATION_PORT = 1455
CALLBACK_PATH = "/auth/callback"
APP_NAME = "SLR Framework Studio"
#: The client id used only to ask OpenAI to register this copy. Never saved.
REGISTRATION_CLIENT_ID = "dynamic_agent_client"

#: Allowed difference between this computer's clock and OpenAI's.
CLOCK_LEEWAY_S = 120

#: Refresh this long before the access token runs out.
REFRESH_MARGIN_S = 300

#: Refresh errors after which only a new sign-in will do.
_TERMINAL_REFRESH = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}

# Names in the credential store.
_HOST = "chatgpt_subscription:host"
_ACCOUNT = "chatgpt_subscription:account"
_ACCESS = "chatgpt_subscription:access"
_REFRESH = "chatgpt_subscription:refresh"
_ID = "chatgpt_subscription:id"

# One refresh at a time: the refresh token is replaced on every use, so two
# threads refreshing together would leave one holding a dead token.
_refresh_lock = threading.Lock()


class SignInError(AuthenticationError):
    """Signing in did not complete. The message says why, in plain words."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""
    verifier = _b64url(secrets.token_bytes(48))  # 64 characters
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - bundled in the app
        raise ProviderError("The httpx package is required.") from exc
    return httpx


@dataclass
class ChatGPTAccount:
    """What is remembered about the signed-in account (no tokens)."""

    client_id: str
    sub: str = ""
    email: str = ""
    scope: str = ""
    expires_at: float = 0.0

    @property
    def plan_enabled(self) -> bool:
        return PLAN_SCOPE in self.scope.split()


@dataclass
class _Callback:
    params: dict[str, str] = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)


_PAGE = (
    "<!doctype html><meta charset='utf-8'><title>{title}</title>"
    "<body style='font:16px system-ui;margin:3em;color:#1b2330'>"
    "<h2>{title}</h2><p>{body}</p></body>"
)


def _handler_for(callback: _Callback):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
            parsed = urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            callback.params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            ok = "code" in callback.params
            page = _PAGE.format(
                title="Signed in" if ok else "Sign-in did not finish",
                body=(
                    "You can close this tab and go back to SLR Framework Studio."
                    if ok
                    else "Go back to SLR Framework Studio to see what happened."
                ),
            )
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            callback.done.set()

        def log_message(self, *args: Any) -> None:
            # The callback URL holds the one-off code; keep it out of logs.
            pass

    return Handler


class ChatGPTSession:
    """Sign-in, token refresh and sign-out for one ChatGPT account.

    ``http`` is anything with httpx's ``get``, ``post`` and ``stream``
    functions (the httpx module itself by default), and ``clock`` returns
    seconds since the epoch. Both are there so the tests can run the whole
    flow without a network.
    """

    def __init__(
        self,
        *,
        http: Any = None,
        clock: Callable[[], float] = time.time,
        open_browser: Callable[[str], Any] = webbrowser.open,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._clock = clock
        self._sleep = sleep
        self._open_browser = open_browser
        self._oidc: dict[str, Any] | None = None

    @property
    def http(self):
        return self._http if self._http is not None else _httpx()

    # -- what is stored ----------------------------------------------------

    def host_id(self) -> str:
        """This computer's stable, opaque identifier for OpenAI.

        Created once and kept through sign-out, so signing in again does not
        register a second copy of the application.
        """
        existing = keys.get_secret(_HOST)
        if existing:
            return existing
        value = f"urn:uuid:{uuid.uuid4()}"
        keys.set_secret(_HOST, value)
        return value

    def account(self) -> ChatGPTAccount | None:
        raw = keys.get_secret(_ACCOUNT)
        if not raw:
            return None
        try:
            return ChatGPTAccount(**json.loads(raw))
        except (ValueError, TypeError):
            return None

    def _save_account(self, account: ChatGPTAccount) -> None:
        keys.set_secret(_ACCOUNT, json.dumps(asdict(account)))

    def signed_in(self) -> bool:
        account = self.account()
        return bool(account and keys.get_secret(_REFRESH))

    def status(self) -> str:
        """One line for the settings window."""
        account = self.account()
        if not account or not keys.get_secret(_REFRESH):
            return "Not signed in."
        who = account.email or "your ChatGPT account"
        if not account.plan_enabled:
            return (
                f"Signed in as {who}, but plan use was not granted, so the "
                "review cannot use this account. Sign in again and allow it."
            )
        return f"Signed in as {who}."

    # -- signing in --------------------------------------------------------

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        challenge: str,
        account: ChatGPTAccount | None,
        id_token: str | None = None,
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": account.client_id if account else REGISTRATION_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "ext_agent_host_id": self.host_id(),
        }
        if account is None:
            # Registration only; omitted when signing in again.
            params["agent_name_hint"] = APP_NAME
        else:
            if id_token:
                params["id_token_hint"] = id_token
            if account.email:
                params["login_hint"] = account.email
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def sign_in(self, *, timeout: float = 300.0, should_stop: Callable[[], bool] | None = None) -> ChatGPTAccount:
        """Open the browser, wait for the callback, and keep the tokens.

        Blocks until the reviewer finishes in the browser, gives up, or
        ``timeout`` seconds pass. Run it off the interface thread.
        """
        account = self.account()
        callback = _Callback()
        handler = _handler_for(callback)
        server: HTTPServer | None = None
        ports = [REGISTRATION_PORT] if account is None else []
        for port in ports + [0]:
            try:
                server = HTTPServer(("127.0.0.1", port), handler)
                break
            except OSError:
                continue
        if server is None:  # pragma: no cover - port 0 always binds
            raise SignInError("Could not open a sign-in callback on this computer.")
        server.timeout = 0.5
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}{CALLBACK_PATH}"

        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        url = self.authorization_url(
            redirect_uri=redirect_uri,
            state=state,
            nonce=nonce,
            challenge=challenge,
            account=account,
            id_token=keys.get_secret(_ID) if account else None,
        )
        try:
            self._open_browser(url)
            deadline = self._clock() + timeout
            while not callback.done.is_set():
                if should_stop is not None and should_stop():
                    raise SignInError("Sign-in was cancelled.")
                if self._clock() > deadline:
                    raise SignInError(
                        "Sign-in timed out. Nothing was changed; try again "
                        "when you are ready to sign in in the browser."
                    )
                server.handle_request()
        finally:
            server.server_close()
        return self.complete_sign_in(
            callback.params,
            state=state,
            nonce=nonce,
            verifier=verifier,
            redirect_uri=redirect_uri,
            account=account,
        )

    def complete_sign_in(
        self,
        params: dict[str, str],
        *,
        state: str,
        nonce: str,
        verifier: str,
        redirect_uri: str,
        account: ChatGPTAccount | None,
    ) -> ChatGPTAccount:
        """Check the callback, exchange the code and store the result."""
        if params.get("state") != state:
            raise SignInError(
                "The sign-in reply did not match this request, so it was "
                "ignored. Try signing in again."
            )
        if "error" in params:
            reason = params.get("error_description") or params["error"]
            raise SignInError(f"OpenAI did not complete the sign-in: {reason}")
        code = params.get("code")
        if not code:
            raise SignInError("The sign-in reply had no authorisation code.")
        client_id = (account.client_id if account else "") or params.get("client_id", "")
        if not client_id or client_id == REGISTRATION_CLIENT_ID:
            raise SignInError(
                "OpenAI did not issue a client id for this computer, so the "
                "sign-in cannot be finished."
            )

        tokens = self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "resource": RESOURCE,
            },
            during="sign-in",
        )
        id_token = tokens.get("id_token") or ""
        if not id_token:
            raise SignInError("OpenAI returned no ID token, so the account could not be confirmed.")
        claims = self.verify_id_token(id_token, client_id=client_id, nonce=nonce)
        if account is not None and account.sub and claims.get("sub") != account.sub:
            # A different person signed in under this registration; the
            # documentation keeps registrations per account.
            raise SignInError(
                "A different ChatGPT account signed in than the one this "
                "computer is registered for. Sign out first, then sign in "
                "with the other account."
            )
        scope = tokens.get("scope") or params.get("scope", "")
        new_account = ChatGPTAccount(
            client_id=client_id,
            sub=str(claims.get("sub", "")),
            email=str(claims.get("email", "")),
            scope=" ".join(sorted(scope.replace("+", " ").split())),
            expires_at=self._clock() + float(tokens.get("expires_in", 3600)),
        )
        self._store_tokens(tokens, id_token)
        self._save_account(new_account)
        return new_account

    def _store_tokens(self, tokens: dict[str, Any], id_token: str | None = None) -> None:
        keys.set_secret(_ACCESS, str(tokens["access_token"]))
        if tokens.get("refresh_token"):
            keys.set_secret(_REFRESH, str(tokens["refresh_token"]))
        if id_token:
            keys.set_secret(_ID, id_token)

    def _token_request(self, form: dict[str, str], *, during: str) -> dict[str, Any]:
        httpx = _httpx()
        try:
            response = self.http.post(TOKEN_URL, data=form, timeout=30.0)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach OpenAI during {during}: {exc}") from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            code = str(body.get("error", "")) if isinstance(body, dict) else ""
            error = SignInError(
                f"OpenAI refused the {during}"
                + (f" ({code})" if code else f" (HTTP {response.status_code})")
                + "."
            )
            error.code = code  # type: ignore[attr-defined]
            if response.status_code >= 500:
                transient = ProviderError(str(error))
                transient.code = code  # type: ignore[attr-defined]
                raise transient
            raise error
        if not isinstance(body, dict) or "access_token" not in body:
            raise SignInError(f"OpenAI's reply to the {during} had no access token.")
        return body

    # -- the ID token -------------------------------------------------------

    def _oidc_config(self) -> dict[str, Any]:
        if self._oidc is None:
            response = self.http.get(OIDC_CONFIG_URL, timeout=30.0)
            if response.status_code >= 400:
                raise ProviderError("Could not read OpenAI's sign-in configuration.")
            self._oidc = response.json()
        return self._oidc

    def verify_id_token(self, id_token: str, *, client_id: str, nonce: str) -> dict[str, Any]:
        """Check signature, issuer, audience, expiry and nonce."""
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - bundled in the app
            raise SignInError("PyJWT is needed to confirm the sign-in.") from exc
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise SignInError("OpenAI's ID token could not be read.") from exc
        jwks_uri = self._oidc_config().get("jwks_uri")
        if not jwks_uri:
            raise SignInError("OpenAI's sign-in configuration names no signing keys.")
        response = self.http.get(jwks_uri, timeout=30.0)
        if response.status_code >= 400:
            raise ProviderError("Could not read OpenAI's signing keys.")
        keyset = jwt.PyJWKSet.from_dict(response.json())
        key = next(
            (k for k in keyset.keys if not header.get("kid") or k.key_id == header.get("kid")),
            None,
        )
        if key is None:
            raise SignInError("OpenAI's ID token was signed with a key it does not publish.")
        try:
            claims = jwt.decode(
                id_token,
                key=key.key,
                algorithms=["RS256", "ES256", "PS256"],
                audience=client_id,
                issuer=ISSUER,
                options={"require": ["exp", "iat", "sub"]},
                # A laptop clock a little behind OpenAI's made a token issued
                # seconds ago look "not yet valid". Two minutes either way is
                # the usual allowance; the signature, audience and nonce are
                # still checked exactly.
                leeway=CLOCK_LEEWAY_S,
            )
        except jwt.PyJWTError as exc:
            raise SignInError(f"OpenAI's ID token did not verify: {exc}") from exc
        if claims.get("nonce") != nonce:
            raise SignInError("OpenAI's ID token was not issued for this sign-in.")
        return claims

    # -- using the tokens ----------------------------------------------------

    def access_token(self, *, force_refresh: bool = False) -> str:
        """A valid access token, refreshed when it is about to run out."""
        account = self.account()
        if account is None or not keys.get_secret(_REFRESH):
            raise AuthenticationError(
                "Not signed in to ChatGPT. Sign in under Tools → Model settings."
            )
        if not account.plan_enabled:
            raise AuthenticationError(
                "This ChatGPT sign-in did not allow plan use. Sign in again "
                "under Tools → Model settings and allow it, or choose another "
                "model."
            )
        token = keys.get_secret(_ACCESS)
        if token and not force_refresh and account.expires_at - self._clock() > REFRESH_MARGIN_S:
            return token
        with _refresh_lock:
            # Another thread may have refreshed while this one waited.
            account = self.account() or account
            token = keys.get_secret(_ACCESS)
            if token and not force_refresh and account.expires_at - self._clock() > REFRESH_MARGIN_S:
                return token
            return self._refresh(account)

    def _refresh(self, account: ChatGPTAccount) -> str:
        refresh_token = keys.get_secret(_REFRESH)
        if not refresh_token:
            raise AuthenticationError("Not signed in to ChatGPT.")
        try:
            tokens = self._token_request(
                {
                    "grant_type": "refresh_token",
                    "client_id": account.client_id,
                    "refresh_token": refresh_token,
                    "resource": RESOURCE,
                },
                during="token refresh",
            )
        except SignInError as exc:
            if getattr(exc, "code", "") in _TERMINAL_REFRESH or getattr(exc, "code", "") == "":
                # Unusable: forget the tokens, keep the registration.
                self._clear_tokens()
                raise AuthenticationError(
                    "The ChatGPT sign-in has expired or was disconnected. "
                    "Sign in again under Tools → Model settings."
                ) from exc
            raise
        # Replace everything together: the old refresh token is now dead.
        self._store_tokens(tokens, tokens.get("id_token"))
        scope = tokens.get("scope")
        account.expires_at = self._clock() + float(tokens.get("expires_in", 3600))
        if scope:
            account.scope = " ".join(sorted(scope.split()))
        self._save_account(account)
        return str(tokens["access_token"])

    def _clear_tokens(self) -> None:
        for name in (_ACCESS, _REFRESH, _ID):
            keys.delete_secret(name)

    def sign_out(self) -> bool:
        """Revoke the session at OpenAI and forget the tokens.

        Returns whether OpenAI confirmed the revocation. The registration and
        this computer's host id are kept, so signing in again later does not
        register the application a second time.
        """
        account = self.account()
        refresh_token = keys.get_secret(_REFRESH)
        confirmed = False
        if account and refresh_token:
            for attempt in range(3):
                try:
                    endpoint = self._oidc_config().get("revocation_endpoint")
                    if not endpoint:
                        break
                    response = self.http.post(
                        endpoint,
                        data={
                            "token": refresh_token,
                            "token_type_hint": "refresh_token",
                            "client_id": account.client_id,
                        },
                        timeout=30.0,
                    )
                    if response.status_code == 200:
                        confirmed = True
                        break
                    if response.status_code < 500:
                        break
                except Exception:
                    pass
                self._sleep(min(2 ** attempt, 4))
        self._clear_tokens()
        if account:
            account.scope = ""
            account.expires_at = 0.0
            self._save_account(account)
        return confirmed

    # -- models ------------------------------------------------------------

    def list_models(self) -> list[dict[str, str]]:
        """The models this account may use, in OpenAI's order."""
        token = self.access_token()
        response = self.http.get(
            f"{RESOURCE}/models",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        if response.status_code in (401, 403):
            raise AuthenticationError("ChatGPT did not accept the sign-in when listing models.")
        if response.status_code >= 400:
            raise ProviderError(f"Could not list ChatGPT models (HTTP {response.status_code}).")
        data = response.json()
        rows = data.get("models") if isinstance(data, dict) else None
        if rows is None and isinstance(data, dict):
            rows = data.get("data", [])
        out = []
        for row in rows or []:
            slug = row.get("slug") or row.get("id")
            if not slug:
                continue
            if row.get("visibility", "list") != "list":
                continue
            out.append({"slug": slug, "display_name": row.get("display_name") or slug})
        return out


# -- the provider --------------------------------------------------------------


def _error_from_body(body: Any) -> tuple[str, str, str]:
    """(code, message, param) from an error body in any of its shapes."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return (
                str(error.get("code") or error.get("type") or ""),
                str(error.get("message") or ""),
                str(error.get("param") or ""),
            )
        if "detail" in body:
            return "", str(body["detail"]), ""
    return "", "", ""


def _raise_for(status: int, code: str, message: str, param: str):
    """Map OpenAI's documented plan-use errors onto the shared error types."""
    if code == "subscription_sharing_usage_limit_exceeded":
        raise PlanLimitReached(
            "Your ChatGPT plan's usage limit for this application has been "
            "reached. The run has paused and everything already decided is "
            f"kept. See how much is left at {USAGE_PAGE}, and resume the run "
            "when the allowance is back."
        )
    if code == "subscription_sharing_user_not_eligible":
        raise AuthenticationError(
            "OpenAI does not allow this ChatGPT account to be used by "
            "applications (its plan or a workspace policy rules it out). "
            "Choose another option under Tools → Model settings: the free "
            "Ollama option costs nothing."
        )
    if code in ("subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"):
        raise ProviderError(
            "ChatGPT could not check your plan's usage just now. It will be "
            "tried again shortly."
        )
    if code == "subscription_sharing_unsupported_capability":
        error = ProviderError(
            "ChatGPT plan use does not support part of this request"
            + (f" ({param})" if param else "") + "."
        )
        error.retryable = False
        error.param = param  # type: ignore[attr-defined]
        raise error
    if status == 401:
        raise AuthenticationError(
            "ChatGPT did not accept the sign-in. Sign in again under "
            "Tools → Model settings."
        )
    if status == 403:
        raise AuthenticationError(
            "ChatGPT refused this request"
            + (f": {message}" if message else ".")
        )
    if status == 429:
        raise RateLimitError("ChatGPT asked the application to slow down.")
    if status >= 500:
        raise ProviderError(f"ChatGPT returned {status}" + (f": {message}" if message else "."))
    error = ProviderError(
        "ChatGPT rejected the request" + (f": {message}" if message else f" (HTTP {status}).")
    )
    error.retryable = False
    raise error


class ChatGPTSubscriptionProvider(Provider):
    """A model reached through the reviewer's ChatGPT plan.

    The plan route accepts less than the paid API: no temperature, no output
    limit and no system-role message. The instructions go in the Responses
    API's ``instructions`` field, unchanged; the model picks its own
    temperature, and the audit log records that none was sent.
    """

    name = "chatgpt_subscription"
    sends_data_off_device = True
    sends_temperature = False
    billing = "plan"

    def __init__(
        self,
        model: str,
        *,
        session: ChatGPTSession | None = None,
        base_url: str = RESOURCE,
        # Off by default: the plan route refuses JSON mode unless the word
        # "json" is in the input itself, and the instructions do not count.
        # The answer is parsed and validated as JSON either way.
        json_mode: bool = False,
        **options: Any,
    ) -> None:
        super().__init__(model, **options)
        self.session = session or ChatGPTSession()
        self.base_url = (base_url or RESOURCE).rstrip("/")
        self.json_mode = json_mode

    def _payload(self, system: str, user: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": [{"role": "user", "content": user}],
            "store": False,
            "stream": True,
        }
        if self.json_mode:
            payload["text"] = {"format": {"type": "json_object"}}
        return payload

    def complete(self, *, system: str, user: str, max_tokens: int = 1024,
                 temperature: float = 0.0, timeout: float = 300.0) -> LLMResponse:
        # max_tokens and temperature are accepted for the interface and not
        # sent: the plan route refuses both.
        if not self.model:
            raise ProviderError(
                "No ChatGPT model is chosen. Pick one under Tools → Model settings."
            )
        refreshed = False
        for _ in range(3):
            try:
                return self._stream_once(system, user, timeout)
            except AuthenticationError as exc:
                if refreshed or isinstance(exc, SignInError) or "did not accept the sign-in" not in str(exc):
                    raise
                # An access token can be revoked before it expires; one
                # refresh is worth trying before asking for a new sign-in.
                self.session.access_token(force_refresh=True)
                refreshed = True
            except ProviderError as exc:
                param = getattr(exc, "param", "")
                message = str(exc)
                # Two ways OpenAI declines JSON mode: as an unsupported
                # capability, or because the input does not contain the word
                # "json" (it checks the input, not the instructions).
                refused_json = param.startswith("text") or (
                    "json" in message.lower()
                    and ("text.format" in message or "json_object" in message)
                )
                if self.json_mode and refused_json:
                    # The answer is parsed and validated as JSON either way.
                    self.json_mode = False
                    continue
                raise
        raise ProviderError("ChatGPT could not answer this request.")

    def _stream_once(self, system: str, user: str, timeout: float) -> LLMResponse:
        httpx = _httpx()
        token = self.session.access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        url = f"{self.base_url}/responses"
        text_parts: list[str] = []
        final: dict[str, Any] | None = None
        incomplete: dict[str, Any] | None = None
        try:
            with self.session.http.stream(
                "POST", url, headers=headers, json=self._payload(system, user), timeout=timeout
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    try:
                        body = response.json()
                    except ValueError:
                        body = {}
                    code, message, param = _error_from_body(body)
                    _raise_for(response.status_code, code, message, param)
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    kind = event.get("type", "")
                    if kind == "response.output_text.delta":
                        text_parts.append(str(event.get("delta", "")))
                    elif kind == "response.completed":
                        final = event.get("response") or {}
                        break
                    elif kind == "response.incomplete":
                        incomplete = event.get("response") or {}
                        break
                    elif kind in ("response.failed", "error"):
                        failed = event.get("response") or {}
                        error = failed.get("error") or event.get("error") or event
                        code, message, param = _error_from_body({"error": error})
                        status = 429 if "limit" in code else 400
                        _raise_for(status, code, message, param)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"ChatGPT did not answer within {timeout:.0f}s.") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach ChatGPT: {exc}") from exc

        done = final if final is not None else incomplete
        if done is None:
            raise ProviderError("ChatGPT's answer was interrupted before it finished.")
        usage = done.get("usage") or {}
        stop_reason = "end_turn"
        if incomplete is not None:
            reason = (incomplete.get("incomplete_details") or {}).get("reason", "")
            stop_reason = "length" if reason == "max_output_tokens" else (reason or "incomplete")
        model = str(done.get("model") or self.model)
        return LLMResponse(
            text="".join(text_parts),
            model=model,
            model_version=model,
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens", 0) or 0),
                completion_tokens=int(usage.get("output_tokens", 0) or 0),
            ),
            stop_reason=stop_reason,
            raw={"id": done.get("id", ""), "status": done.get("status", "")},
        )

    def check(self) -> tuple[bool, str]:
        ok, message = super().check()
        if ok:
            message += " Calls count against your ChatGPT plan's usage limits."
        return ok, message
