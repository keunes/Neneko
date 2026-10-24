"""Background article builds.

Sync only queues builds, newest first. A download request for an article that isn't
built yet moves it to the front of the queue and waits for it.
"""

import asyncio
import html
import itertools
import json
import logging
import os
import queue
import threading
from concurrent.futures import Future
from pathlib import Path

from lxml import html as lxml_html

from ..config import Settings
from ..state import State
from .cover import render_cover
from .epub import Article, EpubImage, write_epub
from .extract import Fetcher, choose_content
from .images import process_image
from .kepub import kepubify, kepubify_available
from .sanitize import clean_fragment, drop_leading_title, image_sources, inner_html, text_length

log = logging.getLogger(__name__)

URGENT, BACKGROUND = 0, 10
COVER_SIZE = (1264, 1680)


def lead_image_url(item: dict) -> str | None:
    """The article's own lead image: the enclosure if it is an image, else the media thumbnail."""
    link, mime = item.get("enclosureLink"), (item.get("enclosureMime") or "")
    if link and mime.startswith("image/"):
        return link
    return item.get("mediaThumbnail") or None


def _rewrite_images(fragment: str, mapping: dict[str, str]) -> str:
    """Point img tags at files inside the EPUB; drop images that couldn't be fetched."""
    root = lxml_html.fragment_fromstring(fragment, create_parent="div")
    for image in list(root.iter("img")):
        target = mapping.get(image.get("src") or "")
        if target:
            image.set("src", target)
        else:
            parent = image.getparent()
            image.drop_tree()
            if parent is not None and parent.tag == "figure" and not parent.text_content().strip() \
                    and not len(parent.findall(".//img")):
                parent.drop_tree()
    return inner_html(root)


