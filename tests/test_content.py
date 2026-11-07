import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from neneko.builder.extract import Fetcher, choose_content, extract_main_content
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
        return httpx.Response(403, headers={"Server": "cloudflare", "cf-mitigated": "challenge"},
                              html="<html><head><title>Just a moment...</title></head><body>"
                                   "<script>challenge()</script></body></html>")

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
    infos = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert len(infos) == 1
    assert "HTTP 403 (server=cloudflare, cf-mitigated=challenge, page 'Just a moment...')" in infos[0]
    assert Fetcher(user_agent="Custom/1")._client.headers["user-agent"] == "Custom/1"


def test_refusal_without_title_logs_the_first_text(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"},
                              html="<body><style>p{}</style><p>Too   many &amp; requests</p></body>")

    with caplog.at_level("INFO", logger="neneko.builder.extract"):
        assert Fetcher(transport=httpx.MockTransport(handler)).page("https://busy.example/") is None
    assert "HTTP 429 (retry-after=120, page 'Too many & requests')" in caplog.text


def test_rate_limits_are_waited_out(monkeypatch, caplog):
    slept = []
    monkeypatch.setattr("neneko.builder.extract.time.sleep", slept.append)
    answers = {"https://wp.example/a": [httpx.Response(429), httpx.Response(429, headers={"Retry-After": "7"}),
                                        httpx.Response(200, html="<p>Article</p>")],
               "https://wp.example/b": [httpx.Response(429)] * 3}

    def handler(request: httpx.Request) -> httpx.Response:
        return answers[str(request.url)].pop(0)

    fetcher = Fetcher(transport=httpx.MockTransport(handler), rate_limit_waits=(5.0, 20.0))
    with caplog.at_level("INFO", logger="neneko.builder.extract"):
        assert fetcher.page("https://wp.example/a") == ("<p>Article</p>", "https://wp.example/a")
        assert slept == [5.0, 7.0]
        assert "refuses" not in caplog.text
        assert fetcher.page("https://wp.example/b") is None  # three 429s: given up
    assert slept == [5.0, 7.0, 5.0, 20.0] and not answers["https://wp.example/b"]
    assert "wp.example refuses automated requests" in caplog.text


def test_one_request_at_a_time_per_host():
    running, most = {}, {}
    lock = threading.Lock()
    both_hosts = threading.Barrier(2, timeout=5)

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        with lock:
            running[host] = running.get(host, 0) + 1
            most[host] = max(most.get(host, 0), running[host])
        if request.url.path == "/meet":
            both_hosts.wait()  # times out if the other host's request can't run alongside
        else:
            time.sleep(0.02)
        with lock:
            running[host] -= 1
        return httpx.Response(200, html="<p>ok</p>")

    fetcher = Fetcher(transport=httpx.MockTransport(handler))
    urls = [f"https://one.example/{n}" for n in range(6)] + ["https://one.example/meet", "https://two.example/meet"]
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(fetcher.page, urls))
    assert all(results) and most == {"one.example": 1, "two.example": 1}


def test_inline_code_stays_inline_and_code_blocks_stay_blocks():
    filler = "<p>" + "Some introduction text about apps and their updates this week. " * 15 + "</p>"
    item = ('<li><strong><a href="https://f-droid.org/en/packages/eu.siacs.conversations">Conversations</a></strong>'
            ' since this week with update <code class="language-plaintext highlighter-rouge">2.20.3+free</code></li>')
    block = "<pre><code>apt update\napt install neneko</code></pre>"
    paragraph = ('<p><strong><a href="https://f-droid.org/en/packages/com.foobnix.pro.pdf.reader">Librera Reader</a>'
                 '</strong> was updated to <code class="language-plaintext highlighter-rouge">9.6.17-fdroid</code>'
                 " and we fixed our build recipe.</p>")
    page = (f"<html><body><article><h1>Updates</h1>{filler}<ul>{item}<li>Another app with an update</li></ul>"
            f"{paragraph}{filler}{block}{filler}</article></body></html>")
    extracted = extract_main_content(page, "https://example.org/post")
    assert "with update <code>2.20.3+free</code></li>" in extracted
    assert "was updated to <code>9.6.17-fdroid</code> and we fixed our build recipe.</p>" in extracted
    assert "<pre>apt update\napt install neneko</pre>" in extracted


