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

With Neneko 0.2.1-dev.5 and dev.6 (firmware 4.38.23828, 25 October 2026):

- Pages of 5 new articles, each response with `x-kobo-recent-reads: 5`: 16 new articles came in
  4 pages and all were downloaded without tapping. Each page's downloads finished before the
  Kobo asked for the next page.
- After raising `NENEKO_MAX_ITEMS`, one Sync delivered 141 articles in 29 pages. The Kobo
  downloaded 105; the other 36 were articles Neneko had removed from the Kobo before and that
  came back into the window. Checked in the Kobo's database (`.kobo/KoboReader.sqlite`, table
  `content`):
  - A removed book isn't deleted: its row stays, with `___UserID` set to `removed`. 12 such rows
    were left, and Neneko's ledger had held 48 removed rows before the Sync (36 + 12).
  - The skipped rows differ from the downloaded ones only in what downloading changes
    (`IsDownloaded`, `IsEncrypted`, `NumShortcovers`).
  - All 141 rows have the same `___SyncTime`: one sync time for the whole Sync.
- The Kobo's "downloading book x of y" counts across the whole Sync: every page adds its books to
  one download queue, so y grows from page to page.

## Observed with Neneko

- Up to 0.2.1-dev.4 the Kobo downloaded only a few new books by itself after a sync; the
  others were listed with a download icon and downloaded when tapped. Repeated Syncs didn't
  change that, because the books were already known. All of it fits what Nickel does when the
  sync response has no `x-kobo-recent-reads` header (see "How Nickel syncs and downloads"):
  only the step after the last page downloads anything, at most 5 books, and it skips books
  the Kobo already listed.
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
- 0.2.1-dev.5 sent new articles 5 per page with `x-kobo-recent-reads: 5` on every page: they
  all download (see above). It also offered 40 never-downloaded articles again, 5 per page, in
  two Syncs: none were downloaded, because Nickel doesn't download a book it already lists
  (`shouldRedownload`, below). Removed in 0.2.1-dev.6.
- With pages, every page that adds books changed the collections, and the Kobo spent seconds per
  page applying them (`ResolveTagUpdatesCommand`, one transaction 4.7 s). Since 0.2.1-dev.6
  collections are sent on the last page only.
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

