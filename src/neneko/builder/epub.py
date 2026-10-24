"""Writing one article as an EPUB 3 file."""

import html
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from ebooklib import epub

STYLESHEET = """\
body { margin: 0; line-height: 1.45; }
header.neneko { margin: 0 0 1.5em 0; padding-bottom: 0.8em; border-bottom: 1px solid #888; }
header.neneko h1 { font-size: 1.5em; line-height: 1.2; margin: 0 0 0.4em 0; }
header.neneko p { margin: 0.15em 0; font-size: 0.85em; text-indent: 0; }
img { max-width: 100%; height: auto; }
figure { margin: 1em 0; }
figcaption { font-size: 0.85em; font-style: italic; }
blockquote { margin: 1em 1.5em; font-style: italic; }
pre, code { font-family: monospace; font-size: 0.9em; white-space: pre-wrap; }
table { border-collapse: collapse; }
td, th { border: 1px solid #888; padding: 0.2em 0.4em; }
p.neneko-note { font-size: 0.85em; font-style: italic; }
"""


@dataclass
class EpubImage:
    file_name: str  # path inside the EPUB, e.g. "images/img001.jpg"
    media_type: str
    data: bytes


@dataclass
class Article:
    uuid: str
    title: str
    feed_title: str
    author: str
    url: str
    published: int | None
    language: str
    rtl: bool
    body_html: str  # cleaned fragment, image src attributes already point at EPUB files
    images: list[EpubImage] = field(default_factory=list)
    cover: EpubImage | None = None
    note_html: str = ""


def _reading_minutes(text_chars: int) -> int:
    return max(1, round(text_chars / 5 / 230))  # ~5 characters per word, 230 words per minute


def header_html(article: Article, text_chars: int) -> str:
    bits = []
    if article.author and article.author.casefold() != article.feed_title.casefold():
        bits.append(html.escape(article.author))
    bits.append(html.escape(article.feed_title))
    if article.published:
        bits.append(datetime.fromtimestamp(article.published, tz=timezone.utc).strftime("%d %b %Y"))
    bits.append(f"{_reading_minutes(text_chars)} min")
    source = ""
    if article.url:
        domain = urlparse(article.url).netloc.removeprefix("www.") or article.url
        source = f'<p><a href="{html.escape(article.url, quote=True)}">{html.escape(domain)}</a></p>'
    return (
        '<header class="neneko">'
        f"<h1>{html.escape(article.title)}</h1>"
        f"<p>{' · '.join(bits)}</p>{source}</header>"
    )


def write_epub(article: Article, target: Path, *, text_chars: int) -> None:
    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{article.uuid}")
    book.set_title(article.title)
    book.set_language(article.language)
    book.add_author(article.feed_title)
    book.add_metadata("DC", "publisher", article.feed_title)
    if article.url:
        book.add_metadata("DC", "source", article.url)
    if article.published:
        book.add_metadata(
            "DC", "date", datetime.fromtimestamp(article.published, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    if article.rtl:
        book.set_direction("rtl")

    style = epub.EpubItem(uid="style", file_name="style.css", media_type="text/css",
                          content=STYLESHEET.encode())
    book.add_item(style)

    direction = ' dir="rtl"' if article.rtl else ""
    chapter = epub.EpubHtml(title=article.title, file_name="article.xhtml", lang=article.language)
    chapter.content = (
        f"<html><body><div{direction}>{header_html(article, text_chars)}"
        f"{article.body_html}{article.note_html}</div></body></html>"
    )
    chapter.add_item(style)
    book.add_item(chapter)

    for number, image in enumerate(article.images, start=1):
        book.add_item(epub.EpubItem(uid=f"img{number:03d}", file_name=image.file_name,
                                    media_type=image.media_type, content=image.data))
    if article.cover:
        book.set_cover(article.cover.file_name, article.cover.data, create_page=False)

    book.toc = [epub.Link("article.xhtml", article.title, "article")]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = [chapter]
    epub.write_epub(str(target), book)
