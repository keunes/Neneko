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

- The Kobo downloads only a few new books by itself after a sync; the others are listed
  with a download icon and download when tapped. Repeated Syncs don't change that, because
  the books are already known.
  - 50 articles in one response: the first 5 were downloaded.
  - 0.2.1-dev.1 sent pages of 5 (`x-kobo-sync: continue`) and offered never-downloaded
    articles again as `NewEntitlement`s: 25 new and 23 re-offered in 10 pages, and only the 3 in
    the last page were downloaded. So neither helps, and the code was removed again.
  - Only those books' `metadata` and file were requested, so Nickel picks what to download
    before it fetches anything.
  - 0.2.1-dev.2 matches calibre-web more closely: every `NewEntitlement` carries a
    `ReadingState` (all timestamps the delivery time) and the sync waits up to 10 s for builds
    so `DownloadUrls[].Size` is the real file size. `/download` also answers `HEAD`. Still
    only a few downloads ("downloading x of 5" on the Kobo).
  - The debug log ([kobo-debugging.md](kobo-debugging.md)) shows the choice being made in
    `LibraryNextPageCommand::downloadBooks`, one line per chosen book. With 8 new articles it
    picked 2, with 3 new ones it picked 1, so the cap may count books already on the Kobo.
  - `libnickel.so` contains `FeatureSettings::maxBooksToDownload()`, a `MaxDownload` string and
    `ResolveEntitlementUpdatesCommand::downloadRecentBooks`. That suggests a setting
    `MaxDownload` in the `[FeatureSettings]` section of `Kobo eReader.conf`. **Untested.**
- Articles removed on the Kobo are not sent again, unless they change in Nextcloud
  afterwards (`lastModified` later than the removal), for example when marked unread again.
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

## Protocol summary

- The Kobo's `api_endpoint` (in `.kobo/Kobo/Kobo eReader.conf`, section `[OneStoreServices]`)
  is the base URL for every store call. Neneko's is `<public url>/kobo/<token>`.
- `GET /v1/initialization` returns `Resources`; Neneko rewrites `library_sync`, `image_host`,
  `image_url_template` and `image_url_quality_template` and sends `x-kobo-apitoken: e30=`.
- `GET /v1/library/sync` returns a JSON list of `NewEntitlement`, `ChangedEntitlement`
  (`BookEntitlement.IsRemoved` removes a book), `NewTag`, `ChangedTag`, `DeletedTag`. The
  `x-kobo-synctoken` response header is stored by the Kobo and sent back next time;
  `x-kobo-sync: continue` makes the Kobo ask again straight away.
- `PUT /v1/library/<id>/state` carries `ReadingStates[0]` with `StatusInfo.Status`
  (`ReadyToRead`, `Reading`, `Finished`), `CurrentBookmark` and `Statistics`.
- `DELETE /v1/library/<id>` is "remove from device".
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
