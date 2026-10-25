"""The Kobo store endpoints Neneko implements, under /kobo/<token>/.

Request handling follows calibre-web's cps/kobo.py (GPL-3.0), which proved that a
stock Kobo accepts these responses.
"""

import base64
import json
import logging
import os
import secrets
import time
import uuid as uuid_lib
from dataclasses import dataclass
from importlib import resources

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ..builder.queue import Builder
from ..config import Settings
from ..ids import item_id_from_uuid
from ..state import State
from ..sync import SyncEngine
from . import payloads
from .proxy import StoreProxy
from .synctoken import HEADER as SYNC_TOKEN_HEADER
from .synctoken import SyncToken

log = logging.getLogger(__name__)

STORE_SYNC_HEADERS = ("x-kobo-sync", "x-kobo-sync-mode", "x-kobo-recent-reads")


@dataclass
class KoboContext:
    settings: Settings
    state: State
    engine: SyncEngine
    builder: Builder
    proxy: StoreProxy
    prefix: bytes


def _json(data, status_code: int = 200, headers: dict | None = None) -> Response:
    # Plain json.dumps, like calibre-web: the Kobo is picky about how Unicode is encoded.
    return Response(json.dumps(data), status_code=status_code, headers=headers,
                    media_type="application/json; charset=utf-8")


def _native_resources() -> dict:
    return json.loads(resources.files("neneko.kobo").joinpath("native_resources.json").read_text())