class Builder:
    def __init__(self, settings: Settings, state: State, fetcher: Fetcher):
        self.settings = settings
        self.state = state
        self.fetcher = fetcher
        self.use_kepub = kepubify_available(settings.kepubify_path)
        if not self.use_kepub:
            log.warning("kepubify not found (%s); articles are served as plain EPUB", settings.kepubify_path)
        self._never_extract = {entry.casefold() for entry in settings.fulltext_never_extract}
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._futures: dict[tuple[int, str], Future] = {}
        self._running: set[tuple[int, str]] = set()
        self._lock = threading.Lock()
        self._seq = itertools.count()
        self._threads: list[threading.Thread] = []
        for name in ("articles", "covers", "lead", "tmp"):
            (settings.cache_dir / name).mkdir(parents=True, exist_ok=True)

    # lifecycle

    def start(self) -> None:
        for number in range(self.settings.build_workers):
            thread = threading.Thread(target=self._worker, name=f"build-{number}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        for _ in self._threads:
            self._queue.put((-1, next(self._seq), None, None))
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()

    # paths

    @staticmethod
    def _hash(row: dict) -> str:
        return (row.get("content_hash") or "0")[:12]

    def article_path(self, row: dict) -> Path:
        suffix = ".kepub.epub" if self.use_kepub else ".epub"
        return self.settings.cache_dir / "articles" / f"{row['item_id']}-{self._hash(row)}{suffix}"

    def purge(self, item_id: int) -> None:
        for directory in ("articles", "covers", "lead"):
            for path in (self.settings.cache_dir / directory).glob(f"{item_id}-*"):
                path.unlink(missing_ok=True)

    # queueing

    def _submit(self, row: dict, priority: int) -> Future:
        key = (row["item_id"], self._hash(row))
        with self._lock:
            future = self._futures.get(key)
            if future is not None and future.done() and future.exception() is None \
                    and self.article_path(row).exists():
                return future
            if future is None or future.done():
                future = Future()
                self._futures[key] = future
            elif priority >= BACKGROUND:
                return future  # already queued
            self._queue.put((priority, next(self._seq), key, row))
            return future

    def enqueue(self, rows: list[dict]) -> None:
        for row in sorted(rows, key=lambda r: -r["item_id"]):
            self._submit(row, BACKGROUND)

    async def article(self, row: dict, timeout: float = 180) -> Path:
        path = self.article_path(row)
        if path.exists():
            return path
        future = self._submit(row, URGENT)
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout)

    def _worker(self) -> None:
        while True:
            priority, _, key, row = self._queue.get()
            if key is None:
                return
            with self._lock:
                future = self._futures.get(key)
                if future is None or future.done() or key in self._running:
                    continue
                self._running.add(key)
            try:
                future.set_result(self.build(row))
            except Exception as exc:  # noqa: BLE001 - reported to whoever waits on the future
                log.exception("building item %s failed", row["item_id"])
                future.set_exception(exc)
            finally:
                with self._lock:
                    self._running.discard(key)
                    if len(self._futures) > 1000:
                        for done_key in [k for k, f in self._futures.items() if f.done()]:
                            del self._futures[done_key]

    # building

    def _extract_wanted(self, item: dict, feed_title: str) -> bool:
        feed_id = str(item.get("feedId", ""))
        return not ({feed_id, feed_title.casefold()} & self._never_extract)

    def lead_image(self, row: dict) -> bytes | None:
        """Raw lead image of an article, fetched once and cached (an empty file means none)."""
        path = self.settings.cache_dir / "lead" / f"{row['item_id']}-{self._hash(row)}"
        if path.exists():
            data = path.read_bytes()
            return data or None
        item = json.loads(row["item_json"]) if row.get("item_json") else {}
        url = lead_image_url(item)
        if url is None:
            sources = image_sources(clean_fragment(item.get("body") or "", item.get("url") or ""))
            url = sources[0] if sources else None
        data = self.fetcher.binary(url) if url else None
        self._write_atomic(path, data or b"")
        return data

    def cover(self, row: dict, width: int, height: int) -> tuple[bytes, str]:
        ext = "png" if self.settings.cover_dither == "ordered" else "jpg"
        path = self.settings.cache_dir / "covers" / f"{row['item_id']}-{self._hash(row)}-{width}x{height}.{ext}"
        media_type = "image/png" if ext == "png" else "image/jpeg"
        if path.exists():
            return path.read_bytes(), media_type
        item = json.loads(row["item_json"]) if row.get("item_json") else {}
        data, media_type = render_cover(
            width, height, title=row.get("title") or item.get("title") or "", feed=row.get("feed_title") or "",
            published=item.get("pubDate"), lead_image=self.lead_image(row) if item else None,
            dither=self.settings.cover_dither,
        )
        self._write_atomic(path, data)
        return data, media_type

    def build(self, row: dict) -> Path:
        item = json.loads(row["item_json"])
        feed_title = row.get("feed_title") or ""
        title = (item.get("title") or "").strip() or "(untitled)"
        extract = self._extract_wanted(item, feed_title)
        content = choose_content(item, extract=extract, fetcher=self.fetcher)

        fragment = drop_leading_title(clean_fragment(content.html, content.base_url), title)
        lead = lead_image_url(item)
        if lead and lead not in image_sources(fragment):
            fragment = f'<figure><img src="{html.escape(lead, quote=True)}" alt=""></figure>{fragment}'

        images, mapping = [], {}
        for src in image_sources(fragment):
            if src in mapping or len(images) >= self.settings.max_images:
                continue
            data = self.fetcher.binary(src)
            processed = process_image(data, max_width=self.settings.image_max_width,
                                      grayscale=self.settings.image_grayscale,
                                      dither=self.settings.image_dither) if data else None
            if processed is None:
                continue
            image_bytes, media_type = processed
            name = f"images/img{len(images) + 1:03d}.{'png' if media_type == 'image/png' else 'jpg'}"
            images.append(EpubImage(name, media_type, image_bytes))
            mapping[src] = name
        fragment = _rewrite_images(fragment, mapping)

        note = ""
        if extract and content.source == "feed" and item.get("url"):
            note = ('<p class="neneko-note">This is the text from the feed; the full article could not '
                    f'be loaded. <a href="{html.escape(item["url"], quote=True)}">Read the original</a>.</p>')

        cover_data, cover_type = render_cover(
            *COVER_SIZE, title=title, feed=feed_title, published=item.get("pubDate"),
            lead_image=self.lead_image(row), dither=self.settings.cover_dither,
        )
        cover = EpubImage("cover." + ("png" if cover_type == "image/png" else "jpg"), cover_type, cover_data)

        article = Article(
            uuid=row["uuid"], title=title, feed_title=feed_title,
            author=(item.get("author") or "").strip(), url=item.get("url") or "",
            published=item.get("pubDate"), language=content.language or self.settings.language,
            rtl=bool(item.get("rtl")), body_html=fragment, images=images, cover=cover, note_html=note,
        )
        target = self.article_path(row)
        tmp_epub = self.settings.cache_dir / "tmp" / f"{row['item_id']}-{self._hash(row)}-{os.getpid()}-{threading.get_ident()}.epub"
        write_epub(article, tmp_epub, text_chars=text_length(fragment))
        try:
            if self.use_kepub:
                tmp_kepub = tmp_epub.with_suffix(".kepub.epub")
                if kepubify(self.settings.kepubify_path, tmp_epub, tmp_kepub):
                    os.replace(tmp_kepub, target)
                else:
                    # Serve the plain EPUB under the expected name rather than nothing.
                    os.replace(tmp_epub, target)
            else:
                os.replace(tmp_epub, target)
        finally:
            tmp_epub.unlink(missing_ok=True)
        for old in (self.settings.cache_dir / "articles").glob(f"{row['item_id']}-*"):
            if old != target:
                old.unlink(missing_ok=True)
        self.state.ledger_set_size(row["item_id"], target.stat().st_size)
        log.info("built item %s (%s, %d images, %d bytes)", row["item_id"], content.source, len(images),
                 target.stat().st_size)
        return target

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        tmp = path.with_name(f".{path.name}.{threading.get_ident()}")
        tmp.write_bytes(data)
        os.replace(tmp, path)
