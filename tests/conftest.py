import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from neneko.config import Settings
from neneko.ids import item_id_from_uuid
from neneko.main import create_app

from .fake_nextcloud import FakeNews

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "test-token-0123456789abcdef"


def kepubify_binary() -> str | None:
    return os.environ.get("NENEKO_TEST_KEPUBIFY") or shutil.which("kepubify")


def no_network(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, text="not found")


def fake_store(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/initialization":
        resources = json.loads((Path(__file__).parents[1] / "src/neneko/kobo/native_resources.json").read_text())
        return httpx.Response(200, json={"Resources": resources})
    if request.url.path == "/v1/library/sync":
        return httpx.Response(200, json=[], headers={"x-kobo-synctoken": "store.token"})
    return httpx.Response(200, json={})


@pytest.fixture
def news() -> FakeNews:
    fake = FakeNews()
    fake.add_folder(1, "Tech")
    fake.add_folder(2, "Long reads")
    fake.add_feed(51, "Example Tech Daily", folder_id=1)
    fake.add_feed(43, "Smart Home Notes", folder_id=1)
    fake.add_feed(35, "Podcast Weekly", folder_id=2)
    fake.add_feed(60, "Loose feed", folder_id=None)
    return fake


@pytest.fixture
def make_settings(tmp_path):
    def factory(**overrides) -> Settings:
        values = dict(
            nc_url="https://cloud.example.org", nc_user="reader", nc_app_password="app-password",
            public_url="http://neneko.local:8089", kobo_token=TOKEN, data_dir=tmp_path / "data",
            store_proxy=False, kepubify_path=kepubify_binary() or "kepubify-missing", build_workers=2,
        )
        values.update(overrides)
        return Settings(**values)
    return factory


class Kobo:
    """Plays the Kobo: keeps the sync token between syncs, like the device does."""

    def __init__(self, client: TestClient):
        self.client = client
        self.token: str | None = None
        self.pages = 0

    def url(self, path: str) -> str:
        return f"/kobo/{TOKEN}{path}"

    def sync(self, *, keep_token: bool = True, download: int | None = None) -> list[dict]:
        """One press of Sync: follows "continue" pages. Afterwards the Kobo downloads `download`
        of the offered books (all by default), recorded as a download request would."""
        results = []
        while True:
            headers = {"x-kobo-synctoken": self.token} if self.token else {}
            response = self.client.get(self.url("/v1/library/sync"), headers=headers)
            assert response.status_code == 200, response.text
            self.pages += 1
            results += response.json()
            if keep_token:
                self.token = response.headers["x-kobo-synctoken"]
            if response.headers.get("x-kobo-sync") != "continue":
                break
        if keep_token:
            offered = [r["NewEntitlement"]["BookEntitlement"]["Id"] for r in results if "NewEntitlement" in r]
            for book_id in offered if download is None else offered[:download]:
                item_id = item_id_from_uuid(self.ctx.prefix, book_id)
                if item_id is not None:
                    self.ctx.state.ledger_set_downloaded(item_id)
        return results

    def put_state(self, book_uuid: str, status: str, **extra) -> httpx.Response:
        body = {"ReadingStates": [{"StatusInfo": {"Status": status}, "CurrentBookmark": extra.get("bookmark"),
                                   "Statistics": extra.get("statistics")}]}
        return self.client.put(self.url(f"/v1/library/{book_uuid}/state"), json=body)


@pytest.fixture
def make_kobo(make_settings, news):
    clients = []

    def factory(*, store=None, fetch=None, start_workers=False, **overrides):
        settings = make_settings(**overrides)
        app = create_app(
            settings,
            news_transport=httpx.ASGITransport(app=news.app),
            store_transport=httpx.MockTransport(store or fake_store),
            fetch_transport=httpx.MockTransport(fetch or no_network),
            start_workers=start_workers,
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        kobo = Kobo(client)
        kobo.app = app
        kobo.ctx = app.state.neneko
        return kobo

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def entitlements(results: list[dict], kind: str) -> list[dict]:
    return [r[kind] for r in results if kind in r]


def ids_of(results: list[dict], kind: str, *, removed: bool | None = None) -> list[str]:
    out = []
    for entry in entitlements(results, kind):
        if removed is None or entry["BookEntitlement"]["IsRemoved"] == removed:
            out.append(entry["BookEntitlement"]["Id"])
    return out
