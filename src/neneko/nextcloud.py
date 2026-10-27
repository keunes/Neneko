"""Client for the Nextcloud News external API, v1-3."""

import logging
import time

import httpx

from .scope import ALL, FEED, FOLDER, Scope

log = logging.getLogger(__name__)

TYPE_FEED = 0
TYPE_FOLDER = 1
TYPE_ALL = 3
SOURCE_TYPES = {ALL: TYPE_ALL, FOLDER: TYPE_FOLDER, FEED: TYPE_FEED}


def modified_at(item: dict) -> float:
    """An item's lastModified as a Unix time (older News versions send seconds, newer microseconds)."""
    try:
        value = float(item.get("lastModified") or 0)
    except (TypeError, ValueError):
        return 0.0
    return value / 1_000_000 if value > 100_000_000_000 else value


class NextcloudError(Exception):
    """kind: dns, connect, timeout, auth, untrusted_domain, not_found, http or bad_response."""

    def __init__(self, message: str, kind: str = "http"):
        super().__init__(message)
        self.kind = kind


HINTS = {
    "dns": "The host name in NENEKO_NC_URL can't be resolved from the container. Is NENEKO_NC_URL set, "
           "and if it's a container name, is Neneko on the same Docker network?",
    "connect": "Nothing answers at NENEKO_NC_URL. Check the address and port, and that Nextcloud is running.",
    "timeout": "Nextcloud didn't answer in time. Check that it's running and reachable from the container.",
    "auth": "Nextcloud rejected the login. NENEKO_NC_USER must be the login name shown next to the app "
            "password, and NENEKO_NC_APP_PASSWORD the app password itself.",
    "untrusted_domain": "Nextcloud doesn't accept this address. Add the host name from NENEKO_NC_URL to "
                        "'trusted_domains' in Nextcloud's config.php, or use the public address.",
    "not_found": "The News API wasn't found. Is the News app installed and enabled, and does NENEKO_NC_URL "
                 "point at the Nextcloud root (without /index.php)?",
    "http": "Nextcloud returned an unexpected error; see its log.",
    "bad_response": "The answer wasn't the News API's JSON. Does NENEKO_NC_URL point at Nextcloud?",
}


def _error_kind(exc: httpx.HTTPError) -> str:
    text = repr(exc).lower()
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError) and any(
            marker in text for marker in ("name or service not known", "no address associated",
                                          "nodename nor servname", "temporary failure in name resolution",
                                          "getaddrinfo")):
        return "dns"
    if isinstance(exc, (httpx.ConnectError, httpx.ProxyError, httpx.UnsupportedProtocol)):
        return "connect"
    return "http"


def _status_kind(response: httpx.Response) -> str:
    if response.status_code == 401:
        return "auth"
    if response.status_code == 400 and "trusted" in response.text.lower():
        return "untrusted_domain"
    if response.status_code == 404:
        return "not_found"
    return "http"


class NewsClient:
    def __init__(self, base_url: str, user: str, password: str, *,
                 transport: httpx.AsyncBaseTransport | None = None, cache_seconds: float = 3600):
        self._client = httpx.AsyncClient(
            base_url=f"{base_url}/index.php/apps/news/api/v1-3",
            auth=(user, password),
            timeout=httpx.Timeout(20, connect=5),
            headers={"Accept": "application/json"},
            transport=transport,
        )
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, object]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, timeout: float | None = None, **kwargs):
        try:
            response = await self._client.request(
                method, path, timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise NextcloudError(f"{method} {path}: {exc!r}", _error_kind(exc)) from exc
        if response.status_code >= 400:
            raise NextcloudError(f"{method} {path}: HTTP {response.status_code} {response.text[:200]!r}",
                                 _status_kind(response))
        if not response.content:
            return None
        try:
            data = response.json()
        except ValueError as exc:
            raise NextcloudError(f"{method} {path}: not JSON: {response.text[:200]!r}", "bad_response") from exc
        if isinstance(data, dict) and set(data) == {"message"}:
            raise NextcloudError(f"{method} {path}: {data['message'] or 'empty error message'}")
        return data

    async def _cached(self, key: str, path: str):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self._cache_seconds:
            return hit[1]
        data = await self._request("GET", path)
        self._cache[key] = (time.monotonic(), data)
        return data

    def invalidate_cache(self) -> None:
        self._cache.clear()

    async def folders(self) -> list[dict]:
        return (await self._cached("folders", "/folders"))["folders"]

    async def feeds(self) -> list[dict]:
        return (await self._cached("feeds", "/feeds"))["feeds"]

    async def check(self, **rules: list[str]) -> tuple[bool, str, str]:
        """Can we read News, and do the include/exclude rules match? Returns (ok, summary, hint);
        never raises."""
        try:
            folders = (await self._request("GET", "/folders"))["folders"]
            feeds = (await self._request("GET", "/feeds"))["feeds"]
            folder_names = {folder["id"]: folder["name"] for folder in folders}
            scope = Scope(folder_names, {feed["id"]: feed for feed in feeds}, **rules)
        except NextcloudError as exc:
            return False, str(exc), HINTS.get(exc.kind, HINTS["http"])
        except (KeyError, TypeError) as exc:
            return False, f"unexpected answer: {exc!r}", HINTS["bad_response"]
        summary = f"{len(folders)} folders, {len(feeds)} feeds; {scope.describe()}"
        unknown = scope.unknown_names()
        if unknown:
            return False, f"{summary}; not found: {', '.join(unknown)}", (
                "Use the names exactly as Nextcloud News shows them (case doesn't matter), or a "
                f"feed's ID. Existing folders: {', '.join(folder_names.values()) or 'none'}.")
        return True, summary, ""

    async def unread_items(self, *, source: tuple[str, int], batch_size: int,
                           offset: int | None = None) -> list[dict]:
        """Newest unread items (by arrival) of a scope source (everything, a folder or a feed),
        older than item `offset` if given."""
        kind, ident = source
        params = {
            "type": SOURCE_TYPES[kind],
            "id": ident,
            "getRead": "false",
            "batchSize": batch_size,
            "oldestFirst": "false",
        }
        if offset is not None:
            params["offset"] = offset
        return (await self._request("GET", "/items", params=params))["items"]

    async def items_updated_since(self, last_modified: int) -> list[dict]:
        params = {"type": TYPE_ALL, "id": 0, "lastModified": last_modified}
        return (await self._request("GET", "/items/updated", params=params))["items"]

    async def mark(self, item_ids: list[int], *, read: bool, timeout: float | None = None) -> None:
        if not item_ids:
            return
        path = "/items/read/multiple" if read else "/items/unread/multiple"
        await self._request("POST", path, json={"itemIds": item_ids}, timeout=timeout)
