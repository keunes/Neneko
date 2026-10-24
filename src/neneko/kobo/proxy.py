"""Passing requests Neneko doesn't handle on to the real Kobo store."""

import logging

import httpx
from fastapi import Request
from fastapi.responses import RedirectResponse, Response

log = logging.getLogger(__name__)

DROP_REQUEST_HEADERS = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
DROP_RESPONSE_HEADERS = {"connection", "content-encoding", "content-length", "transfer-encoding", "keep-alive"}


class StoreProxy:
    def __init__(self, store_url: str, enabled: bool, *, transport: httpx.AsyncBaseTransport | None = None):
        self.enabled = enabled
        self.store_url = store_url
        self._client = httpx.AsyncClient(
            base_url=store_url, timeout=httpx.Timeout(10, connect=2), follow_redirects=False, transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def url(self, path: str, query: str = "") -> str:
        return f"{self.store_url}/{path.lstrip('/')}" + (f"?{query}" if query else "")

    async def forward(self, request: Request, path: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
        outgoing = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST_HEADERS}
        if headers:
            outgoing.update(headers)
        return await self._client.request(
            request.method, f"/{path.lstrip('/')}", params=request.query_params,
            headers=outgoing, content=await request.body(),
        )

    async def passthrough(self, request: Request, path: str) -> Response:
        """calibre-web's behaviour: redirect GETs to the store, proxy everything else
        (the Kobo turns other methods into GET when it follows a redirect)."""
        if not self.enabled:
            return Response("{}", media_type="application/json")
        if request.method == "GET":
            return RedirectResponse(self.url(path, request.url.query), status_code=307)
        try:
            upstream = await self.forward(request, path)
        except httpx.HTTPError as exc:
            log.warning("store request %s %s failed: %r", request.method, path, exc)
            return Response("{}", media_type="application/json")
        headers = {k: v for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE_HEADERS}
        return Response(upstream.content, status_code=upstream.status_code, headers=headers)