def create_router(ctx: KoboContext) -> APIRouter:
    settings, state, engine, builder, proxy = ctx.settings, ctx.state, ctx.engine, ctx.builder, ctx.proxy

    def check_token(token: str) -> None:
        if not secrets.compare_digest(token.encode(), settings.kobo_token.encode()):
            raise HTTPException(status_code=404)

    router = APIRouter(prefix="/kobo/{token}", dependencies=[Depends(check_token)])

    def ours(value: str) -> int | None:
        return item_id_from_uuid(ctx.prefix, value)

    def store_path(request: Request) -> str:
        """The request path without the /kobo/<token> prefix."""
        return request.url.path.split(f"/kobo/{settings.kobo_token}", 1)[1]

    @router.get("")
    async def top_level():
        return JSONResponse({})

    @router.get("/v1/initialization")
    async def initialization(request: Request):
        kobo_resources = None
        if proxy.enabled:
            try:
                upstream = await proxy.forward(request, "/v1/initialization")
                kobo_resources = upstream.json().get("Resources")
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                log.warning("store initialization failed, using built-in resources: %r", exc)
        if not kobo_resources:
            kobo_resources = _native_resources()
        base = settings.kobo_base_url
        kobo_resources["image_host"] = settings.public_url
        kobo_resources["image_url_template"] = f"{base}/{{ImageId}}/{{Width}}/{{Height}}/false/image.jpg"
        kobo_resources["image_url_quality_template"] = (
            f"{base}/{{ImageId}}/{{Width}}/{{Height}}/{{Quality}}/{{IsGreyscale}}/image.jpg")
        kobo_resources["library_sync"] = f"{base}/v1/library/sync"
        return _json({"Resources": kobo_resources}, headers={"x-kobo-apitoken": "e30="})

    @router.get("/v1/library/sync")
    async def library_sync(request: Request):
        token = SyncToken.parse(request.headers.get(SYNC_TOKEN_HEADER))
        response = await engine.library_sync(token)
        results, headers = response.results, {}
        if proxy.enabled and not response.more:
            try:
                upstream = await proxy.forward(
                    request, "/v1/library/sync", headers={SYNC_TOKEN_HEADER: token.store_token})
                if upstream.status_code == 200:
                    results = results + upstream.json()
                    response.token.store_token = upstream.headers.get(SYNC_TOKEN_HEADER, token.store_token)
                    for name in STORE_SYNC_HEADERS:
                        if upstream.headers.get(name):
                            headers[name] = upstream.headers[name]
                else:
                    log.warning("store library sync returned HTTP %s", upstream.status_code)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("store library sync failed: %r", exc)
        if response.more:
            headers["x-kobo-sync"] = "continue"
        headers[SYNC_TOKEN_HEADER] = response.token.serialize()
        return _json(results, headers=headers)

    @router.get("/v1/library/{ids}/metadata")
    async def library_metadata(ids: str, request: Request):
        rows = [state.ledger_get(item_id) for item_id in map(ours, ids.split(",")) if item_id is not None]
        rows = [row for row in rows if row and row.get("item_json")]
        if not rows:
            return await proxy.passthrough(request, store_path(request))
        return _json([engine.book_metadata(row) for row in rows])

    @router.api_route("/v1/library/{book_uuid}/state", methods=["GET", "PUT"])
    async def reading_state(book_uuid: str, request: Request):
        item_id = ours(book_uuid)
        if item_id is None:
            return await proxy.passthrough(request, store_path(request))
        if request.method == "GET":
            row = state.ledger_get(item_id)
            if row is None:
                raise HTTPException(status_code=404)
            return _json([payloads.reading_state(row)])
        try:
            data = await request.json()
            reading = data["ReadingStates"][0]
            engine.report_state(item_id, reading)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.info("malformed reading state for %s: %r", book_uuid, exc)
            raise HTTPException(status_code=400, detail="Malformed request data is missing 'ReadingStates' key") from exc
        engine.flush_soon()
        now = payloads.timestamp()
        result = {"EntitlementId": book_uuid, "LastModified": now, "PriorityTimestamp": now}
        if reading.get("CurrentBookmark"):
            result["CurrentBookmarkResult"] = {"Result": "Success"}
        if reading.get("Statistics"):
            result["StatisticsResult"] = {"Result": "Success"}
        if reading.get("StatusInfo"):
            result["StatusInfoResult"] = {"Result": "Success"}
        return _json({"RequestResult": "Success", "UpdateResults": [result]})

    @router.delete("/v1/library/{ids}")
    async def delete_books(ids: str, request: Request):
        uuids = ids.split(",")
        mine = [(value, ours(value)) for value in uuids]
        handled = [item_id for _, item_id in mine if item_id is not None]
        for item_id in handled:
            engine.delete_from_kobo(item_id)
            log.info("item %s deleted on the Kobo (%s)", item_id, settings.delete_action)
        if handled:
            engine.flush_soon()
        others = [value for value, item_id in mine if item_id is None]
        if others and proxy.enabled:
            return await proxy.passthrough(request, f"/v1/library/{','.join(others)}")
        return Response(status_code=204)

    @router.api_route("/download/{book_uuid}/{book_format}", methods=["GET", "HEAD"])
    async def download(book_uuid: str, book_format: str, request: Request):
        item_id = ours(book_uuid)
        row = state.ledger_get(item_id) if item_id is not None else None
        if row is None or not row.get("item_json"):
            raise HTTPException(status_code=404)
        started = time.monotonic()
        was_built = builder.article_path(row).exists()
        try:
            path = await builder.article(row)
        except Exception as exc:  # noqa: BLE001 - logged by the builder
            log.error("download of item %s failed: %r", item_id, exc)
            raise HTTPException(status_code=500) from exc
        # Headers as calibre-web and other Kobo servers send them.
        name = f"{item_id}.kepub" if builder.use_kepub else f"{item_id}.epub"
        headers = {"x-kobo-apitoken": "e30="}
        if request.method == "HEAD":
            log.info("download of item %s: size checked (HEAD)", item_id)
        else:
            state.ledger_set_downloaded(item_id)
            log.info("download of item %s: %s, ready after %.1f s", item_id,
                     "from cache" if was_built else "built on request", time.monotonic() - started)
        return FileResponse(path, media_type="application/octet-stream", filename=name, headers=headers)

    async def cover(book_uuid: str, width: str, height: str):
        item_id = ours(book_uuid)
        if item_id is None:
            if proxy.enabled:
                return RedirectResponse(
                    f"{settings.image_cdn_url}/{book_uuid}/{width}/{height}/false/image.jpg", status_code=307)
            raise HTTPException(status_code=404)
        row = state.ledger_get(item_id)
        if row is None or not row.get("item_json"):
            raise HTTPException(status_code=404)
        try:
            w, h = int(width), int(height)
        except ValueError:
            w, h = 355, 530
        data, media_type = await run_in_threadpool(builder.cover, row, w, h)
        return Response(data, media_type=media_type, headers={"Cache-Control": "max-age=86400"})

    @router.get("/{book_uuid}/{width}/{height}/{is_greyscale}/image.jpg")
    async def cover_image(book_uuid: str, width: str, height: str, is_greyscale: str):
        return await cover(book_uuid, width, height)

    @router.get("/{book_uuid}/{width}/{height}/{quality}/{is_greyscale}/image.jpg")
    async def cover_image_quality(book_uuid: str, width: str, height: str, quality: str, is_greyscale: str):
        return await cover(book_uuid, width, height)

    @router.post("/v1/auth/device")
    @router.post("/v1/auth/refresh")
    async def auth(request: Request):
        if proxy.enabled:
            try:
                upstream = await proxy.forward(request, store_path(request))
                if upstream.status_code < 400:
                    return Response(upstream.content, status_code=upstream.status_code,
                                    media_type=upstream.headers.get("content-type", "application/json"))
            except httpx.HTTPError as exc:
                log.warning("store auth failed, answering locally: %r", exc)
        try:
            user_key = (await request.json()).get("UserKey", "")
        except ValueError:
            user_key = ""
        return _json({
            "AccessToken": base64.b64encode(os.urandom(24)).decode(),
            "RefreshToken": base64.b64encode(os.urandom(24)).decode(),
            "TokenType": "Bearer",
            "TrackingId": str(uuid_lib.uuid4()),
            "UserKey": user_key,
        })

    @router.api_route("/v1/analytics/gettests", methods=["GET", "POST"])
    async def gettests(request: Request):
        if proxy.enabled:
            return await proxy.passthrough(request, store_path(request))
        return _json({"Result": "Success", "TestKey": request.headers.get("X-Kobo-userkey", ""), "Tests": {}})

    @router.get("/v1/user/loyalty/benefits")
    async def benefits(request: Request):
        if proxy.enabled:
            return await proxy.passthrough(request, store_path(request))
        return _json({"Benefits": {}})

    @router.api_route("/v1/products/{ids}/{action}", methods=["GET", "POST"])
    async def product_lookup(ids: str, action: str, request: Request):
        # Lookups like nextread/recommendations for our articles: the store doesn't
        # know them, so answer here instead of sending the IDs to Kobo. An empty
        # "next read" list is the truth; a 404 makes the Kobo log an error.
        if all(ours(value) is not None for value in ids.split(",")):
            if action == "nextread":
                return _json([])
            return _json({}, status_code=404)
        return await proxy.passthrough(request, store_path(request))

    def is_our_tag(tag_id: str) -> bool:
        return any(tag["uuid"] == tag_id for tag in state.tags_all())

    @router.post("/v1/library/tags")
    async def tag_create(request: Request):
        if proxy.enabled:
            return await proxy.passthrough(request, store_path(request))
        return _json(str(uuid_lib.uuid4()), status_code=201)

    @router.api_route("/v1/library/tags/{tag_id}", methods=["PUT", "DELETE"])
    @router.post("/v1/library/tags/{tag_id}/items")
    @router.post("/v1/library/tags/{tag_id}/items/delete")
    async def tag_change(tag_id: str, request: Request):
        if is_our_tag(tag_id):
            # Neneko's collections follow Nextcloud; changes made on the Kobo are overwritten
            # the next time the collection changes.
            return Response(status_code=200)
        if proxy.enabled:
            return await proxy.passthrough(request, store_path(request))
        return Response(status_code=200)

    @router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def everything_else(path: str, request: Request):
        return await proxy.passthrough(request, store_path(request))

    return router