To look at what the Kobo stored, connect it over USB and copy `.kobo/KoboReader.sqlite` off it
first (don't query the file on the Kobo itself). Neneko's articles are the `content` rows with
`ContentType = 6` whose `ContentID` starts with the first part of any Neneko book ID (the
`/download/<id>/` part of a download line in Neneko's log):

```sh
sqlite3 KoboReader.sqlite "SELECT ___UserID = 'removed', IsDownloaded, count(*) FROM content
  WHERE ContentType = 6 AND ContentID LIKE '<first 23 characters of a Neneko book ID>%' GROUP BY 1, 2"
```

## How Nickel syncs and downloads (firmware 4.38.23828)

Read from the Kobo's `libnickel.so.1.0.0` without running it, using the C++ names it exports
([`tools/nickel/`](../tools/nickel/README.md) has the helper and the commands to check each
point again after a firmware update), and checked against the Kobo's debug log where marked.
Names below are Nickel's.

### One sync, page by page

- `LibraryNextPageCommand` runs the library sync. At the start of a sync (not when resuming
  one) `initializeSync` sets the user's `librarySyncTime` to the sync's start time
  (`SyncState::getSyncTime()`). Every page of that sync uses the same time.
- `LibraryParser::parseHeaders` reads four response headers: `x-kobo-synctoken` (stored),
  `x-kobo-sync-mode` (`full`), `x-kobo-sync` (`continue`: there are more pages) and
  `x-kobo-recent-reads` (a number, `numRecentBooksFirst`; 0 when missing).
- For each page, `onResponseParsed` queues commands for the page's books
  (`ResolveEntitlementUpdatesCommand`), metadata changes, reading states and tags, and runs them
  straight away (`QueuedSyncCommand::pumpQueue`, seen in the Kobo's stack traces). Saving a book
  (`handleBook`) stamps it with `librarySyncTime` (`Content::setSyncTime`). A `NewEntitlement`
  for a book the Kobo already has goes the same way (`parseNewEntitlement` looks it up with
  `getById` and updates it).
- On the last page (no `continue`) `lastPageParsed()` runs, then `librarySyncTime` is cleared.
  The continuation token is saved after every page.

### Which books it downloads

Two steps choose downloads, and both go through `LibraryNextPageCommand::downloadBooks`:

- **Every page**, after that page's books are saved
  (`ResolveEntitlementUpdatesCommand::downloadRecentBooks`): n = min(`x-kobo-recent-reads`,
  `MaxDownload`). With n = 0 (the header is missing) nothing is chosen. With n < 0 the whole page
  is. Otherwise the first n books of the page, in the order the server sent them; sorting by
  recency happens only after the cut. Seen on the device: up to 5 per page, and the page's
  downloads finish before the next page is requested.
- **After the last page** (`lastPageParsed` → `VolumeManager::retrieveSyncedBooks`): the books
  whose sync time equals `librarySyncTime` (so the whole sync's), sorted by recency. Seen on the
  device: it runs after the last page is saved, and books the first step already downloaded are
  logged as `already downloaded` and not fetched again.
- `downloadBooks` goes through its list and takes books `canDownload` allows until it has
  `MaxDownload` of them. The count starts again at 0 for every call. With debug logging on it
  logs each chosen title. The books go into the saved download queue
  (`VolumeManager::appendToDownloadQueue`), and `DownloadQueuedBooksCommand` downloads that queue
  one book at a time, with no limit of its own.
- Only Kobos with selective sync work like this (`Device::supportsSelectiveSync`: device class
  2 or 4 and a version check). Others download every book they get.
- `MaxDownload`: `FeatureSettings::maxBooksToDownload()` reads `MaxDownload` in the
  `[FeatureSettings]` section of `.kobo/Kobo/Kobo eReader.conf`, default 5 (0 or less: nothing).
  The same limit applies to Kobo store books and to notebooks
  (`SyncNotebookCommand::downloadNotebooks`). Raising it hasn't been tried on the device yet.

### Why a book the Kobo already lists isn't downloaded again

`canDownload` takes a book only if it isn't downloaded, its format is supported, its
accessibility allows it, and a flag `handleBook` sets from `VolumeManager::shouldRedownload` is
on. `shouldRedownload` compares the incoming book with the one the Kobo already has:

- the Kobo doesn't have it yet: yes
- `isFullBookAccessible` differs: yes
- not fully accessible (a preview): yes if the preview URL appeared or disappeared
- otherwise: yes only if `hasDownloadUrl` differs

So sending a known, undownloaded book again (same download URL) changes nothing: it is saved and
skipped. Seen on the device with 0.2.1-dev.5's re-offering (40 articles, none downloaded).

Removing a book doesn't make it unknown: `IsRemoved` keeps the book's row in the Kobo's
database, with `___UserID` = `removed` (seen on the device). So an article that comes back
after a removal (marked unread again, back in a larger window) has the same ID, `getById` finds
the old row, and it isn't downloaded either. Removing and re-sending a book therefore can't force
a download. Only a different book ID would; Neneko's IDs are fixed per article on purpose (see
`src/neneko/ids.py`), so these articles download when tapped.

### How the library sorts

The library's sort menu maps to `AbstractLibraryBuilder::sortBy…`. For store books (all of
Neneko's articles; sideloaded books use other fields):

- **Recent** (`sortByNewest`, `RecentPrimarySorter` / `RecentSecondarySorter`): by
  `DateLastRead` (`RecentKey`), newest first. Nickel sets `DateLastRead` from the sync's
  `ReadingState.PriorityTimestamp` when that is later than what the book has
  (`ReadingState::updateVolume`), and to the current time when the book is opened. So for an
  article that was never opened, "Recent" is the `PriorityTimestamp` the server sent.
- **Date added** (`sortByDateAdded`, oldest first with `sortByDateAddedOldest`): by the
  entitlement's `Created` date (`Volume::getDateAddedSortKey` →
  `EntitlementManager::getCreatedRaw`), i.e. `BookEntitlement.Created` from the sync.
- Books with the same key are ordered by title and subtitle (both sorts add a
  `TitleSubtitleSorter` / `SeriesOnlySorter` tie-break).

The order in which the server lists books in a response doesn't matter for either sort; only
these two timestamps do. Neneko (up to 0.2.1-dev.6) sends the delivery time as both, so all
articles of one sync page share a time, sorted by title within the page, and later pages sort
as newer. Seen on the device: a 29-page Sync gave 29 distinct `DateLastRead` values, and because
Neneko sends the newest articles first, the oldest articles came out on top of "Recent" (and
"Date added").

Since 0.2.1-dev.7 both timestamps are the article's arrival in Nextcloud News: its
`lastModified`, which follows Nextcloud's newest-first order (by item ID) unless the article
changed there since, e.g. was marked unread again (it then sorts as just arrived).
`PriorityTimestamp` becomes the time the article was last opened once the Kobo reports reading
it. This only affects books new to the Kobo: `updateVolume` never moves `DateLastRead`
backwards, so articles already on it keep their old order.

### What Neneko does with this

- New articles go out a page at a time (`NENEKO_NEW_PER_SYNC_PAGE`, default 5), and every sync
  response carries `x-kobo-recent-reads` set to the page size, so each page downloads in full.
  Neneko's value replaces the store's: the store's books are merged into the last page and are
  covered by it too.
- With `MaxDownload` raised on the Kobo, a larger page (up to `NENEKO_MAX_ITEMS`) makes a Sync
  one response: fewer library refreshes, but the Kobo store and notebooks use the same limit.
  See the README, "Fewer refreshes during a Sync".
- Collections are sent once per Sync, on its last page.
- Articles the Kobo lists but never downloaded aren't offered again, and articles that come back
  after a removal aren't downloaded by themselves (see above); both download when tapped.

### Still open

- What the `ui()` check in `downloadBooks`'s loop reads; probably whether debug logging is on,
  since it guards the title line.
- Whether a raised `MaxDownload` is read as described (and whether the Kobo needs a restart).

## Protocol summary

- The Kobo's `api_endpoint` (in `.kobo/Kobo/Kobo eReader.conf`, section `[OneStoreServices]`)
  is the base URL for every store call. Neneko's is `<public url>/kobo/<token>`.
- `GET /v1/initialization` returns `Resources`; Neneko rewrites `library_sync`, `image_host`,
  `image_url_template` and `image_url_quality_template` and sends `x-kobo-apitoken: e30=`.
- `GET /v1/library/sync` returns a JSON list of `NewEntitlement`, `ChangedEntitlement`
  (`BookEntitlement.IsRemoved` removes a book from the library; its row stays in the Kobo's
  database with `___UserID` = `removed`), `NewTag`, `ChangedTag`, `DeletedTag`. The
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
again on the next sync.

## Nextcloud News API (v1-3)

- `GET /items?type=3&id=0&getRead=false&batchSize=N` (all) or `type=1&id=<folder>` (one folder):
  newest first by item ID (arrival order; `pubDate` doesn't follow it).
- `GET /folders` always returns `"feeds": []`; folder membership is `folderId` in `GET /feeds`.
- `POST /items/read/multiple` and `/items/unread/multiple` with `{"itemIds": [...]}`.
- `GET /items/updated?type=3&id=0&lastModified=<unix time>` lists items changed since then.
- The per-feed "full text" setting isn't in the public API; the web UI endpoint that has it
  rejects app passwords.
