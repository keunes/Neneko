"""Turn feed or extracted HTML into a clean fragment an e-reader can render."""

import html
import re
from urllib.parse import urljoin, urlparse

import nh3
from lxml import etree
from lxml import html as lxml_html

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "cite", "code", "dd", "del", "dfn", "div",
    "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img",
    "ins", "kbd", "li", "mark", "ol", "p", "pre", "q", "s", "samp", "small", "span", "strong",
    "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "u", "ul", "var",
}
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "ol": {"start"},
    "*": {"dir", "lang"},
}
# Inline elements that mean nothing when empty. An empty one ends up self-closed in the EPUB
# (<strong/>), and kepubify's HTML parser reads that as an opening tag that is never closed:
# everything after it turns bold, or into a link.
EMPTY_DROPPABLE = {
    "a", "abbr", "b", "cite", "code", "del", "dfn", "em", "i", "ins", "kbd", "mark", "q", "s", "samp",
    "small", "span", "strong", "sub", "sup", "time", "u", "var",
}
EMBED_TAGS = ("iframe", "video", "audio", "embed", "object")
LAZY_SRC_ATTRIBUTES = ("data-src", "data-lazy-src", "data-original", "data-url")


def _youtube_watch_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        match = re.match(r"^/embed/([\w-]+)", parsed.path)
        if match:
            return f"https://www.youtube.com/watch?v={match.group(1)}"
    if host == "player.vimeo.com":
        match = re.match(r"^/video/(\d+)", parsed.path)
        if match:
            return f"https://vimeo.com/{match.group(1)}"
    return url


def _embed_label(element) -> str:
    title = (element.get("title") or "").strip()
    kind = "Audio" if element.tag == "audio" else "Video"
    return f"▶ {kind}: {title}" if title else f"▶ {kind}"


def _embed_src(element) -> str | None:
    src = element.get("src") or element.get("data")
    if not src:
        for source in element.iter("source"):
            if source.get("src"):
                return source.get("src")
    return src


def _best_srcset(srcset: str) -> str | None:
    candidates = []
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        width = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                width = int(bits[1][:-1])
            except ValueError:
                width = 0
        candidates.append((width, bits[0]))
    if not candidates:
        return None
    # Prefer the smallest image that is still at least e-reader sized.
    big_enough = sorted(c for c in candidates if c[0] >= 1000)
    return (big_enough[0] if big_enough else max(candidates))[1]


def drop_empty_inline(root) -> None:
    """Unwrap inline elements without text or children, innermost first."""
    for element in reversed(list(root.iter(*EMPTY_DROPPABLE))):
        if element is not root and not element.text and not len(element):
            element.drop_tag()


def inner_html(root) -> str:
    """Serialize an element's content without the element itself."""
    parts = [html.escape(root.text)] if root.text else []
    parts += [etree.tostring(child, encoding="unicode", method="html") for child in root]
    return "".join(parts)


def clean_fragment(fragment: str, base_url: str) -> str:
    """Replace embeds with links, fix lazy-loaded images, make URLs absolute and
    strip everything that isn't simple, readable markup."""
    if not fragment or not fragment.strip():
        return ""
    root = lxml_html.fragment_fromstring(fragment, create_parent="div")

    for element in list(root.iter(*EMBED_TAGS)):
        src = _embed_src(element)
        parent = element.getparent()
        if parent is None:
            continue
        if src and not src.startswith("data:"):
            paragraph = etree.Element("p")
            link = etree.SubElement(paragraph, "a", href=_youtube_watch_url(urljoin(base_url, src)))
            link.text = _embed_label(element)
            paragraph.tail = element.tail
            parent.replace(element, paragraph)
        else:
            element.drop_tree()  # keeps the text that followed the embed

    for image in root.iter("img"):
        src = image.get("src") or ""
        if not src or src.startswith("data:"):
            for attribute in LAZY_SRC_ATTRIBUTES:
                if image.get(attribute):
                    src = image.get(attribute)
                    break
            else:
                srcset = image.get("srcset") or image.get("data-srcset")
                src = _best_srcset(srcset) if srcset else src
        if src and not src.startswith("data:"):
            image.set("src", urljoin(base_url, src))
        else:
            image.set("src", "")

    for link in root.iter("a"):
        if link.get("href"):
            link.set("href", urljoin(base_url, link.get("href")))

    cleaned = nh3.clean(
        inner_html(root),
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel=None,
        strip_comments=True,
    )
    if not cleaned.strip():
        return cleaned
    root = lxml_html.fragment_fromstring(cleaned, create_parent="div")
    drop_empty_inline(root)
    return inner_html(root)


def text_length(fragment: str) -> int:
    """Number of visible characters, whitespace collapsed."""
    if not fragment or not fragment.strip():
        return 0
    try:
        root = lxml_html.fragment_fromstring(fragment, create_parent="div")
    except etree.ParserError:
        return 0
    return len(" ".join(root.text_content().split()))


def plain_excerpt(fragment: str, limit: int = 300) -> str:
    if not fragment:
        return ""
    try:
        root = lxml_html.fragment_fromstring(fragment, create_parent="div")
    except etree.ParserError:
        return ""
    text = " ".join(root.text_content().split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def image_sources(fragment: str) -> list[str]:
    if not fragment:
        return []
    root = lxml_html.fragment_fromstring(fragment, create_parent="div")
    return [img.get("src") for img in root.iter("img") if img.get("src")]


def drop_leading_title(fragment: str, title: str) -> str:
    """Remove a first heading that repeats the article title (Neneko adds its own)."""
    if not fragment or not title:
        return fragment
    root = lxml_html.fragment_fromstring(fragment, create_parent="div")
    first = next(iter(root), None)
    normalize = lambda text: " ".join(text.split()).casefold()  # noqa: E731
    if first is not None and first.tag in ("h1", "h2") and normalize(first.text_content()) == normalize(title):
        first.drop_tree()
        return inner_html(root)
    return fragment
