import json
from pathlib import Path

import httpx

from neneko.builder.extract import Fetcher, choose_content
from neneko.builder.sanitize import clean_fragment, drop_leading_title, image_sources, plain_excerpt, text_length

FIXTURES = Path(__file__).parent / "fixtures"
ITEMS = {item["id"]: item for item in json.loads((FIXTURES / "items_sample.json").read_text())["items"]}
PAGE = (FIXTURES / "article_page.html").read_text()
PAGE_URL = "https://smarthome.example/products/acme-lumen-hub-delayed-again/"


def fetcher_for(pages: dict[str, str]) -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        page = pages.get(str(request.url))
        if page is None:
            return httpx.Response(404)
        return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})
    return Fetcher(transport=httpx.MockTransport(handler))


def test_iframes_become_links_and_scripts_go():
    podcast = ITEMS[1101]
    cleaned = clean_fragment(podcast["body"], podcast["url"])
    assert "<iframe" not in cleaned
    assert 'href="https://www.youtube.com/watch?v=abcDEF12345"' in cleaned
    assert "▶ Video: Tiny gardens, big tomatoes" in cleaned
    assert "<script" not in clean_fragment('<p>a</p><script>alert(1)</script><p onclick="x()">b</p>', "https://x/")
    assert "onclick" not in clean_fragment('<p onclick="x()">b</p>', "https://x/")


def test_lazy_images_and_absolute_urls():
    fragment = ('<p><a href="/next">next</a></p><img src="data:image/gif;base64,AAAA" data-src="/img/a.jpg">'
                '<img srcset="/s.jpg 400w, /m.jpg 1200w, /l.jpg 2400w">')
    cleaned = clean_fragment(fragment, "https://example.org/post/1")
    assert 'href="https://example.org/next"' in cleaned
    assert image_sources(cleaned) == ["https://example.org/img/a.jpg", "https://example.org/m.jpg"]


def test_drop_leading_title_and_text_helpers():
    fragment = "<h1> Hello  World </h1><p>Body text here.</p>"
    assert drop_leading_title(fragment, "Hello World") == "<p>Body text here.</p>"
    assert drop_leading_title(fragment, "Something else") == fragment
    assert text_length("<p>one  two</p><p>three</p>") == len("one twothree")
    assert plain_excerpt("<p>" + "word " * 200 + "</p>", limit=50).endswith("…")


def test_teaser_feed_is_replaced_by_extracted_article():
    teaser = ITEMS[1104]
    content = choose_content(teaser, extract=True, fetcher=fetcher_for({PAGE_URL: PAGE}))
    assert content.source == "extracted"
    assert content.language == "en"
    assert "Certification for the new protocol version" in content.html
    assert "Related articles" not in content.html
    assert "We use cookies" not in content.html
    assert text_length(content.html) > 2 * text_length(teaser["body"])


def test_feed_text_used_when_extraction_is_off_or_fails():
    teaser = ITEMS[1104]
    assert choose_content(teaser, extract=False, fetcher=fetcher_for({PAGE_URL: PAGE})).source == "feed"
    assert choose_content(teaser, extract=True, fetcher=fetcher_for({})).source == "feed"


def test_feed_wins_when_extraction_is_shorter():
    full = ITEMS[1105]  # this feed sends the whole article
    cookie_wall = "<html><body><main><p>Please accept our cookies to continue reading.</p></main></body></html>"
    content = choose_content(full, extract=True, fetcher=fetcher_for({full["url"]: cookie_wall}))
    assert content.source == "feed"


def test_clean_fragment_has_no_wrapper_and_title_is_dropped():
    cleaned = clean_fragment("<h1>Title</h1><p>Text &amp; more</p>", "https://x/")
    assert cleaned.startswith("<h1>")
    assert drop_leading_title(cleaned, "Title") == "<p>Text &amp; more</p>"
    assert clean_fragment("plain &lt;text&gt;", "https://x/") == "plain &lt;text&gt;"


def test_requests_look_like_a_browser_and_refusals_are_logged_once(caplog):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(403)

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    with caplog.at_level("INFO", logger="neneko.builder.extract"):
        assert fetcher.page("https://blocked.example/post") is None
        for number in range(3):
            assert fetcher.binary(f"https://blocked.example/img{number}.png",
                                  referer="https://blocked.example/post") is None
    page, image = seen[0], seen[1]
    assert "Neneko" not in page.headers["user-agent"] and "Chrome/" in page.headers["user-agent"]
    assert page.headers["accept"].startswith("text/html") and "referer" not in page.headers
    assert image.headers["accept"].startswith("image/") and image.headers["referer"] == "https://blocked.example/post"
    assert len([r for r in caplog.records if r.levelname == "INFO"]) == 1
    assert Fetcher(user_agent="Custom/1")._client.headers["user-agent"] == "Custom/1"
