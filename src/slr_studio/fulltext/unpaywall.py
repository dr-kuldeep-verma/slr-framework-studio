"""Finding legally readable copies, through Unpaywall.

Unpaywall is an index of open-access locations keyed by DOI. It is queried
with the reviewer's own email address, which is what its terms require.

What this module does **not** do is as important as what it does. It returns
links. It never downloads anything, never sends institutional credentials,
never solves a CAPTCHA and never scrapes a publisher's site. The reviewer
follows a link, decides whether they may read it, and saves the file
themselves. A tool that fetched PDFs on a researcher's behalf would be making
a licensing judgement it is in no position to make.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

__all__ = [
    "UnpaywallClient",
    "OaResult",
    "OaLocation",
    "UnpaywallError",
    "doi_url",
]

API = "https://api.unpaywall.org/v2/{doi}"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UnpaywallError(RuntimeError):
    pass


class Fetcher(Protocol):
    """Anything that can turn a URL into parsed JSON.

    Injectable so the tests never touch the network, and so a reviewer behind
    a proxy can be given a different one without this module changing.
    """

    def __call__(self, url: str, *, timeout: float) -> dict[str, Any]: ...


@dataclass
class OaLocation:
    url: str
    host_type: str = ""
    version: str = ""
    license: str = ""
    is_best: bool = False


@dataclass
class OaResult:
    doi: str
    is_oa: bool = False
    oa_status: str = ""
    locations: list[OaLocation] = field(default_factory=list)
    title: str = ""
    error: str = ""

    @property
    def best(self) -> OaLocation | None:
        for location in self.locations:
            if location.is_best:
                return location
        return self.locations[0] if self.locations else None


def doi_url(doi: str) -> str:
    """The resolver link, which works whether or not a free copy exists."""
    return f"https://doi.org/{doi}" if doi else ""


def _httpx_fetch(url: str, *, timeout: float) -> dict[str, Any]:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - bundled in the app
        raise UnpaywallError("The httpx package is required to query Unpaywall.") from exc
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise UnpaywallError(f"Could not reach Unpaywall: {exc}") from exc
    if response.status_code == 404:
        # Unpaywall answers 404 for a DOI it has never heard of. That is an
        # answer, not a failure: the record simply has no entry.
        return {"doi": "", "is_oa": False, "oa_locations": [], "_not_found": True}
    if response.status_code == 422:
        raise UnpaywallError("Unpaywall rejected the DOI as malformed.")
    if response.status_code == 429:
        raise UnpaywallError(
            "Unpaywall is rate limiting these requests. Wait a few minutes and "
            "continue; everything already looked up is saved."
        )
    if response.status_code >= 400:
        raise UnpaywallError(f"Unpaywall returned {response.status_code}.")
    try:
        return response.json()
    except ValueError as exc:
        raise UnpaywallError("Unpaywall returned something that was not JSON.") from exc


class UnpaywallClient:
    def __init__(
        self,
        email: str,
        *,
        fetch: Fetcher | Callable[..., dict[str, Any]] | None = None,
        timeout: float = 20.0,
    ) -> None:
        email = (email or "").strip()
        if not _EMAIL_RE.match(email):
            raise UnpaywallError(
                "Unpaywall requires your email address, which it uses to "
                "identify polite API users. Add one on the Project page."
            )
        self.email = email
        self._fetch = fetch or _httpx_fetch
        self.timeout = timeout

    def lookup(self, doi: str) -> OaResult:
        """Look up one DOI. Network problems are returned, not raised.

        A retrieval pass over a thousand records should not stop because one
        lookup timed out; the failure is recorded against that record and the
        pass continues.
        """
        doi = (doi or "").strip().casefold()
        if not doi:
            return OaResult(doi="", error="the record has no DOI")
        url = API.format(doi=doi) + f"?email={self.email}"
        try:
            payload = self._fetch(url, timeout=self.timeout)
        except UnpaywallError as exc:
            return OaResult(doi=doi, error=str(exc))
        except Exception as exc:
            return OaResult(doi=doi, error=f"Unexpected problem: {exc}")
        return self.parse(doi, payload)

    @staticmethod
    def parse(doi: str, payload: dict[str, Any]) -> OaResult:
        if payload.get("_not_found"):
            return OaResult(doi=doi, error="Unpaywall has no entry for this DOI")
        result = OaResult(
            doi=str(payload.get("doi") or doi),
            is_oa=bool(payload.get("is_oa")),
            oa_status=str(payload.get("oa_status") or ""),
            title=str(payload.get("title") or ""),
        )
        best = payload.get("best_oa_location") or {}
        best_url = str(best.get("url") or best.get("url_for_pdf") or "")

        seen: set[str] = set()
        for raw in payload.get("oa_locations") or []:
            url = str(raw.get("url_for_pdf") or raw.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            result.locations.append(
                OaLocation(
                    url=url,
                    host_type=str(raw.get("host_type") or ""),
                    version=str(raw.get("version") or ""),
                    license=str(raw.get("license") or ""),
                    is_best=url in (best_url, str(best.get("url_for_pdf") or "")),
                )
            )
        if best_url and best_url not in seen:
            result.locations.insert(
                0,
                OaLocation(
                    url=best_url,
                    host_type=str(best.get("host_type") or ""),
                    version=str(best.get("version") or ""),
                    license=str(best.get("license") or ""),
                    is_best=True,
                ),
            )
        if result.locations and not any(loc.is_best for loc in result.locations):
            result.locations[0].is_best = True
        return result
