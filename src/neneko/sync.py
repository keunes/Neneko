"""What goes onto the Kobo, what comes off it, and read state back to Nextcloud."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from .builder.queue import Builder
from .config import Settings
from .ids import item_uuid, tag_uuid
from .kobo import payloads
from .kobo.synctoken import SyncToken
from .nextcloud import NewsClient, NextcloudError
from .state import State

log = logging.getLogger(__name__)

KOBO_STATUSES = {"ReadyToRead", "Reading", "Finished"}
FORGET_REMOVED_AFTER = 30 * 86400
SYNC_BUILD_WAIT = 10  # seconds a sync waits for new articles to be built
# How often an article the Kobo never downloaded is offered again. Tracked in the ledger to see
# whether the limit is ever reached; see docs/kobo-notes.md, "Temporary: re-offer tracking".
REOFFER_LIMIT = 3


def _modified_at(item: dict) -> float:
    """An item's lastModified as a Unix time (older News versions send seconds, newer microseconds)."""
    try:
        value = float(item.get("lastModified") or 0)
    except (TypeError, ValueError):
        return 0.0
    return value / 1_000_000 if value > 100_000_000_000 else value


@dataclass
class SyncResponse:
    results: list[dict] = field(default_factory=list)
    token: SyncToken = field(default_factory=SyncToken)
    more: bool = False


