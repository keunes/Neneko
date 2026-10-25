"""SQLite state: what is on the Kobo (ledger), pending writes to Nextcloud (outbox),
the collections sent to the Kobo (tags), and a few counters (kv).

This is not a mirror of Nextcloud News. Every row can be rebuilt by a sync.
"""

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    item_id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    feed_id INTEGER,
    folder_id INTEGER,
    feed_title TEXT,
    title TEXT,
    content_hash TEXT,
    item_json TEXT,
    size INTEGER,
    delivered_at REAL,
    delivered_gen INTEGER NOT NULL DEFAULT 0,
    changed_gen INTEGER NOT NULL DEFAULT 0,
    removed_at REAL,
    removed_gen INTEGER NOT NULL DEFAULT 0,
    kobo_status TEXT NOT NULL DEFAULT 'ReadyToRead',
    status_at REAL,
    finished_at REAL,
    times_started INTEGER NOT NULL DEFAULT 0,
    last_started_at REAL,
    bookmark_json TEXT,
    statistics_json TEXT,
    state_at REAL,
    nc_read_by_neneko INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0,
    downloaded_at REAL,
    reoffer_count INTEGER NOT NULL DEFAULT 0,
    last_reoffered_at REAL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('read', 'unread')),
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS tags (
    uuid TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    name TEXT NOT NULL,
    items_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    created_gen INTEGER NOT NULL,
    modified_at REAL NOT NULL,
    sent_gen INTEGER NOT NULL,
    deleted_at REAL
);
"""


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            self._db.executescript(SCHEMA)
            if 0 < version < 2:
                self._db.execute("ALTER TABLE ledger ADD COLUMN downloaded_at REAL")
            if 0 < version < 3:
                self._db.execute("ALTER TABLE ledger ADD COLUMN reoffer_count INTEGER NOT NULL DEFAULT 0")
                self._db.execute("ALTER TABLE ledger ADD COLUMN last_reoffered_at REAL")
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

    def _all(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, params)]

    def _one(self, sql: str, params=()) -> dict | None:
        with self._lock:
            row = self._db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    def transaction(self):
        return _Transaction(self)

    # kv

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def epoch(self) -> str:
        """Random ID of this database. A sync token from another epoch means the
        database was reset since the Kobo last synced."""
        with self._lock:
            value = self.kv_get("epoch")
            if value is None:
                value = secrets.token_hex(8)
                self.kv_set("epoch", value)
            return value

    def next_gen(self) -> int:
        """Generation number of a sync response. The Kobo echoes the last one it
        received in its sync token, which tells us what it has acknowledged."""
        with self._lock:
            gen = int(self.kv_get("gen", "0")) + 1
            self.kv_set("gen", str(gen))
            return gen

    # ledger

    def ledger_get(self, item_id: int) -> dict | None:
        return self._one("SELECT * FROM ledger WHERE item_id = ?", (item_id,))

    def ledger_active(self) -> list[dict]:
        return self._all("SELECT * FROM ledger WHERE removed_at IS NULL ORDER BY item_id DESC")

    def ledger_removed(self) -> list[dict]:
        return self._all("SELECT * FROM ledger WHERE removed_at IS NOT NULL ORDER BY item_id DESC")

    def ledger_deliver(self, item: dict, *, uuid: str, folder_id: int | None, feed_title: str,
                       gen: int, size: int) -> None:
        """Insert or reset a row for an item being sent to the Kobo as new."""
        now = time.time()
        self._exec(
            """
            INSERT INTO ledger (item_id, uuid, feed_id, folder_id, feed_title, title, content_hash,
                                item_json, size, delivered_at, delivered_gen, changed_gen)
            VALUES (:item_id, :uuid, :feed_id, :folder_id, :feed_title, :title, :content_hash,
                    :item_json, :size, :now, :gen, :gen)
            ON CONFLICT(item_id) DO UPDATE SET
                feed_id = excluded.feed_id, folder_id = excluded.folder_id,
                feed_title = excluded.feed_title, title = excluded.title,
                content_hash = excluded.content_hash, item_json = excluded.item_json,
                size = excluded.size, delivered_at = excluded.delivered_at,
                delivered_gen = excluded.delivered_gen, changed_gen = excluded.changed_gen,
                removed_at = NULL, removed_gen = 0, kobo_status = 'ReadyToRead',
                status_at = NULL, finished_at = NULL, bookmark_json = NULL,
                statistics_json = NULL, state_at = NULL, nc_read_by_neneko = 0, dismissed = 0,
                downloaded_at = NULL, reoffer_count = 0, last_reoffered_at = NULL
            """,
            {
                "item_id": item["id"], "uuid": uuid, "feed_id": item.get("feedId"),
                "folder_id": folder_id, "feed_title": feed_title, "title": item.get("title"),
                "content_hash": item.get("contentHash"), "item_json": json.dumps(item),
                "size": size, "now": now, "gen": gen,
            },
        )

    def ledger_update_content(self, item: dict, *, folder_id: int | None, feed_title: str,
                              gen: int, size: int) -> None:
        self._exec(
            "UPDATE ledger SET folder_id = ?, feed_title = ?, title = ?, content_hash = ?, "
            "item_json = ?, size = ?, changed_gen = ? WHERE item_id = ?",
            (folder_id, feed_title, item.get("title"), item.get("contentHash"),
             json.dumps(item), size, gen, item["id"]),
        )

    def ledger_set_downloaded(self, item_id: int) -> None:
        self._exec("UPDATE ledger SET downloaded_at = ? WHERE item_id = ? AND downloaded_at IS NULL",
                   (time.time(), item_id))

    def ledger_redelivered(self, item_id: int, gen: int) -> None:
        """A new book sent again because the Kobo didn't acknowledge it."""
        self._exec("UPDATE ledger SET delivered_gen = ? WHERE item_id = ?", (gen, item_id))

    def ledger_mark_reoffered(self, item_ids: list[int]) -> None:
        now = time.time()
        with self._lock:
            self._db.executemany(
                "UPDATE ledger SET reoffer_count = reoffer_count + 1, last_reoffered_at = ? WHERE item_id = ?",
                [(now, item_id) for item_id in item_ids],
            )

    def ledger_set_size(self, item_id: int, size: int) -> None:
        self._exec("UPDATE ledger SET size = ? WHERE item_id = ?", (size, item_id))

    def ledger_remove(self, item_id: int, gen: int) -> None:
        self._exec(
            "UPDATE ledger SET removed_at = ?, removed_gen = ? WHERE item_id = ?",
            (time.time(), gen, item_id),
        )

    def ledger_dismiss(self, item_id: int) -> None:
        """The Kobo deleted this article; don't send it again unless it changes in Nextcloud."""
        self._exec(
            "UPDATE ledger SET removed_at = COALESCE(removed_at, ?), removed_gen = 0, dismissed = 1 "
            "WHERE item_id = ?",
            (time.time(), item_id),
        )

    def ledger_undismiss(self, item_id: int) -> None:
        self._exec("UPDATE ledger SET dismissed = 0 WHERE item_id = ?", (item_id,))

    def ledger_dismissed_ids(self) -> dict[int, float]:
        """Articles deleted on the Kobo, with when they left it."""
        rows = self._all("SELECT item_id, removed_at FROM ledger WHERE dismissed = 1")
        return {row["item_id"]: row["removed_at"] or 0.0 for row in rows}

    def ledger_purge_item_json(self, item_id: int) -> None:
        self._exec("UPDATE ledger SET item_json = NULL WHERE item_id = ?", (item_id,))

    def ledger_delete_older_than(self, cutoff: float) -> int:
        # Rows that were offered again are kept (temporary, added in 0.2.1-dev.5): they show whether
        # the re-offer limit is ever reached. What to check before removing this: docs/kobo-notes.md,
        # "Temporary: re-offer tracking".
        return self._exec(
            "DELETE FROM ledger WHERE removed_at IS NOT NULL AND removed_at < ? AND reoffer_count = 0",
            (cutoff,),
        ).rowcount

    def ledger_set_read_by_neneko(self, item_id: int, value: bool) -> None:
        self._exec("UPDATE ledger SET nc_read_by_neneko = ? WHERE item_id = ?", (int(value), item_id))

    def ledger_set_reading_state(self, item_id: int, *, status: str | None,
                                 bookmark: dict | None, statistics: dict | None) -> dict:
        """Store a reading state reported by the Kobo. Returns the previous row."""
        with self._lock:
            row = self.ledger_get(item_id)
            if row is None:
                raise KeyError(item_id)
            now = time.time()
            fields: dict = {"state_at": now}
            if bookmark is not None:
                fields["bookmark_json"] = json.dumps(bookmark)
            if statistics is not None:
                fields["statistics_json"] = json.dumps(statistics)
            if status is not None and status != row["kobo_status"]:
                fields["kobo_status"] = status
                fields["status_at"] = now
                if status == "Finished":
                    fields["finished_at"] = now
                elif status == "Reading":
                    fields["times_started"] = row["times_started"] + 1
                    fields["last_started_at"] = now
            assignments = ", ".join(f"{key} = :{key}" for key in fields)
            self._exec(f"UPDATE ledger SET {assignments} WHERE item_id = :item_id",
                       {**fields, "item_id": item_id})
            return row

    def ledger_counts(self) -> dict:
        row = self._one(
            "SELECT "
            "SUM(removed_at IS NULL) AS on_kobo, "
            "SUM(removed_at IS NULL AND kobo_status = 'Reading') AS reading, "
            "SUM(removed_at IS NULL AND kobo_status = 'Finished') AS finished, "
            "SUM(removed_at IS NULL AND downloaded_at IS NOT NULL) AS downloaded, "
            "SUM(reoffer_count > 0 AND downloaded_at IS NULL) AS reoffered_not_downloaded "
            "FROM ledger"
        )
        return {key: row[key] or 0 for key in row}

    # outbox

    def outbox_add(self, item_id: int, action: str) -> None:
        """Queue a read/unread for Nextcloud. A newer action replaces an older one."""
        with self._lock:
            self._exec("DELETE FROM outbox WHERE item_id = ?", (item_id,))
            self._exec(
                "INSERT INTO outbox (item_id, action, created_at) VALUES (?, ?, ?)",
                (item_id, action, time.time()),
            )

    def outbox_cancel(self, item_id: int) -> None:
        self._exec("DELETE FROM outbox WHERE item_id = ?", (item_id,))

    def outbox_pending(self) -> list[dict]:
        return self._all("SELECT * FROM outbox ORDER BY id")

    def outbox_done(self, ids: list[int]) -> None:
        with self._lock:
            self._db.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])

    def outbox_failed(self, ids: list[int], error: str) -> None:
        with self._lock:
            self._db.executemany(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                [(error, i) for i in ids],
            )

    # tags

    def tags_all(self) -> list[dict]:
        return self._all("SELECT * FROM tags")

    def tag_upsert(self, *, uuid: str, kind: str, ref: str, name: str, items: list[str],
                   gen: int, created: bool) -> None:
        now = time.time()
        if created:
            self._exec(
                "INSERT INTO tags (uuid, kind, ref, name, items_json, created_at, created_gen, "
                "modified_at, sent_gen, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(uuid) DO UPDATE SET name = excluded.name, items_json = excluded.items_json, "
                "created_at = excluded.created_at, created_gen = excluded.created_gen, "
                "modified_at = excluded.modified_at, sent_gen = excluded.sent_gen, deleted_at = NULL",
                (uuid, kind, ref, name, json.dumps(items), now, gen, now, gen),
            )
        else:
            self._exec(
                "UPDATE tags SET name = ?, items_json = ?, modified_at = ?, sent_gen = ? WHERE uuid = ?",
                (name, json.dumps(items), now, gen, uuid),
            )

    def tag_resent(self, uuid: str, gen: int) -> None:
        self._exec("UPDATE tags SET sent_gen = ? WHERE uuid = ?", (gen, uuid))

    def tag_delete(self, uuid: str, gen: int) -> None:
        now = time.time()
        self._exec(
            "UPDATE tags SET deleted_at = ?, modified_at = ?, sent_gen = ? WHERE uuid = ?",
            (now, now, gen, uuid),
        )

    def tag_purge(self, uuid: str) -> None:
        self._exec("DELETE FROM tags WHERE uuid = ?", (uuid,))


class _Transaction:
    def __init__(self, state: State):
        self._state = state

    def __enter__(self):
        self._state._lock.acquire()
        self._state._db.execute("BEGIN")
        return self._state

    def __exit__(self, exc_type, exc, tb):
        try:
            self._state._db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self._state._lock.release()
        return False
