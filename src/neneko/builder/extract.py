"""Getting the full article: fetch the page, extract the main content, and decide
whether it beats the text Nextcloud News already has."""

import http.cookiejar
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import trafilatura
from trafilatura.utils import decode_file
from lxml import html as lxml_html
from readability import Document

from .sanitize import text_length

log = logging.getLogger(__name__)

# Some sites refuse anything that doesn't look like a browser, so Neneko sends what one sends.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
PAGE_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
ACCEPT_LANGUAGE = "en-US,en;q=0.9,*;q=0.5"
REFUSED = {401, 403, 429}
REFUSAL_LOG_INTERVAL = 600  # seconds between "site refuses" lines per host
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# An extraction that recovers less than this share of the feed's own text is
# probably a cookie wall or a teaser page; the feed text wins then.
MIN_SHARE_OF_FEED_TEXT = 0.8


class Fetcher:
    """Blocking HTTP client used by the build workers."""

    def __init__(self, cookies_file: Path | None = None, *, user_agent: str = "",
                 transport: httpx.BaseTransport | None = None):
        cookies = None
        if cookies_file:
            jar = http.cookiejar.MozillaCookieJar(str(cookies_file))
            jar.load(ignore_discard=True, ignore_expires=True)
            cookies = jar
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(20, connect=5),
            headers={"User-Agent": user_agent or USER_AGENT, "Accept-Language": ACCEPT_LANGUAGE},
            cookies=cookies,
            transport=transport,
        )
        self._refusals: dict[str, float] = {}
        self._refusals_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def _log_refusal(self, url: str, status: int) -> None:
        """One line per site every few minutes, not one per image."""
        host = httpx.URL(url).host
        now = time.monotonic()
        with self._refusals_lock:
            quiet = now - self._refusals.get(host, -REFUSAL_LOG_INTERVAL) < REFUSAL_LOG_INTERVAL
            if not quiet:
                self._refusals[host] = now
        if quiet:
            log.debug("fetch %s: HTTP %s", url, status)
        else:
            log.info("fetch %s: HTTP %s; %s refuses automated requests (more refusals from it in the next "
                     "%d minutes are logged at DEBUG level)", url, status, host, REFUSAL_LOG_INTERVAL // 60)

    def _get(self, url: str, limit: int, headers: dict[str, str]) -> tuple[bytes, str, str, str | None] | None:
        """Return (body, final url, content type, charset), or None on any failure."""
        try:
            with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code in REFUSED:
                    self._log_refusal(url, response.status_code)
                    return None
                if response.status_code >= 400:
                    log.info("fetch %s: HTTP %s", url, response.status_code)
                    return None
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        log.info("fetch %s: larger than %d bytes, skipped", url, limit)
                        return None
                    chunks.append(chunk)
                return (b"".join(chunks), str(response.url),
                        response.headers.get("content-type", ""), response.charset_encoding)
        except (httpx.HTTPError, ValueError) as exc:
            log.info("fetch %s failed: %r", url, exc)
            return None

    def page(self, url: str) -> tuple[str, str] | None:
        """Return (html, final url) of a web page."""
        result = self._get(url, MAX_PAGE_BYTES, {"Accept": PAGE_ACCEPT})
        if result is None:
            return None
        body, final_url, content_type, charset = result
        if content_type and "html" not in content_type:
            return None
        if charset:
            return body.decode(charset, errors="replace"), final_url
        return decode_file(body), final_url  # sniffs <meta charset> and byte patterns

    def binary(self, url: str, referer: str | None = None) -> bytes | None:
        """An image; `referer` is the page it appears on, as a browser would send it."""
        headers = {"Accept": IMAGE_ACCEPT}
        if referer and referer.startswith(("http://", "https://")):
            headers["Referer"] = referer
        result = self._get(url, MAX_IMAGE_BYTES, headers)
        return result[0] if result is not None else None


@dataclass
class Content:
    html: str
    base_url: str
    language: str | None
    source: str  # "extracted" or "feed"


def page_language(page_html: str) -> str | None:
    try:
        root = lxml_html.document_fromstring(page_html)
    except Exception:  # noqa: BLE001
        return None
    lang = (root.get("lang") or root.get("xml:lang") or "").strip()
    return lang.split("-")[0].lower() or None


def extract_main_content(page_html: str, url: str) -> str | None:
    """Main article HTML from a full page: trafilatura first, readability as fallback."""
    try:
        extracted = trafilatura.extract(
            page_html,
            url=url,
            output_format="html",
            include_images=True,
            include_tables=True,
            include_links=True,
            include_formatting=True,
            include_comments=False,
        )
    except Exception as exc:  # noqa: BLE001 - extractor bugs must not break the build
        log.warning("trafilatura failed on %s: %r", url, exc)
        extracted = None
    if extracted:
        body = _body_inner_html(extracted)
        if body.strip():
            return body
    try:
        return Document(page_html, url=url).summary(html_partial=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("readability failed on %s: %r", url, exc)
        return None


def _body_inner_html(document: str) -> str:
    root = lxml_html.document_fromstring(document)
    body = root.find("body")
    if body is None:
        return document
    parts = [body.text or ""]
    parts += [lxml_html.tostring(child, encoding="unicode") for child in body]
    return "".join(parts)


def choose_content(item: dict, *, extract: bool, fetcher: Fetcher) -> Content:
    feed_html = item.get("body") or ""
    url = item.get("url") or ""
    if extract and url.startswith(("http://", "https://")):
        page = fetcher.page(url)
        if page is not None:
            page_html, final_url = page
            extracted = extract_main_content(page_html, final_url)
            if extracted and text_length(extracted) >= MIN_SHARE_OF_FEED_TEXT * text_length(feed_html):
                return Content(extracted, final_url, page_language(page_html), "extracted")
            log.info("item %s: extraction gave less text than the feed, using the feed text", item.get("id"))
    return Content(feed_html, url or "", None, "feed")
