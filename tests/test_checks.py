import asyncio
import io
import zipfile

import httpx
import pytest

from neneko.ids import item_uuid
from neneko.nextcloud import NewsClient

from .conftest import kepubify_binary


def check(handler, folders=()):
    async def run():
        client = NewsClient("https://cloud.example.org", "reader", "pw", transport=httpx.MockTransport(handler))
        try:
            return await client.check(list(folders))
        finally:
            await client.aclose()
    return asyncio.run(run())


def raising(exc):
    def handler(request):
        raise exc
    return handler


@pytest.mark.parametrize("handler, words", [
    (raising(httpx.ConnectError("[Errno -5] No address associated with hostname")), "can't be resolved"),
    (raising(httpx.ConnectError("[Errno 111] Connection refused")), "Nothing answers"),
    (raising(httpx.ReadTimeout("timed out")), "didn't answer in time"),
    (raising(httpx.ProxyError("403 Forbidden")), "Nothing answers"),
    (lambda r: httpx.Response(401, json={"message": "Current user is not logged in"}), "login name"),
    (lambda r: httpx.Response(400, text="<html>Access through untrusted domain</html>"), "trusted_domains"),
    (lambda r: httpx.Response(404, text="<html>Not found</html>"), "News app"),
    (lambda r: httpx.Response(200, text="<html>login page</html>"), "News API's JSON"),
])
def test_check_explains_failures(handler, words):
    ok, summary, hint = check(handler)
    assert not ok
    assert words in hint


def test_check_ok_and_unknown_folders():
    def handler(request):
        if request.url.path.endswith("/folders"):
            return httpx.Response(200, json={"folders": [{"id": 1, "name": "Tech"}]})
        return httpx.Response(200, json={"feeds": [{"id": 5}, {"id": 6}]})
    assert check(handler) == (True, "1 folders, 2 feeds; syncing all unread items", "")
    ok, summary, hint = check(handler, ["tech", "Nope"])
    assert not ok and "Nope" in summary and "Tech" in hint


def test_startup_check_and_health_endpoint(make_kobo, news, caplog):
    kobo = make_kobo()
    assert kobo.client.get("/healthz?nextcloud=1").json()["nextcloud"]["ok"] is True
    assert "Nextcloud News" in kobo.client.get("/").text
    news.down = True
    response = kobo.client.get("/healthz?nextcloud=1")
    assert response.status_code == 503
    assert kobo.client.get("/healthz").status_code == 200  # process health doesn't depend on Nextcloud


@pytest.mark.skipif(kepubify_binary() is None, reason="kepubify not installed")
def test_no_blank_first_page(make_kobo, news):
    item = news.add_item(51, body="<p>" + "A long enough paragraph of words. " * 40 + "</p>" * 1)
    kobo = make_kobo(start_workers=True)
    kobo.sync()
    response = kobo.client.get(kobo.url(f"/download/{item_uuid(kobo.ctx.prefix, item['id'])}/kepub"))
    book = zipfile.ZipFile(io.BytesIO(response.content))
    assert "kepubify-titlepage-dummy" not in book.read("EPUB/content.opf").decode()
    assert 'class="koboSpan"' in book.read("EPUB/article.xhtml").decode()
