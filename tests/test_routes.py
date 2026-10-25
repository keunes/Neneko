import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from PIL import Image

from neneko.ids import item_uuid

from .conftest import TOKEN, entitlements, fake_store, kepubify_binary

FIXTURES = Path(__file__).parent / "fixtures"
ITEMS = json.loads((FIXTURES / "items_sample.json").read_text())["items"]
PAGE = (FIXTURES / "article_page.html").read_text()


def jpeg_bytes(width=1600, height=900) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (40, 120, 200)).save(buffer, "JPEG")
    return buffer.getvalue()


def web(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.startswith("https://smarthome.example/products/"):
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})
    if url.endswith(".jpg") or "imageId=" in url:
        return httpx.Response(200, content=jpeg_bytes(), headers={"content-type": "image/jpeg"})
    return httpx.Response(404)


@pytest.fixture
def sample(news):
    for item in ITEMS:
        news.items[item["id"]] = dict(item)
    return news


def test_wrong_token_is_404(make_kobo):
    kobo = make_kobo()
    assert kobo.client.get("/kobo/wrong-token-1234567890/v1/library/sync").status_code == 404
    assert kobo.client.get("/healthz").json()["status"] == "ok"


def test_initialization_points_kobo_at_neneko(make_kobo):
    kobo = make_kobo(store_proxy=True)
    response = kobo.client.get(kobo.url("/v1/initialization"))
    resources = response.json()["Resources"]
    assert response.headers["x-kobo-apitoken"] == "e30="
    assert resources["library_sync"] == f"http://neneko.local:8089/kobo/{TOKEN}/v1/library/sync"
    assert resources["image_url_template"].startswith(f"http://neneko.local:8089/kobo/{TOKEN}/{{ImageId}}/")
    assert resources["instapaper_enabled"] == "True"  # store resources passed through untouched