class SyncEngine:
    def __init__(self, settings: Settings, state: State, news: NewsClient, builder: Builder, prefix: bytes):
        self.settings = settings
        self.state = state
        self.news = news
        self.builder = builder
        self.prefix = prefix
        self._sync_lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()

    # payload helpers

    def download_url(self, row: dict) -> str:
        fmt = "kepub" if self.builder.use_kepub else "epub"
        return f"{self.settings.kobo_base_url}/download/{row['uuid']}/{fmt}"

    def book_metadata(self, row: dict) -> dict:
        size = row.get("size") or 0
        if not size:
            # Estimate until the article is built; the Kobo uses it for progress only.
            size = 60_000 + len(row.get("item_json") or "")
        return payloads.metadata(
            row, download_url=self.download_url(row),
            download_format="KEPUB" if self.builder.use_kepub else "EPUB3",
            size=size, language=self.settings.language,
        )

    def _book(self, kind: str, row: dict, *, removed: bool = False) -> dict:
        entry = {
            "BookEntitlement": payloads.entitlement(row, removed=removed),
            "BookMetadata": self.book_metadata(row),
        }
        if kind == "NewEntitlement":
            # calibre-web sends every new book with its reading state; the Kobo then has the
            # timestamps it sorts its download queue by (it asks for PrioritizeRecentReads).
            entry["ReadingState"] = payloads.reading_state(row)
        return {kind: entry}

    # Nextcloud side

    async def _window(self) -> tuple[list[dict], dict[int, dict], dict[int, str]]:
        """Newest unread items in scope, plus feeds by ID and folder names by ID."""
        folders = await self.news.folders()
        feeds = {feed["id"]: feed for feed in await self.news.feeds()}
        folder_names = {folder["id"]: folder["name"] for folder in folders}
        batch = min(max(self.settings.max_items * 2, 20), 500)
        if not self.settings.nc_folders:
            items = await self.news.unread_items(folder_id=None, batch_size=batch)
        else:
            wanted = {name.casefold() for name in self.settings.nc_folders}
            folder_ids = [fid for fid, name in folder_names.items() if name.casefold() in wanted]
            missing = wanted - {folder_names[fid].casefold() for fid in folder_ids}
            if missing:
                log.warning("NENEKO_NC_FOLDERS names not found in Nextcloud News: %s", ", ".join(sorted(missing)))
            items = []
            for folder_id in folder_ids:
                items += await self.news.unread_items(folder_id=folder_id, batch_size=batch)
        unique = {item["id"]: item for item in items if item.get("unread", True) and not item.get("filtered")}
        return sorted(unique.values(), key=lambda item: -item["id"]), feeds, folder_names

    def _in_scope(self, item: dict, feeds: dict[int, dict], folder_names: dict[int, str]) -> bool:
        if not self.settings.nc_folders:
            return True
        feed = feeds.get(item.get("feedId"))
        folder = folder_names.get(feed.get("folderId")) if feed else None
        return folder is not None and folder.casefold() in {n.casefold() for n in self.settings.nc_folders}

    async def flush_outbox(self, timeout: float | None = None) -> bool:
        """Send queued read/unread changes. Returns True if the outbox is empty afterwards."""
        async with self._flush_lock:
            pending = self.state.outbox_pending()
            if not pending:
                return True
            ok = True
            for action in ("read", "unread"):
                rows = [row for row in pending if row["action"] == action]
                if not rows:
                    continue
                try:
                    await self.news.mark([row["item_id"] for row in rows], read=action == "read", timeout=timeout)
                except NextcloudError as exc:
                    log.warning("could not mark %d items %s in Nextcloud: %s", len(rows), action, exc)
                    self.state.outbox_failed([row["id"] for row in rows], str(exc))
                    ok = False
                else:
                    self.state.outbox_done([row["id"] for row in rows])
            return ok

    def flush_soon(self) -> None:
        asyncio.get_running_loop().create_task(self.flush_outbox(timeout=5))

    async def _unread_beats_kobo_removal(self) -> None:
        """The Kobo reports a removal only at the next Sync, with no time of its own. An article
        marked unread in Nextcloud since the previous Sync is the newer wish: drop the pending
        "read" from the removal, so the article is delivered again."""
        dismissed = self.state.ledger_dismissed_ids()
        pending = [row["item_id"] for row in self.state.outbox_pending()
                   if row["action"] == "read" and row["item_id"] in dismissed]
        last_sync = self.state.kv_get("last_sync_at")
        if not pending or not last_sync:
            return
        try:
            changed = await self.news.items_updated_since(int(float(last_sync)))
        except NextcloudError as exc:
            log.warning("could not check articles removed on the Kobo: %s", exc)
            return
        unread = {item["id"] for item in changed if item.get("unread")}
        for item_id in pending:
            if item_id in unread:
                self.state.outbox_cancel(item_id)
                self.state.ledger_undismiss(item_id)
                log.info("item %s: removed on the Kobo but marked unread in Nextcloud since the last sync; "
                         "keeping it unread", item_id)

    # library sync

    async def library_sync(self, token: SyncToken) -> SyncResponse:
        async with self._sync_lock:
            return await self._library_sync(token)

    async def _library_sync(self, token: SyncToken) -> SyncResponse:
        settings, state = self.settings, self.state
        epoch = state.epoch()
        acked = token.gen if token.epoch == epoch else 0
        recovering = token.epoch is not None and token.epoch != epoch
        gen = state.next_gen()
        now = time.time()
        response = SyncResponse(token=SyncToken(epoch=epoch, gen=gen, store_token=token.store_token))
        results = response.results

        await self._unread_beats_kobo_removal()
        await self.flush_outbox()
        try:
            window, feeds, folder_names = await self._window()
        except NextcloudError as exc:
            log.error("Nextcloud News unavailable, syncing without changes: %s", exc)
            state.kv_set("last_sync_error", f"{time.strftime('%Y-%m-%d %H:%M:%S')} {exc}")
            window = None
            feeds, folder_names = {}, {}

        active = {row["item_id"]: row for row in state.ledger_active()}
        sent: set[int] = set()
        to_build: list[int] = []

        if window is not None:
            by_id = {item["id"]: item for item in window}
            dismissed = state.ledger_dismissed_ids()
            grace = settings.finished_grace_hours * 3600
            keep_always, keep_finished, candidates = set(), set(), set()
            for item_id, row in active.items():
                status = row["kobo_status"]
                if status == "Reading":
                    keep_always.add(item_id)
                elif status == "Finished":
                    if (row["finished_at"] or now) + grace > now:
                        keep_finished.add(item_id)
                elif item_id in by_id or row["nc_read_by_neneko"]:
                    candidates.add(item_id)
            # An article removed on the Kobo comes back only if it changed in Nextcloud since
            # (e.g. marked unread again); until then it is just Neneko's own mark-read on its way.
            candidates |= {item_id for item_id, item in by_id.items() if item_id not in active
                           and (item_id not in dismissed or _modified_at(item) > dismissed[item_id])}
            slots = max(0, settings.max_items - len(keep_always))
            chosen = sorted(candidates, reverse=True)[:slots]
            target = set(chosen) | keep_always | keep_finished

            # The Kobo downloads at most 5 books per sync page by itself (MaxDownload), so new
            # articles go out a page at a time; "continue" makes it ask for the next page.
            # Unacknowledged new books are sent again below and count towards the page.
            new_ids = [item_id for item_id in chosen if item_id not in active]
            unacked = sum(1 for item_id in target & active.keys() if active[item_id]["delivered_gen"] > acked)
            page = max(settings.new_per_sync_page - unacked, 0)
            response.more = len(new_ids) > page
            new_ids = new_ids[:page]

            with state.transaction():
                for item_id in new_ids:
                    item = by_id[item_id]
                    feed = feeds.get(item.get("feedId"), {})
                    state.ledger_deliver(
                        item, uuid=item_uuid(self.prefix, item_id), folder_id=feed.get("folderId") or None,
                        feed_title=feed.get("title") or "", gen=gen, size=0,
                    )
                    if settings.mark_read_on == "delivered":
                        state.outbox_add(item_id, "read")
                        state.ledger_set_read_by_neneko(item_id, True)
                    to_build.append(item_id)
                for item_id in target & active.keys():
                    item = by_id.get(item_id)
                    # Only replace articles that haven't been opened: a new file resets the position.
                    if item and item.get("contentHash") != active[item_id]["content_hash"] \
                            and active[item_id]["kobo_status"] == "ReadyToRead":
                        feed = feeds.get(item.get("feedId"), {})
                        self.builder.purge(item_id)
                        state.ledger_update_content(
                            item, folder_id=feed.get("folderId") or None,
                            feed_title=feed.get("title") or active[item_id]["feed_title"] or "", gen=gen, size=0,
                        )
                        to_build.append(item_id)
                for item_id in active.keys() - target:
                    state.ledger_remove(item_id, gen)

            # Build before answering (they take about a second each, in parallel), so the sync can
            # report each file's real size, like calibre-web does; slow ones keep an estimate.
            if to_build:
                await self.builder.wait([state.ledger_get(item_id) for item_id in to_build], SYNC_BUILD_WAIT)
            for item_id in new_ids:
                results.append(self._book("NewEntitlement", state.ledger_get(item_id)))
                sent.add(item_id)
            for item_id in to_build:
                if item_id not in sent:
                    results.append(self._book("ChangedEntitlement", state.ledger_get(item_id)))
                    sent.add(item_id)
            for item_id in active.keys() - target:
                results.append(self._book("ChangedEntitlement", state.ledger_get(item_id), removed=True))
                sent.add(item_id)

        # Re-send what the Kobo hasn't acknowledged (a response it never received).
        for row in state.ledger_active():
            if row["item_id"] in sent:
                continue
            if row["delivered_gen"] > acked:
                results.append(self._book("NewEntitlement", row))
                sent.add(row["item_id"])
                state.ledger_redelivered(row["item_id"], gen)  # delivered in this Sync: not re-offered
            elif row["changed_gen"] > acked:
                results.append(self._book("ChangedEntitlement", row))
        for row in state.ledger_removed():
            if row["item_id"] in sent or row["dismissed"]:
                continue
            if row["removed_gen"] > acked and row["item_json"]:
                results.append(self._book("ChangedEntitlement", row, removed=True))
            elif row["item_json"]:
                state.ledger_purge_item_json(row["item_id"])
                self.builder.purge(row["item_id"])
        state.ledger_delete_older_than(now - FORGET_REMOVED_AFTER)

        # Offer articles the Kobo has but never downloaded again, in the room left on this page.
        # The token tracks one Sync's pages: where it started (articles delivered since can't have
        # been downloaded yet) and how far the re-offer got.
        continuing = token.session is not None and token.epoch == epoch
        session_start = token.session if continuing else gen
        resend_below = token.resend_below if continuing else None
        budget = max(settings.new_per_sync_page - sum("NewEntitlement" in r for r in results), 0)
        undownloaded = [
            row for row in state.ledger_active()
            if row["item_id"] not in sent and not row["downloaded_at"] and row["delivered_gen"] <= acked
            and row["delivered_gen"] < session_start and row["kobo_status"] == "ReadyToRead"
            and row["reoffer_count"] < REOFFER_LIMIT
            and (resend_below is None or row["item_id"] < resend_below)
        ]
        reoffered = undownloaded[:budget]
        for row in reoffered:
            results.append(self._book("NewEntitlement", row))
            sent.add(row["item_id"])
        state.ledger_mark_reoffered([row["item_id"] for row in reoffered])
        if len(undownloaded) > len(reoffered):
            response.more = True
        if response.more:
            response.token.session = session_start
            response.token.resend_below = reoffered[-1]["item_id"] if reoffered else resend_below

        if recovering and window is not None:
            results += await self._recovery_sweep(feeds, folder_names)

        results += self._sync_tags(gen, acked, feeds, folder_names, recovering)

        if to_build:
            self.builder.enqueue([state.ledger_get(item_id) for item_id in to_build])
        if state.outbox_pending():
            self.flush_soon()  # e.g. NENEKO_MARK_READ_ON=delivered
        state.kv_set("last_sync_at", str(now))
        if window is not None:
            state.kv_set("last_sync_error", "")
        log.info("sync: %d new, %d re-offered (not downloaded yet), %d changed/removed, %d results in total%s",
                 sum("NewEntitlement" in r for r in results) - len(reoffered), len(reoffered),
                 len([r for r in results if "ChangedEntitlement" in r]), len(results),
                 " (more to come)" if response.more else "")
        return response

    async def _recovery_sweep(self, feeds: dict[int, dict], folder_names: dict[int, str]) -> list[dict]:
        """The database was reset since the Kobo last synced. Remove articles that
        were read recently; the Kobo ignores removals of books it doesn't have."""
        since = int(time.time() - self.settings.recovery_days * 86400)
        try:
            items = await self.news.items_updated_since(since)
        except NextcloudError as exc:
            log.warning("recovery sweep skipped: %s", exc)
            return []
        on_kobo = {row["item_id"] for row in self.state.ledger_active()}
        read = [item for item in items
                if not item.get("unread") and item["id"] not in on_kobo and self._in_scope(item, feeds, folder_names)]
        # Only the most recent ones can have been on the Kobo; this keeps the response small.
        limit = max(4 * self.settings.max_items, 200)
        results = []
        for item in sorted(read, key=lambda i: -i["id"])[:limit]:
            feed = feeds.get(item.get("feedId"), {})
            row = {
                "item_id": item["id"], "uuid": item_uuid(self.prefix, item["id"]),
                "feed_title": feed.get("title") or "", "title": item.get("title"),
                "item_json": json.dumps(item), "delivered_at": None,
            }
            results.append(self._book("ChangedEntitlement", row, removed=True))
        log.info("recovery sweep: %d removals sent", len(results))
        return results

    def _desired_tags(self, folder_names: dict[int, str]) -> dict[str, tuple[str, str, str, list[str]]]:
        settings = self.settings
        rows = self.state.ledger_active()
        desired: dict[str, tuple[str, str, str, list[str]]] = {}
        if not rows:
            return desired
        if "all" in settings.collections:
            desired[tag_uuid(self.prefix, "all", "all")] = (
                "all", "all", settings.collection_all_name, [row["uuid"] for row in rows])
        if "folder" in settings.collections:
            by_folder: dict[int, list[str]] = {}
            for row in rows:
                if row["folder_id"]:
                    by_folder.setdefault(row["folder_id"], []).append(row["uuid"])
            for folder_id, uuids in by_folder.items():
                name = folder_names.get(folder_id)
                if name is None:
                    existing = self._existing_tag_name("folder", folder_id)
                    if existing is None:
                        continue
                    desired[tag_uuid(self.prefix, "folder", folder_id)] = ("folder", str(folder_id), existing, uuids)
                    continue
                desired[tag_uuid(self.prefix, "folder", folder_id)] = (
                    "folder", str(folder_id), settings.collection_folder_template.format(folder=name), uuids)
        if "feed" in settings.collections:
            by_feed: dict[int, tuple[str, int | None, list[str]]] = {}
            for row in rows:
                if row["feed_id"] is None:
                    continue
                entry = by_feed.setdefault(row["feed_id"], (row["feed_title"] or "", row["folder_id"], []))
                entry[2].append(row["uuid"])
            for feed_id, (title, folder_id, uuids) in by_feed.items():
                name = settings.collection_feed_template.format(
                    feed=title or f"Feed {feed_id}", folder=folder_names.get(folder_id, "") if folder_id else "")
                desired[tag_uuid(self.prefix, "feed", feed_id)] = ("feed", str(feed_id), name, uuids)
        return desired

    def _existing_tag_name(self, kind: str, ref) -> str | None:
        for tag in self.state.tags_all():
            if tag["kind"] == kind and tag["ref"] == str(ref) and not tag["deleted_at"]:
                return tag["name"]
        return None

    def _sync_tags(self, gen: int, acked: int, feeds: dict, folder_names: dict, recovering: bool) -> list[dict]:
        state = self.state
        desired = self._desired_tags(folder_names)
        existing = {tag["uuid"]: tag for tag in state.tags_all()}
        results = []
        for uuid, (kind, ref, name, items) in desired.items():
            tag = existing.get(uuid)
            if tag is None or tag["deleted_at"]:
                state.tag_upsert(uuid=uuid, kind=kind, ref=ref, name=name, items=items, gen=gen, created=True)
                kind_key = "NewTag"
            elif tag["name"] != name or json.loads(tag["items_json"]) != items:
                state.tag_upsert(uuid=uuid, kind=kind, ref=ref, name=name, items=items, gen=gen, created=False)
                kind_key = "NewTag" if tag["created_gen"] > acked else "ChangedTag"
            elif tag["sent_gen"] > acked:
                state.tag_resent(uuid, gen)
                kind_key = "NewTag" if tag["created_gen"] > acked else "ChangedTag"
            else:
                continue
            stored = next(t for t in state.tags_all() if t["uuid"] == uuid)
            results.append({kind_key: payloads.tag(uuid=uuid, name=name, items=items,
                                                   created=stored["created_at"], modified=stored["modified_at"])})
        for uuid, tag in existing.items():
            if uuid in desired:
                continue
            if tag["deleted_at"] is None:
                state.tag_delete(uuid, gen)
                results.append({"DeletedTag": payloads.deleted_tag(uuid=uuid, modified=time.time())})
            elif tag["sent_gen"] > acked:
                state.tag_resent(uuid, gen)
                results.append({"DeletedTag": payloads.deleted_tag(uuid=uuid, modified=tag["deleted_at"])})
            else:
                state.tag_purge(uuid)
        if recovering:
            # Collections from before the reset that are no longer wanted.
            candidates = {tag_uuid(self.prefix, "all", "all")}
            candidates |= {tag_uuid(self.prefix, "folder", fid) for fid in folder_names}
            candidates |= {tag_uuid(self.prefix, "feed", fid) for fid in feeds}
            for uuid in candidates - desired.keys() - existing.keys():
                results.append({"DeletedTag": payloads.deleted_tag(uuid=uuid, modified=time.time())})
        return results

    # reading state and deletion

    def report_state(self, item_id: int, reading_state: dict) -> None:
        """Apply a reading state PUT from the Kobo; queue read/unread for Nextcloud."""
        status_info = reading_state.get("StatusInfo") or {}
        status = status_info.get("Status")
        if status is not None and status not in KOBO_STATUSES:
            raise ValueError(f"unknown reading status {status!r}")
        bookmark = reading_state.get("CurrentBookmark")
        statistics = reading_state.get("Statistics")
        try:
            previous = self.state.ledger_set_reading_state(
                item_id, status=status, bookmark=bookmark, statistics=statistics)
            old = previous["kobo_status"]
        except KeyError:
            old = None  # not in the ledger (e.g. after a reset): act on the status alone
        if status is None or status == old:
            return
        mode = self.settings.mark_read_on
        if status == "Finished" or (status == "Reading" and mode == "opened" and old in (None, "ReadyToRead")):
            self.state.outbox_add(item_id, "read")
            self.state.ledger_set_read_by_neneko(item_id, True)
        elif status == "ReadyToRead" and old is not None:
            self.state.outbox_add(item_id, "unread")
            self.state.ledger_set_read_by_neneko(item_id, False)
        log.info("item %s: %s -> %s on the Kobo", item_id, old, status)

    def delete_from_kobo(self, item_id: int) -> None:
        if self.settings.delete_action == "mark_read":
            self.state.outbox_add(item_id, "read")
        self.state.ledger_dismiss(item_id)
        self.state.ledger_purge_item_json(item_id)
        self.builder.purge(item_id)
