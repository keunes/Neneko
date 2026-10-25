# Kobo sync notes

What Neneko relies on, and what was verified on a real device. Useful when Kobo firmware or the
Kobo store changes.

## Verified on the device (October 2026)

With calibre-web as the stand-in store (firmware 4.38.23697 and 4.38.23828):

- Library sync, download, "Mark as finished" → read, and "Mark as unread" → unread all work.
- The Kobo resolves a LAN host name and talks plain HTTP to it.
- Per sync, the Kobo requests: `initialization` → `user/profile`, `products/books/subscriptions`,
  `deals`, `assets`, `analytics/gettests` → `library/sync` → `user/wishlist`,
  `user/recommendations` → `products/<id>/nextread` for each library book.
- The store's `initialization` response contains `instapaper_enabled` and `instapaper_env_url`;
  Neneko passes them through unchanged.

## Observed with Neneko

- Up to 0.2.1-dev.4 the Kobo downloaded only a few new books by itself after a sync; the
  others were listed with a download icon and downloaded when tapped. Repeated Syncs didn't
  change that, because the books were already known. All of it fits what Nickel does when the
  sync response has no `x-kobo-recent-reads` header (see "How Nickel syncs and downloads"):
  only the step after the last page downloads anything, at most 5 books, out of those saved
  by the time it runs.
  - 50 articles in one response: the first 5 were downloaded.
  - 0.2.1-dev.1 sent pages of 5 (`x-kobo-sync: continue`) and offered never-downloaded
    articles again as `NewEntitlement`s: 25 new and 23 re-offered in 10 pages, and only the 3 in
    the last page were downloaded.
  - Only those books' `metadata` and file were requested, so Nickel picks what to download
    before it fetches anything.
  - 0.2.1-dev.2 matched calibre-web more closely (a `ReadingState` with every `NewEntitlement`,
    the real `DownloadUrls[].Size`, `HEAD` on `/download`). No change ("downloading x of 5" on the
    Kobo): Nickel doesn't choose by these.
  - The debug log ([kobo-debugging.md](kobo-debugging.md)) showed the choice in
    `LibraryNextPageCommand::downloadBooks`, one line per chosen book: 2 of 8 new articles, 1 of 3.
- 0.2.1-dev.5 sends new articles 5 per page with `x-kobo-recent-reads: 5` on every page, and
  offers never-downloaded ones again (at most 3 times). **To verify on the device**: every
  article should download without tapping.
- Articles removed on the Kobo are not sent again, unless they change in Nextcloud
  afterwards (`lastModified` later than the removal), for example when marked unread again.
  Because the removal itself arrives late (see `DELETE` below), an article marked unread since
  the previous Sync also wins: its pending "mark read" is dropped before the outbox is sent.
- kepubify's dummy title page ("intentionally left blank") is disabled with
  `--no-add-dummy-titlepage`.

## Still to verify with Neneko itself

- Download `Size` in the sync metadata is an estimate for articles not built within the
  sync's wait. Komga also reports sizes that differ from the downloaded file, so the Kobo is
  expected to tolerate it.
- `IsRemoved` for an ID the Kobo doesn't have (the recovery sweep after a database loss) should
  be ignored by the Kobo.
- Covers are served as PNG (dithered) from a URL ending in `image.jpg`.
- `products/<id>/nextread` for Neneko's own articles is answered locally with an empty list
  (a 404 made the Kobo log an error each time).

How to watch the Kobo's own log during a Sync: [kobo-debugging.md](kobo-debugging.md).

## How Nickel syncs and downloads (firmware 4.38.23828)

Read from the Kobo's `libnickel.so.1.0.0` without running it, using the C++ names it exports
([`tools/nickel/`](../tools/nickel/README.md) has the helper and the commands to check each
point again after a firmware update). Only the outcome has been seen on a device, not each
step. Names below are Nickel's.

### One sync, page by page

- `LibraryNextPageCommand` runs the library sync. At the start of a sync (not when resuming
  one) `initializeSync` sets the user's `librarySyncTime` to the sync's start time
  (`SyncState::getSyncTime()`). Every page of that sync uses the same time.
- `LibraryParser::parseHeaders` reads four response headers: `x-kobo-synctoken` (stored),
  `x-kobo-sync-mode` (`full`), `x-kobo-sync` (`continue`: there are more pages) and
  `x-kobo-recent-reads` (a number, `numRecentBooksFirst`; 0 when missing).
- For each page, `onResponseParsed` queues commands for the page's books
  (`ResolveEntitlementUpdatesCommand`), metadata changes, reading states and tags. Saving a
  book (`handleBook`) stamps it with `librarySyncTime` (`Content::setSyncTime`).
- On the last page (no `continue`) `lastPageParsed()` runs, then `librarySyncTime` is cleared.
  The continuation token is saved after every page.

### Which books it downloads

Two steps choose downloads, and both go through `LibraryNextPageCommand::downloadBooks`:

- **Every page**, after that page's books are saved
  (`ResolveEntitlementUpdatesCommand::downloadRecentBooks`): n = min(`x-kobo-recent-reads`,
  `MaxDownload`). With n = 0 (the header is missing) nothing is chosen. With n < 0 the whole page
  is. Otherwise the first n books of the page, in the order the server sent them; sorting by
  recency happens only after the cut.
- **After the last page** (`lastPageParsed` → `VolumeManager::retrieveSyncedBooks`): the books
  whose sync time equals `librarySyncTime`, sorted by recency. It runs before the last page's own
  books are saved (their command is only queued by then), so it sees what was saved up to that
  point. That makes its choice small and hard to predict.