def test_store_results_are_merged(make_kobo, sample):
    store_book = {"NewEntitlement": {"BookEntitlement": {"Id": "store-book"}, "BookMetadata": {}}}

    def store(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/library/sync":
            assert request.headers.get("x-kobo-synctoken") == ""
            return httpx.Response(200, json=[store_book], headers={
                "x-kobo-synctoken": "next.store", "x-kobo-sync-mode": "full", "x-kobo-recent-reads": "1"})
        return fake_store(request)

    kobo = make_kobo(store_proxy=True, store=store)
    results = kobo.sync()
    assert store_book in results
    assert len(entitlements(results, "NewEntitlement")) == len(ITEMS) + 1
    last = kobo.responses[-1]
    assert last.headers["x-kobo-sync-mode"] == "full"  # passed through
    assert last.headers["x-kobo-recent-reads"] == "5"  # Neneko's, so the whole page is downloaded


def test_unknown_requests_go_to_the_store(make_kobo):
    kobo = make_kobo(store_proxy=True)
    response = kobo.client.get(kobo.url("/v1/user/profile"), follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "https://storeapi.kobo.com/v1/user/profile"
    assert make_kobo(store_proxy=False).client.get(kobo.url("/v1/user/profile")).json() == {}


def test_product_lookups_for_articles_stay_local(make_kobo, sample):
    kobo = make_kobo(store_proxy=True)
    ours = item_uuid(kobo.ctx.prefix, ITEMS[0]["id"])
    nextread = kobo.client.get(kobo.url(f"/v1/products/{ours}/nextread"))
    assert nextread.status_code == 200 and nextread.json() == []
    assert kobo.client.get(kobo.url(f"/v1/products/{ours}/recommendations")).status_code == 404
    response = kobo.client.get(kobo.url("/v1/products/3f0b2c4e-8d7a-4b1e-9c55-1a2b3c4d5e6f/nextread"),
                               follow_redirects=False)
    assert response.status_code == 307


def test_metadata_and_cover(make_kobo, sample):
    kobo = make_kobo(fetch=web)
    kobo.sync()
    uuid = item_uuid(kobo.ctx.prefix, 1105)
    metadata = kobo.client.get(kobo.url(f"/v1/library/{uuid}/metadata")).json()
    assert metadata[0]["Title"].startswith("Researchers find a faster way")
    cover = kobo.client.get(kobo.url(f"/{uuid}/355/530/false/image.jpg"))
    assert cover.status_code == 200
    assert cover.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(cover.content)).size == (355, 530)


@pytest.mark.skipif(kepubify_binary() is None, reason="kepubify not installed")
def test_download_builds_a_kepub_with_full_text_and_images(make_kobo, sample):
    kobo = make_kobo(fetch=web, start_workers=True)
    results = kobo.sync()
    teaser = next(e for e in entitlements(results, "NewEntitlement")
                  if e["BookMetadata"]["Title"].startswith("Acme"))
    url = teaser["BookMetadata"]["DownloadUrls"][0]["Url"].replace("http://neneko.local:8089", "")
    assert teaser["BookMetadata"]["DownloadUrls"][0]["Format"] == "KEPUB"

    response = kobo.client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-kobo-apitoken"] == "e30="
    assert 'filename="1104.kepub"' in response.headers["content-disposition"]
    book = zipfile.ZipFile(io.BytesIO(response.content))
    assert book.namelist()[0] == "mimetype"
    assert book.read("mimetype") == b"application/epub+zip"
    article = book.read("EPUB/article.xhtml").decode()
    assert "Certification for the new protocol version" in article  # extracted, not the teaser
    assert 'class="koboSpan"' in article  # kepubify ran
    assert any(name.startswith("EPUB/images/") for name in book.namelist())
    opf = book.read("EPUB/content.opf").decode()
    assert f"urn:uuid:{item_uuid(kobo.ctx.prefix, 1104)}" in opf

    # The sync waited for the build, so it already reported the real size.
    assert teaser["BookMetadata"]["DownloadUrls"][0]["Size"] == len(response.content)
    uuid = item_uuid(kobo.ctx.prefix, 1104)
    metadata = kobo.client.get(kobo.url(f"/v1/library/{uuid}/metadata")).json()[0]
    assert metadata["DownloadUrls"][0]["Size"] == len(response.content)
    head = kobo.client.head(url)
    assert head.status_code == 200 and int(head.headers["content-length"]) == len(response.content)


def test_download_without_kepubify_serves_epub(make_kobo, sample):
    kobo = make_kobo(fetch=web, kepubify_path="kepubify-missing", start_workers=True)
    results = kobo.sync()
    entry = entitlements(results, "NewEntitlement")[0]
    assert entry["BookMetadata"]["DownloadUrls"][0]["Format"] == "EPUB3"
    url = entry["BookMetadata"]["DownloadUrls"][0]["Url"].replace("http://neneko.local:8089", "")
    response = kobo.client.get(url)
    assert response.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(response.content)).read("mimetype") == b"application/epub+zip"


def test_unknown_book_is_404(make_kobo):
    kobo = make_kobo()
    unknown = item_uuid(kobo.ctx.prefix, 999999)
    assert kobo.client.get(kobo.url(f"/download/{unknown}/kepub")).status_code == 404
    assert kobo.client.get(kobo.url(f"/v1/library/{unknown}/state")).status_code == 404


def test_malformed_state_is_400(make_kobo, sample):
    kobo = make_kobo()
    kobo.sync()
    uuid = item_uuid(kobo.ctx.prefix, 1105)
    assert kobo.client.put(kobo.url(f"/v1/library/{uuid}/state"), json={"nope": 1}).status_code == 400
    bad_status = {"ReadingStates": [{"StatusInfo": {"Status": "Bogus"}}]}
    assert kobo.client.put(kobo.url(f"/v1/library/{uuid}/state"), json=bad_status).status_code == 400


def test_status_page(make_kobo, sample):
    kobo = make_kobo()
    kobo.sync()
    page = kobo.client.get("/").text
    assert "Articles on the Kobo" in page
    assert "Offered again, never downloaded" in page
    assert '<time datetime="' in page and " UTC</time>" in page  # shown in the browser's time zone
    assert TOKEN not in page
