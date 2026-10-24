"""Application setup, status page and the `neneko` command."""

import asyncio
import html
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from .builder.extract import Fetcher
from .builder.queue import Builder
from .config import Settings
from .ids import instance_prefix
from .kobo.proxy import StoreProxy
from .kobo.routes import KoboContext, create_router
from .nextcloud import NewsClient
from .state import State
from .sync import SyncEngine

log = logging.getLogger("neneko")


def create_app(settings: Settings | None = None, *,
               news_transport: httpx.AsyncBaseTransport | None = None,
               store_transport: httpx.AsyncBaseTransport | None = None,
               fetch_transport: httpx.BaseTransport | None = None,
               start_workers: bool = True) -> FastAPI:
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    state = State(settings.db_path)
    news = NewsClient(settings.nc_url, settings.nc_user, settings.nc_app_password, transport=news_transport)
    fetcher = Fetcher(settings.cookies_file, user_agent=settings.user_agent, transport=fetch_transport)
    builder = Builder(settings, state, fetcher)
    prefix = instance_prefix(settings.nc_url, settings.nc_user)
    engine = SyncEngine(settings, state, news, builder, prefix)
    proxy = StoreProxy(settings.store_url, settings.store_proxy, transport=store_transport)
    check_cache: dict[str, tuple[float, tuple[bool, str, str]]] = {}

    async def nextcloud_check(max_age: float = 60) -> tuple[bool, str, str]:
        hit = check_cache.get("result")
        if hit and time.monotonic() - hit[0] < max_age:
            return hit[1]
        result = await news.check(settings.nc_folders)
        check_cache["result"] = (time.monotonic(), result)
        return result

    async def startup_check() -> None:
        ok, summary, hint = await nextcloud_check(max_age=0)
        if ok:
            log.info("Nextcloud News OK (%s): %s", settings.nc_url, summary)
        else:
            log.error("Nextcloud News NOT reachable at %s: %s", settings.nc_url, summary)
            log.error("Hint: %s", hint)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_workers:
            builder.start()
        log.info("Neneko %s ready; Kobo api_endpoint=%s/kobo/<token>", __version__, settings.public_url)
        check_task = asyncio.get_running_loop().create_task(startup_check())
        try:
            yield
        finally:
            check_task.cancel()
            builder.stop()
            await news.aclose()
            await proxy.aclose()
            fetcher.close()
            state.close()

    app = FastAPI(title="Neneko", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.neneko = KoboContext(settings, state, engine, builder, proxy, prefix)
    app.include_router(create_router(app.state.neneko))

    @app.get("/healthz")
    async def healthz(nextcloud: bool = False):
        """Process health. With ?nextcloud=1 also checks Nextcloud News (503 if it fails)."""
        body = {"status": "ok", "version": __version__}
        if not nextcloud:
            return JSONResponse(body)
        ok, summary, hint = await nextcloud_check(max_age=0)
        body["nextcloud"] = {"ok": ok, "summary": summary, "hint": hint}
        if not ok:
            body["status"] = "nextcloud unreachable"
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/", response_class=HTMLResponse)
    async def status_page():
        counts = state.ledger_counts()
        last_sync = state.kv_get("last_sync_at")
        last_error = state.kv_get("last_sync_error") or ""
        outbox = state.outbox_pending()
        nc_ok, nc_summary, nc_hint = await nextcloud_check()
        rows = [
            ("Nextcloud News", f"OK: {nc_summary}" if nc_ok else f"NOT OK: {nc_summary}"),
            *([("What to check", nc_hint)] if nc_hint else []),
            ("Last sync", _Time(float(last_sync)) if last_sync else "never"),
            ("Articles on the Kobo", counts["on_kobo"]),
            ("Downloaded by the Kobo", counts["downloaded"]),
            ("Being read", counts["reading"]),
            ("Finished (leaving after the grace period)", counts["finished"]),
            ("Waiting to be sent to Nextcloud", len(outbox)),
            ("Last Nextcloud error", last_error or "none"),
            ("KEPUB conversion", "kepubify" if builder.use_kepub else "off (plain EPUB)"),
        ]
        table = "".join(f"<tr><th>{html.escape(str(k))}</th><td>{_cell(v)}</td></tr>" for k, v in rows)
        return (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><title>Neneko</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem}"
            "th{text-align:left;padding:.3rem 1rem .3rem 0;font-weight:500}td{padding:.3rem 0}</style>"
            f"<h1>Neneko</h1><table>{table}</table><p><small>Version {__version__}</small></p>"
            # Times in the browser's time zone; the container usually runs in UTC.
            "<script>for(const t of document.querySelectorAll('time'))"
            "t.textContent=new Date(t.dateTime).toLocaleString()</script></html>"
        )

    return app


class _Time(float):
    """A Unix time shown on the status page."""


def _cell(value) -> str:
    if isinstance(value, _Time):
        moment = datetime.fromtimestamp(value, tz=timezone.utc)
        return (f'<time datetime="{moment.isoformat(timespec="seconds")}">'
                f'{moment.strftime("%Y-%m-%d %H:%M:%S")} UTC</time>')
    return html.escape(str(value))


class _RedactToken(logging.Filter):
    """Keep the Kobo token (part of every Kobo request path) out of the logs."""

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                arg.replace(self._token, "<token>") if isinstance(arg, str) else arg for arg in record.args)
        if isinstance(record.msg, str):
            record.msg = record.msg.replace(self._token, "<token>")
        return True


def run() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact = _RedactToken(settings.kobo_token)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)  # handler filters see records from every logger
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # log_config=None: uvicorn's loggers propagate to the root handler and its filter.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                log_level=settings.log_level.lower(), log_config=None, proxy_headers=False)


if __name__ == "__main__":
    run()