- `downloadBooks` goes through its list and takes books `canDownload` allows (not downloaded,
  a supported format, accessible) until it has `MaxDownload` of them. The count starts again at
  0 for every call. With debug logging on it logs each chosen title. The books go into the saved
  download queue (`VolumeManager::appendToDownloadQueue`), and `DownloadQueuedBooksCommand`
  downloads that queue one book at a time, with no limit of its own.
- Only Kobos with selective sync work like this (`Device::supportsSelectiveSync`: device class
  2 or 4 and a version check). Others download every book they get.
- `MaxDownload`: `FeatureSettings::maxBooksToDownload()` reads `MaxDownload` in the
  `[FeatureSettings]` section of `.kobo/Kobo/Kobo eReader.conf`, default 5 (0 or less: nothing).
  The same limit applies to Kobo store books and to notebooks
  (`SyncNotebookCommand::downloadNotebooks`). Raising it should work too, but that hasn't been
  tried, and Neneko doesn't need it.

### What Neneko does with this

- New articles go out a page at a time (`NENEKO_NEW_PER_SYNC_PAGE`, default 5), and every sync
  response carries `x-kobo-recent-reads` set to the page size, so each page downloads in full.
  Neneko's value replaces the store's: the store's books are merged into the last page and are
  covered by it too. Pages larger than 5 only help with `MaxDownload` raised.
- A book the Kobo already lists is only considered again when it is sent again, so articles it
  never downloaded are re-offered as `NewEntitlement`s in the room left on a page, at most 3
  times each.

### Still open

- Whether a page's downloads start before the Kobo asks for the next page.
- Whether a book chosen in both steps is downloaded once (`VolumeManager` has
  `isVolumeInDownloadQueue`, so probably).
- What the `ui()` check in `downloadBooks`'s loop reads; probably whether debug logging is on,
  since it guards the title line.

### Temporary: re-offer tracking (added 0.2.1-dev.5)

The limit of 3 re-offers is there in case the Kobo never takes some article (full storage, a
file it rejects), which would otherwise be offered at every Sync. It isn't known whether that
happens. To find out, the ledger counts re-offers (`reoffer_count`, `last_reoffered_at`), and
rows that were offered again are not deleted 30 days after leaving the Kobo
(`State.ledger_delete_older_than`). The status page shows "Offered again, never downloaded".

After about a month of use, look at the rows:

```sh
docker compose exec neneko python -c "import sqlite3; [print(r) for r in sqlite3.connect('/data/neneko.sqlite').execute(
  'SELECT item_id, title, reoffer_count, last_reoffered_at, downloaded_at, removed_at FROM ledger WHERE reoffer_count > 0')]"
```

- Rows with `reoffer_count` 3 and no `downloaded_at`: the limit was needed. Keep it (the
  retention exemption can still go).
- Rows with a `downloaded_at`: re-offering worked for those.
- No rows at all: re-offers were never needed, and the paging alone is enough.

Removing the tracking: the `reoffer_count = 0` condition in `ledger_delete_older_than`, the
status page row, and this subsection. The columns can stay.

## Protocol summary

- The Kobo's `api_endpoint` (in `.kobo/Kobo/Kobo eReader.conf`, section `[OneStoreServices]`)
  is the base URL for every store call. Neneko's is `<public url>/kobo/<token>`.
- `GET /v1/initialization` returns `Resources`; Neneko rewrites `library_sync`, `image_host`,
  `image_url_template` and `image_url_quality_template` and sends `x-kobo-apitoken: e30=`.
- `GET /v1/library/sync` returns a JSON list of `NewEntitlement`, `ChangedEntitlement`
  (`BookEntitlement.IsRemoved` removes a book), `NewTag`, `ChangedTag`, `DeletedTag`. The
  `x-kobo-synctoken` response header is stored by the Kobo and sent back next time;
  `x-kobo-sync: continue` makes the Kobo ask again straight away; `x-kobo-recent-reads: N` makes it
  download up to N books of that page (see above).
- `PUT /v1/library/<id>/state` carries `ReadingStates[0]` with `StatusInfo.Status`
  (`ReadyToRead`, `Reading`, `Finished`), `CurrentBookmark` and `Statistics`.
- `DELETE /v1/library/<id>` is "remove from device". The Kobo sends it only at the next Sync
  (just before `library/sync`), with no body and no timestamp, so the server can't know when
  the book was actually removed on the device.
- Covers: `<image host>/<id>/<width>/<height>/false/image.jpg`.

Neneko's sync token holds the database epoch, the generation number of the last response, and
the real store's token. A response the Kobo never received (generation not echoed back) is sent
again on the next sync. Between the pages of one Sync it also holds the generation the Sync
started at and how far re-offering got.

## Nextcloud News API (v1-3)

- `GET /items?type=3&id=0&getRead=false&batchSize=N` (all) or `type=1&id=<folder>` (one folder):
  newest first by item ID (arrival order; `pubDate` doesn't follow it).
- `GET /folders` always returns `"feeds": []`; folder membership is `folderId` in `GET /feeds`.
- `POST /items/read/multiple` and `/items/unread/multiple` with `{"itemIds": [...]}`.
- `GET /items/updated?type=3&id=0&lastModified=<unix time>` lists items changed since then.
- The per-feed "full text" setting isn't in the public API; the web UI endpoint that has it
  rejects app passwords.
