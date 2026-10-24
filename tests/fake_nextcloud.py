"""A small in-memory stand-in for the Nextcloud News v1-3 API."""

import base64
import itertools
import time

from fastapi import FastAPI, HTTPException, Request

API = "/index.php/apps/news/api/v1-3"


class FakeNews:
    def __init__(self, user: str = "reader", password: str = "app-password"):
        self.user, self.password = user, password
        self.folders: dict[int, str] = {}
        self.feeds: dict[int, dict] = {}
        self.items: dict[int, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.marks: list[tuple[str, list[int]]] = []
        self.down = False
        self._ids = itertools.count(1000)
        self.app = self._build_app()

    # setup helpers

    def add_folder(self, folder_id: int, name: str) -> None:
        self.folders[folder_id] = name

    def add_feed(self, feed_id: int, title: str, folder_id: int | None = None) -> None:
        self.feeds[feed_id] = {"id": feed_id, "title": title, "folderId": folder_id, "url": f"https://feed/{feed_id}"}

    def add_item(self, feed_id: int, *, item_id: int | None = None, title: str | None = None,
                 body: str = "<p>Body text.</p>", unread: bool = True, **extra) -> dict:
        item_id = item_id if item_id is not None else next(self._ids)
        item = {
            "id": item_id, "guid": f"guid-{item_id}", "guidHash": f"hash{item_id}",
            "url": f"https://example.org/articles/{item_id}", "title": title or f"Article {item_id}",
            "author": "Some Author", "pubDate": 1792700000 + item_id, "updatedDate": None, "body": body,
            "enclosureMime": None, "enclosureLink": None, "mediaThumbnail": None, "mediaDescription": None,
            "feedId": feed_id, "unread": unread, "starred": False, "filtered": False,
            "lastModified": int(time.time()), "rtl": False, "fingerprint": f"fp{item_id}",
            "contentHash": f"content{item_id}",
        }
        item.update(extra)
        self.items[item_id] = item
        return item

    def set_read(self, item_id: int, read: bool = True) -> None:
        self.items[item_id]["unread"] = not read
        self.items[item_id]["lastModified"] = int(time.time())

    def unread_ids(self) -> set[int]:
        return {i for i, item in self.items.items() if item["unread"]}

    # API

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        def check(request: Request) -> None:
            if self.down:
                raise HTTPException(status_code=503)
            expected = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            if request.headers.get("authorization") != expected:
                raise HTTPException(status_code=401, detail="Current user is not logged in")
            self.calls.append((request.method, request.url.path, dict(request.query_params)))

        @app.get(f"{API}/folders")
        async def folders(request: Request):
            check(request)
            return {"folders": [{"id": i, "name": n, "opened": True, "feeds": []} for i, n in self.folders.items()]}

        @app.get(f"{API}/feeds")
        async def feeds(request: Request):
            check(request)
            return {"feeds": list(self.feeds.values()), "starredCount": 0}

        @app.get(f"{API}/items")
        async def items(request: Request, type: int = 3, id: int = 0, getRead: str = "true",
                        batchSize: int = -1, offset: int = 0, oldestFirst: str = "false"):
            check(request)
            result = sorted(self.items.values(), key=lambda item: -item["id"])
            if type == 1:
                feed_ids = {fid for fid, feed in self.feeds.items() if feed["folderId"] == id}
                result = [item for item in result if item["feedId"] in feed_ids]
            elif type == 0:
                result = [item for item in result if item["feedId"] == id]
            if getRead == "false":
                result = [item for item in result if item["unread"]]
            if batchSize > 0:
                result = result[:batchSize]
            return {"items": result}

        @app.get(f"{API}/items/updated")
        async def updated(request: Request, type: int = 3, id: int = 0, lastModified: int = 0):
            check(request)
            return {"items": [item for item in self.items.values() if item["lastModified"] >= lastModified]}

        @app.post(f"{API}/items/read/multiple")
        async def read_multiple(request: Request):
            check(request)
            ids = (await request.json())["itemIds"]
            self.marks.append(("read", ids))
            for item_id in ids:
                if item_id in self.items:
                    self.set_read(item_id, True)

        @app.post(f"{API}/items/unread/multiple")
        async def unread_multiple(request: Request):
            check(request)
            ids = (await request.json())["itemIds"]
            self.marks.append(("unread", ids))
            for item_id in ids:
                if item_id in self.items:
                    self.set_read(item_id, False)

        return app