def test_bold_heading_with_line_break_keeps_the_break_and_bold_ends():
    filler = "<p>" + "Some text about parking plans in the neighbourhood next door. " * 15 + "</p>"
    page = ("<html><body><article><h1>Parking</h1>" + filler +
            '<p style="background-color:#e1ebfc"><strong>In a box</strong><br>Boxed text.</p>'
            "<p><strong>Separate break</strong><strong><br></strong>Text after the first heading.</p>"
            "<p><strong>Break inside<br></strong>Text after the second heading.</p>"
            "<p>A paragraph that is not bold.</p>" + filler + "</article></body></html>")
    cleaned = clean_fragment(extract_main_content(page, "https://example.org/post"), "https://example.org/post")
    assert "<strong>In a box</strong><br>Boxed text." in cleaned
    assert "<strong>Separate break</strong><br>Text after the first heading." in cleaned
    assert "<strong>Break inside</strong><br>Text after the second heading." in cleaned
    assert "<strong></strong>" not in cleaned


def test_nested_formatting_keeps_the_space_after_it():
    filler = "<p>" + "Some text about a musical in a small theatre in town. " * 15 + "</p>"
    page = ("<html><body><article><h1>Awards</h1>" + filler +
            "<p><strong>won with the musical</strong> <strong><em>Next to Normal</em></strong> "
            "<strong>three awards</strong></p>"
            '<p>read <a href="/x"><em>the review</em></a> <strong>first</strong></p>' + filler +
            "</article></body></html>")
    cleaned = clean_fragment(extract_main_content(page, "https://example.org/post"), "https://example.org/post")
    assert "<i>Next to Normal</i> <strong>three awards</strong>" in cleaned
    # trafilatura drops the link itself here, with or without the space
    assert "<i>the review</i> <strong>first</strong>" in cleaned.replace("</a>", "")


def test_empty_inline_elements_are_dropped():
    cleaned = clean_fragment('<p><strong>Head</strong><strong></strong>text <a href="/x"><em></em></a>end</p>',
                             "https://x/")
    assert cleaned == "<p><strong>Head</strong>text end</p>"
    assert clean_fragment("<p>a<br>b <span> </span>c</p>", "https://x/") == "<p>a<br>b <span> </span>c</p>"


def test_paragraph_after_an_anchor_target_is_not_a_link():
    # An anchor target before a paragraph; trafilatura moves the text into it as <a href="">.
    filler = "<p>" + "Gewone tekst van het artikel met genoeg woorden voor de extractie. " * 8 + "</p>"
    page = (f"<html><body><article><h1>Titel</h1>{filler}"
            f"<p><a id='intro'></a>Het is in tien jaar tijd normaal geworden.</p>{filler}</article></body></html>")
    extracted = extract_main_content(page, "https://example.org/post")
    assert '<a href="">' in extracted  # what trafilatura gives
    cleaned = clean_fragment(extracted, "https://example.org/post")
    assert "<p>Het is in tien jaar tijd normaal geworden.</p>" in cleaned
    assert clean_fragment('<p><a href="javascript:void(0)">Lees meer</a> <a href="/x">echt</a></p>',
                          "https://example.org/") == '<p>Lees meer <a href="https://example.org/x">echt</a></p>'


def test_only_code_blocks_keep_their_line_breaks():
    # Inline code with a line break in its source ("include <code>\ngtk-...</code>") must not
    # break the line on the Kobo; whitespace is kept in <pre> blocks only.
    import re

    from neneko.builder.epub import STYLESHEET

    rules = dict(re.findall(r"^([^{]+)\{([^}]*)\}", STYLESHEET, re.M))
    keeping = [selector.strip() for selector, body in rules.items() if "white-space: pre" in body]
    assert keeping == ["pre"]


def test_image_captions_come_through_as_one_paragraph():
    # Omroep Delft writes captions as separate bold pieces; trafilatura made each one a
    # paragraph, dropped plain-text captions and the text before an <em>.
    filler = "<p>" + "Gewone tekst van het artikel met genoeg woorden voor de extractie. " * 8 + "</p>"
    captions = [
        "<strong>Met een hyperloop</strong> <strong>wordt er gereisd</strong> <strong>| Foto: Delft</strong>",
        "Met een hyperloop wordt er gereisd | Foto: Delft",
        "Met een <em>hyperloop</em> wordt er gereisd | Foto: Delft",
    ]
    expected = [
        "<p><strong>Met een hyperloop</strong> <strong>wordt er gereisd</strong> <strong>| Foto: Delft</strong></p>",
        "<p>Met een hyperloop wordt er gereisd | Foto: Delft</p>",
        "<p>Met een <i>hyperloop</i> wordt er gereisd | Foto: Delft</p>",
    ]
    for caption, want in zip(captions, expected, strict=True):
        page = (f"<html><body><article><h1>Titel</h1>{filler}<figure class='wp-block-image'>"
                f"<img src='https://example.org/a.png' alt=''><figcaption class='wp-element-caption'>{caption}"
                f"</figcaption></figure>{filler}</article></body></html>")
        assert want in extract_main_content(page, "https://example.org/post")
