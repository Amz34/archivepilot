"""Microsoft Graph transport + app-only auth (stdlib only, no dependencies).

This module owns *HTTP and Graph semantics only*:

  * token acquisition/caching — POST {login}/{tenant}/oauth2/v2.0/token
    with scope ``https://graph.microsoft.com/.default``
  * retry policy — ``429`` honours ``Retry-After``, ``5xx`` gets bounded
    exponential backoff
  * paging — follows ``@odata.nextLink`` (and refuses links pointing off
    the configured Graph host, so a hostile/incorrect link can't make us
    send a bearer token to a third party)
  * thin typed helpers for SharePoint/OneDrive drive items and Teams
    channel messages

Mapping Graph entities onto ArchivePilot's indexed record lives in
``archivepilot.ingest`` — that keeps raw Graph JSON out of the index and
keeps this file swappable in tests.

Credentials are read from the environment ONLY:

    MSGRAPH_TENANT_ID, MSGRAPH_CLIENT_ID, MSGRAPH_CLIENT_SECRET

They are never written to disk and never logged: ``GraphCredentials``
masks the secret in its repr and error messages carry only Graph's
``error``/``error_description`` fields.

TESTING: the single network seam is :meth:`GraphClient._request`. Tests
replace ``client._request`` with a stub, so the suite never opens a
socket.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator, Mapping, NamedTuple

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

ENV_TENANT_ID = "MSGRAPH_TENANT_ID"
ENV_CLIENT_ID = "MSGRAPH_CLIENT_ID"
ENV_CLIENT_SECRET = "MSGRAPH_CLIENT_SECRET"

SETUP_HINT = (
    "Register an app in Microsoft Entra ID (Azure AD), add the Microsoft "
    "Graph *application* permissions Files.Read.All, Sites.Read.All and "
    "ChannelMessage.Read.All, grant admin consent, then export "
    f"{ENV_TENANT_ID}, {ENV_CLIENT_ID} and {ENV_CLIENT_SECRET} in the "
    "environment. Credentials are read from the environment only — never "
    "stored on disk, never logged. Docs: "
    "https://learn.microsoft.com/graph/auth-v2-service"
)

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_BACKOFF = 30.0
# Refresh the token a little early so a request never races the expiry.
TOKEN_SKEW_SECONDS = 60.0
MAX_PAGES = 10_000
MAX_FOLDER_DEPTH = 5
ERROR_DETAIL_CHARS = 300


class GraphError(RuntimeError):
    """A Graph/transport failure. ``exit_code`` is used by the CLI."""

    exit_code = 3


class MissingCredentials(GraphError):
    """Required MSGRAPH_* environment variables are absent."""

    exit_code = 2


class HttpResponse(NamedTuple):
    """A completed HTTP exchange — what the transport seam hands back."""

    status: int
    headers: dict
    body: bytes

    def header(self, name: str, default: str | None = None) -> str | None:
        """Case-insensitive header lookup."""
        return self.headers.get(name.lower(), default)

    def json(self) -> dict:
        """Parsed body, or ``{}`` when the body is empty/not JSON."""
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


class DriveItemRef(NamedTuple):
    """A text-extractable drive item plus its folder path within the drive."""

    item: dict
    path: str


@dataclass(frozen=True)
class GraphCredentials:
    """App-only (client credentials) credentials, sourced from the env."""

    tenant_id: str
    client_id: str
    client_secret: str

    def __repr__(self) -> str:  # pragma: no cover - trivial, but leak-critical
        return (
            f"GraphCredentials(tenant_id={self.tenant_id!r}, "
            f"client_id={self.client_id!r}, client_secret='***')"
        )

    __str__ = __repr__

    @property
    def token_url(self) -> str:
        return (
            f"{LOGIN_BASE}/{urllib.parse.quote(self.tenant_id, safe='')}"
            "/oauth2/v2.0/token"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GraphCredentials":
        """Build credentials from the environment, or explain how to fix it."""
        env = os.environ if env is None else env
        values: dict[str, str] = {}
        missing: list[str] = []
        for name in (ENV_TENANT_ID, ENV_CLIENT_ID, ENV_CLIENT_SECRET):
            value = str(env.get(name) or "").strip()
            values[name] = value
            if not value:
                missing.append(name)
        if missing:
            raise MissingCredentials(
                "missing Microsoft Graph credentials: " + ", ".join(missing)
                + ". " + SETUP_HINT
            )
        return cls(
            tenant_id=values[ENV_TENANT_ID],
            client_id=values[ENV_CLIENT_ID],
            client_secret=values[ENV_CLIENT_SECRET],
        )


class GraphClient:
    """Minimal Microsoft Graph client: auth, retries, paging, entities.

    Injectable ``sleep``/``clock`` keep the retry and cache logic testable
    without real time passing.
    """

    def __init__(
        self,
        credentials: GraphCredentials,
        *,
        base_url: str = GRAPH_BASE,
        scope: str = GRAPH_SCOPE,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        max_pages: int = MAX_PAGES,
        max_folder_depth: int = MAX_FOLDER_DEPTH,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.credentials = credentials
        self.base_url = base_url.rstrip("/")
        self.scope = scope
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_pages = max_pages
        self.max_folder_depth = max_folder_depth
        self._sleep = sleep
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._allowed_host = urllib.parse.urlsplit(self.base_url).netloc.lower()

    # ------------------------------------------------------------------
    # transport seam — the only method that touches the network.
    # Tests assign ``client._request = stub`` (see tests/test_graph_ingest.py).
    # ------------------------------------------------------------------
    def _request(self, method: str, url: str, *, headers: dict | None = None,
                 data: bytes | None = None, timeout: float | None = None) -> HttpResponse:
        request = urllib.request.Request(
            url, data=data, method=method, headers=dict(headers or {})
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
                return HttpResponse(
                    int(resp.status), _lower_headers(resp.headers), resp.read()
                )
        except urllib.error.HTTPError as exc:
            # Error statuses are normal control flow here (429/5xx retries).
            return HttpResponse(int(exc.code), _lower_headers(exc.headers), exc.read())
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise GraphError(f"network error calling {url}: {reason}") from exc

    # ------------------------------------------------------------------
    # retries
    # ------------------------------------------------------------------
    def _backoff_seconds(self, response: HttpResponse, attempt: int) -> float:
        """``Retry-After`` for 429, bounded exponential backoff otherwise."""
        if response.status == 429:
            retry_after = response.header("retry-after")
            if retry_after:
                try:
                    return max(0.0, float(str(retry_after).strip()))
                except ValueError:
                    pass  # HTTP-date form — fall back to backoff
        return min(2.0 ** attempt, MAX_BACKOFF)

    def _send(self, method: str, url: str, *, headers: dict | None = None,
              data: bytes | None = None) -> HttpResponse:
        """One logical request: retry 429/5xx, return the final response."""
        attempt = 0
        while True:
            response = self._request(
                method, url, headers=headers, data=data, timeout=self.timeout
            )
            if response.status in RETRY_STATUSES and attempt < self.max_retries:
                self._sleep(self._backoff_seconds(response, attempt))
                attempt += 1
                continue
            return response

    @staticmethod
    def _describe_error(response: HttpResponse, context: str) -> str:
        """Graph/AAD error text — never includes credentials."""
        payload = response.json()
        error = payload.get("error")
        detail = ""
        code = error
        if isinstance(error, dict):  # Graph envelope: {"error": {"code", "message"}}
            code = error.get("code") or error.get("message")
            detail = error.get("message") or ""
        elif isinstance(payload.get("error_description"), str):
            detail = payload["error_description"]
        parts = [f"{context}: HTTP {response.status}"]
        if code:
            parts.append(str(code))
        detail = " ".join(str(detail).split())[:ERROR_DETAIL_CHARS]
        if detail:
            parts.append(f"- {detail}")
        return " ".join(parts)

    def _json(self, method: str, url: str, *, headers: dict | None = None,
              data: bytes | None = None, context: str | None = None) -> dict:
        response = self._send(method, url, headers=headers, data=data)
        if response.status >= 400:
            raise GraphError(self._describe_error(response, context or f"{method} {url}"))
        return response.json()

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------
    def token(self, *, force: bool = False) -> str:
        """Cached bearer token; refreshed once it is within the skew window."""
        now = self._clock()
        if not force and self._token and now < self._token_expires_at:
            return self._token
        payload = self._fetch_token()
        access_token = payload.get("access_token")
        if not access_token:
            raise GraphError("token endpoint returned no access_token")
        try:
            ttl = max(0.0, float(payload.get("expires_in", 3599)) - TOKEN_SKEW_SECONDS)
        except (TypeError, ValueError):
            ttl = 3599.0 - TOKEN_SKEW_SECONDS
        self._token = str(access_token)
        self._token_expires_at = now + ttl
        return self._token

    def _fetch_token(self) -> dict:
        creds = self.credentials
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scope": self.scope,
        }).encode("utf-8")
        return self._json(
            "POST",
            creds.token_url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data=body,
            context="Microsoft identity platform token request",
        )

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}

    # ------------------------------------------------------------------
    # paging
    # ------------------------------------------------------------------
    def _next_link(self, page: dict) -> str | None:
        link = page.get("@odata.nextLink")
        if not link:
            return None
        if not isinstance(link, str):
            raise GraphError("@odata.nextLink was not a string")
        host = urllib.parse.urlsplit(link).netloc.lower()
        if host != self._allowed_host:
            raise GraphError(
                f"refusing to follow @odata.nextLink to unexpected host {host!r}"
            )
        return link

    def iter_pages(self, url: str, *, params: dict | None = None,
                   context: str | None = None) -> Iterator[dict]:
        """Yield each response page, following ``@odata.nextLink``."""
        next_url = _url_with_params(url, params)
        pages = 0
        while next_url:
            pages += 1
            if pages > self.max_pages:
                raise GraphError(f"pagination exceeded {self.max_pages} pages at {url}")
            page = self._json(
                "GET", next_url, headers=self._auth_headers(),
                context=context or f"GET {url}",
            )
            yield page
            next_url = self._next_link(page)

    def iter_collection(self, url: str, *, params: dict | None = None,
                        key: str = "value", limit: int | None = None,
                        context: str | None = None) -> Iterator[dict]:
        """Yield every item of a paged Graph collection."""
        emitted = 0
        for page in self.iter_pages(url, params=params, context=context):
            for item in page.get(key) or []:
                if not isinstance(item, dict):
                    continue
                yield item
                emitted += 1
                if limit is not None and emitted >= limit:
                    return

    # ------------------------------------------------------------------
    # SharePoint / OneDrive
    # ------------------------------------------------------------------
    def get_site(self, site_id: str = "root") -> dict:
        """``/sites/root`` or ``/sites/{id}``."""
        return self._json(
            "GET", f"{self.base_url}/sites/{_quote(site_id)}",
            headers=self._auth_headers(), context=f"GET /sites/{site_id}",
        )

    def list_sites(self, *, limit: int | None = None) -> Iterator[dict]:
        return self.iter_collection(f"{self.base_url}/sites", limit=limit)

    def list_drives(self, site_id: str, *, limit: int | None = None) -> Iterator[dict]:
        return self.iter_collection(
            f"{self.base_url}/sites/{_quote(site_id)}/drives", limit=limit,
            context=f"GET /sites/{site_id}/drives",
        )

    def get_drive(self, drive_id: str) -> dict:
        return self._json(
            "GET", f"{self.base_url}/drives/{_quote(drive_id)}",
            headers=self._auth_headers(), context=f"GET /drives/{drive_id}",
        )

    def list_children(self, drive_id: str, item_id: str = "root",
                      *, limit: int | None = None) -> Iterator[dict]:
        return self.iter_collection(
            f"{self.base_url}/drives/{_quote(drive_id)}/items/{_quote(item_id)}/children",
            limit=limit, context=f"GET /drives/{drive_id}/items/{item_id}/children",
        )

    def walk_drive(self, drive_id: str, item_id: str = "root", *,
                   max_depth: int | None = None,
                   limit: int | None = None) -> Iterator[DriveItemRef]:
        """Breadth-first walk of a drive, yielding files (not folders).

        Folders are descended up to ``max_depth``; ``seen`` guards against
        cycles/shortcuts. ``limit`` caps how many files are yielded.
        """
        depth_limit = self.max_folder_depth if max_depth is None else max_depth
        queue: list[tuple[str, str, int]] = [(item_id, "", 0)]
        seen: set[str] = set()
        emitted = 0
        while queue:
            current, prefix, depth = queue.pop(0)
            if not current or current in seen:
                continue
            seen.add(current)
            for child in self.list_children(drive_id, current):
                name = (child.get("name") or "").strip()
                child_id = child.get("id") or ""
                if "folder" in child:
                    if depth >= depth_limit or not child_id:
                        continue
                    child_path = f"{prefix}/{name}" if prefix else name
                    queue.append((child_id, child_path, depth + 1))
                    continue
                if "file" not in child or not child_id:
                    continue
                yield DriveItemRef(child, prefix)
                emitted += 1
                if limit is not None and emitted >= limit:
                    return

    def download(self, drive_id: str, item_id: str) -> bytes:
        """Fetch the raw bytes of a drive item (``/content``)."""
        url = f"{self.base_url}/drives/{_quote(drive_id)}/items/{_quote(item_id)}/content"
        response = self._send("GET", url, headers=self._auth_headers())
        if response.status >= 400:
            raise GraphError(self._describe_error(response, f"download {item_id}"))
        return response.body

    # ------------------------------------------------------------------
    # Teams
    # ------------------------------------------------------------------
    def get_team(self, team_id: str) -> dict:
        return self._json(
            "GET", f"{self.base_url}/teams/{_quote(team_id)}",
            headers=self._auth_headers(), context=f"GET /teams/{team_id}",
        )

    def list_teams(self, *, limit: int | None = None) -> Iterator[dict]:
        return self.iter_collection(f"{self.base_url}/teams", limit=limit)

    def list_channels(self, team_id: str, *, limit: int | None = None) -> Iterator[dict]:
        return self.iter_collection(
            f"{self.base_url}/teams/{_quote(team_id)}/channels", limit=limit,
            context=f"GET /teams/{team_id}/channels",
        )

    def iter_channel_messages(self, team_id: str, channel_id: str, *,
                              limit: int | None = None) -> Iterator[dict]:
        """Channel messages, paged across ``@odata.nextLink``."""
        return self.iter_collection(
            f"{self.base_url}/teams/{_quote(team_id)}/channels/{_quote(channel_id)}/messages",
            limit=limit,
            context=f"GET /teams/{team_id}/channels/{channel_id}/messages",
        )

    def iter_message_replies(self, team_id: str, channel_id: str, message_id: str,
                             *, limit: int | None = None) -> Iterator[dict]:
        """Replies in a message thread, paged."""
        return self.iter_collection(
            f"{self.base_url}/teams/{_quote(team_id)}/channels/{_quote(channel_id)}"
            f"/messages/{_quote(message_id)}/replies",
            limit=limit,
            context=f"GET /teams/{team_id}/messages/{message_id}/replies",
        )

    def iter_drive_delta(self, drive_id: str, delta_link: str | None = None, *,
                         limit: int | None = None) -> Iterator[dict]:
        """Incremental drive changes (``/root/delta``).

        Pass the ``@odata.deltaLink`` from a previous run to get only what
        changed since. Paging is identical to the children endpoints.
        """
        url = delta_link or f"{self.base_url}/drives/{_quote(drive_id)}/root/delta"
        return self.iter_collection(url, limit=limit, context=f"GET {url}")


def client_from_env(env: Mapping[str, str] | None = None, **kwargs) -> GraphClient:
    """Convenience factory: credentials from the environment -> client."""
    return GraphClient(GraphCredentials.from_env(env), **kwargs)


def _quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def _lower_headers(headers) -> dict:
    if headers is None:
        return {}
    try:
        items = headers.items()
    except AttributeError:
        return {str(k).lower(): str(v) for k, v in dict(headers).items()}
    return {str(k).lower(): str(v) for k, v in items}


def _url_with_params(url: str, params: dict | None) -> str:
    if not params:
        return url
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    for key, value in params.items():
        if value is not None:
            query.append((str(key), str(value)))
    return urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query))
    )
