"""Getting the full article: fetch the page, extract the main content, and decide
whether it beats the text Nextcloud News already has."""

import html
import http.cookiejar
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import trafilatura
from trafilatura.utils import decode_file
from lxml import etree
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
# Logged with a refusal: they tell a JavaScript challenge (Cloudflare sets cf-mitigated) from a
# plain block or a rate limit, and which bot protection answered.
REFUSAL_HEADERS = ("server", "cf-mitigated", "retry-after")
REFUSAL_PEEK_BYTES = 8192
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

    def _log_refusal(self, url: str, response: httpx.Response) -> None:
        """One line per site every few minutes, not one per image."""
        host = httpx.URL(url).host
        now = time.monotonic()
        with self._refusals_lock:
            quiet = now - self._refusals.get(host, -REFUSAL_LOG_INTERVAL) < REFUSAL_LOG_INTERVAL
            if not quiet:
                self._refusals[host] = now
        details = _refusal_details(response)
        if quiet:
            log.debug("fetch %s: HTTP %s (%s)", url, response.status_code, details)
        else:
            log.info("fetch %s: HTTP %s (%s); %s refuses automated requests (more refusals from it in the "
                     "next %d minutes are logged at DEBUG level)", url, response.status_code, details, host,
                     REFUSAL_LOG_INTERVAL // 60)

    def _get(self, url: str, limit: int, headers: dict[str, str]) -> tuple[bytes, str, str, str | None] | None:
        """Return (body, final url, content type, charset), or None on any failure."""
        try:
            with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code in REFUSED:
                    self._log_refusal(url, response)
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


def _refusal_details(response: httpx.Response) -> str:
    """The telling headers and the refusal page's title (or first text), e.g.
    "server=cloudflare, cf-mitigated=challenge, page 'Just a moment...'"."""
    parts = [f"{name}={response.headers[name]}" for name in REFUSAL_HEADERS if name in response.headers]
    head = b""
    try:
        for chunk in response.iter_bytes():
            head += chunk
            if len(head) >= REFUSAL_PEEK_BYTES:
                break
    except httpx.HTTPError:
        pass
    page = head[:REFUSAL_PEEK_BYTES].decode("utf-8", errors="replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    if title:
        text = title.group(1)
    else:
        text = re.sub(r"<(script|style)\b.*?(</\1>|$)", " ", page, flags=re.I | re.S)
        text = re.sub(r"<[^>]*>?", " ", text)
    text = " ".join(html.unescape(text).split())
    if text:
        parts.append(f"page {text[:100]!r}")
    return ", ".join(parts) or "no details"


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
        tree = trafilatura.load_html(page_html)
        if tree is not None:
            _move_breaks_out_of_formatting(tree)
        extracted = trafilatura.extract(
            tree if tree is not None else page_html,
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
    # trafilatura serializes its output as XML. Read it as XML too: the HTML parser closes a
    # <p> at the <pre> of an inline version number, before _restore_inline_code sees it.
    try:
        root = etree.fromstring(document)
    except etree.XMLSyntaxError:
        root = lxml_html.document_fromstring(document)
    body = root.find("body")
    if body is None:
        return document
    _restore_inline_code(body)
    parts = [html.escape(body.text or "")]
    parts += [etree.tostring(child, encoding="unicode", method="html") for child in body]
    return "".join(parts)


INLINE_PARENTS = {"p", "li", "td", "th", "dd", "dt", "a", "strong", "em", "b", "i", "span", "h1", "h2", "h3",
                  "h4", "h5", "h6", "figcaption"}
FORMATTING = {"strong", "b", "em", "i", "u", "span"}


def _move_breaks_out_of_formatting(tree) -> None:
    """trafilatura drops a <br> at the end of bold or italic text, so a heading written as
    <strong>Heading<br></strong>Text or <strong>Heading</strong><strong><br></strong>Text
    runs into the text. Move such a <br> to just after the formatting element."""
    for br in list(tree.iter("br")):
        parent = br.getparent()
        while parent is not None and parent.tag in FORMATTING and br.getnext() is None \
                and not (br.tail or "").strip():
            br.tail, parent.tail = parent.tail, None
            parent.addnext(br)
            parent = br.getparent()


def _restore_inline_code(body) -> None:
    """trafilatura turns every <code> into <pre>, also inline ones such as a version number
    in a sentence. Turn those back into <code> so the sentence stays one paragraph."""
    for pre in list(body.iter("pre")):
        if "\n" in "".join(pre.itertext()).strip():
            continue  # a real code block
        parent = pre.getparent()
        previous = pre.getprevious()
        text_before = previous.tail if previous is not None else (parent.text if parent is not None else None)
        if (parent is not None and parent.tag in INLINE_PARENTS) or (text_before or "").strip() \
                or (pre.tail or "").strip():
            pre.tag = "code"


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
