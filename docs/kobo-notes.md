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

- The Kobo downloads only 5 new books by itself after a sync; the others are listed
  with a download icon and download when tapped. Repeated Syncs don't change that, because
  the books are already known. 0.2.1-dev.1 tests two remedies together: articles in pages of
  5 (`x-kobo-sync: continue`) and offering never-downloaded articles again on later Syncs.
- kepubify's dummy title page ("intentionally left blank") is disabled with
  `--no-add-dummy-titlepage`.

## Still to verify with Neneko itself

- Download `Size` in the sync metadata is an estimate until an article is built. Komga also
  reports sizes that differ from the downloaded file, so the Kobo is expected to tolerate it.
- `IsRemoved` for an ID the Kobo doesn't have (the recovery sweep after a database loss) should
  be ignored by the Kobo.
- Covers are served as PNG (dithered) from a URL ending in `image.jpg`.
- `products/<id>/nextread` for Neneko's own articles is answered with 404 locally.

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
