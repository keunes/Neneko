# Neneko

Neneko puts your unread [Nextcloud News](https://github.com/nextcloud/news) articles on a Kobo
e-reader over Wi-Fi, as an open source alternative to the Kobo's built-in Instapaper/Pocket
integration. Press **Sync** on the Kobo and the newest unread articles appear in your library,
one book per article, with the full text and images. Open an article on the Kobo and it is
marked read in Nextcloud News; "Mark as unread" on the Kobo marks it unread again.

It runs as one small Docker container next to your Nextcloud (a Raspberry Pi is plenty) and
works with the Kobo's stock firmware: you only change one line in the Kobo's settings file.

## How it works

The Kobo syncs its library with the Kobo store. Neneko pretends to be that store, the same
way [calibre-web](https://github.com/janeczku/calibre-web) and [Komga](https://komga.org) do:

1. When you press **Sync**, the Kobo asks Neneko for library changes. Neneko asks Nextcloud
   News for the newest unread items and answers within a second or two, so the articles show up
   in the library straight away.
2. In the background Neneko builds each article: it fetches the original page, extracts the
   article text ([trafilatura](https://trafilatura.readthedocs.io)), converts images to
   greyscale, writes an EPUB and converts it to Kobo's KEPUB format
   ([kepubify](https://pgaskin.net/kepubify/)). The Kobo downloads them as they're ready.
3. When you open (or finish) an article, the Kobo reports it; Neneko marks it read in Nextcloud
   News. Articles you read elsewhere disappear from the Kobo at the next sync.

Everything else the Kobo asks for (your purchased books, store pages, Instapaper) is passed
on to the real Kobo store, so those keep working.

Nextcloud is only contacted when the Kobo syncs; there is no background polling.

## Setup

You need Docker (with Compose), Nextcloud with the News app, and a Kobo on the same network.

1. **Create a Nextcloud app password**: Nextcloud → Settings → Security → *Create new app
   password*. Note the login name Nextcloud shows with it.

2. **Create the secrets**, next to your `docker-compose.yml`:

   ```sh
   mkdir -p secrets
   printf '%s' 'the-app-password' > secrets/neneko_nc_app_password
   openssl rand -hex 24 > secrets/neneko_kobo_token
   chmod 644 secrets/*   # the container runs as uid 1000 and must be able to read them
   ```

   The Kobo token is part of the URL the Kobo uses; anyone who has it can read your synced
   articles, so keep it private.

3. **Add the service** from [`docker-compose.example.yml`](docker-compose.example.yml) to your
   compose file, set `NENEKO_NC_URL`, `NENEKO_NC_USER` and `NENEKO_PUBLIC_URL` (the address the
   Kobo uses to reach the container, e.g. `http://neneko-host:8089`), and start it:

   ```sh
   docker compose pull neneko && docker compose up -d neneko
   ```

   The image `ghcr.io/keunes/neneko` is built for 64-bit ARM (Raspberry Pi 3/4/5 with a 64-bit
   OS) and x86-64. To build it yourself instead, replace `image:` with `build: .` in a checkout of
   this repository and run `docker compose up -d --build neneko`.

   The log shows at startup whether Nextcloud News is reachable, with a hint if it isn't. Open
   `http://<host>:8089/` for the status page, which runs the same check.

   If Nextcloud runs in the same compose project you can use its service name in
   `NENEKO_NC_URL` (e.g. `http://nextcloud`). That name must then be in Nextcloud's
   `trusted_domains`; otherwise use the public address.

4. **Point the Kobo at Neneko.** Connect the Kobo with USB and open
   `.kobo/Kobo/Kobo eReader.conf` (the `.kobo` folder is hidden). Make a backup copy, then in
   the `[OneStoreServices]` section change the `api_endpoint` line to:

   ```
   api_endpoint=http://neneko-host:8089/kobo/<contents of secrets/neneko_kobo_token>
   ```

   Eject the Kobo, connect it to Wi-Fi and press **Sync**.

   To undo this, set the line back to `api_endpoint=https://storeapi.kobo.com`.

## What ends up on the Kobo

- The **newest unread items** (by arrival in Nextcloud News), at most `NENEKO_MAX_ITEMS`. Limit
  them to some folders with `NENEKO_NC_FOLDERS`; by default all feeds are included. Items
  hidden by News feed filters are skipped.
- When newer articles push an older one out of that window and you haven't opened it, it is
  removed from the Kobo; it stays unread in Nextcloud.
- Articles you have started reading stay until you finish them.
- Opening an article marks it read in Nextcloud (`NENEKO_MARK_READ_ON=opened`, like the
  Nextcloud News Android app). It stays on the Kobo until you finish it.
- Finished articles leave the Kobo after `NENEKO_FINISHED_GRACE_HOURS`, so you can still
  "Mark as unread" for a while.
- The Kobo itself downloads only a few new books after a sync; the others show a download
  icon and download instantly when you tap them (they are already built). Neneko now sends new
  articles the way calibre-web does (with a reading state and the real file size) in case that
  changes it; see [`docs/kobo-notes.md`](docs/kobo-notes.md) for what was tried.
- Articles marked read elsewhere (web, phone app) are removed at the next sync.
- Deleting an article on the Kobo marks it read in Nextcloud (or only forgets it, see
  `NENEKO_DELETE_ACTION`) and it isn't sent again, unless you change it in Nextcloud
  afterwards: mark it unread there and it comes back at the next Sync.

**Collections.** Kobo collections can't be nested, but one article can be in several. By default
every article is in `Neneko` (everything) and `Neneko · <folder>`; with `NENEKO_COLLECTIONS=all,folder,feed`
you also get one collection per feed. Changing these settings renames or removes the collections on the
Kobo at the next sync.

**Full text.** Neneko fetches the original web page of every article and extracts the main
text. If that fails, or gives less text than the feed itself (a cookie wall, a paywall), the
feed's text is used with a link to the original. For feeds that already contain the full
article you can skip the extra download with `NENEKO_FULLTEXT_NEVER_EXTRACT`. For sites you
subscribe to, export your browser cookies in Netscape `cookies.txt` format and mount the file
as `NENEKO_COOKIES_FILE`. Embedded videos become links. Code is set in a monospace font; if
it doesn't show as such, try the Kobo's "Publisher Default" font setting, since a font you
choose on the Kobo can replace the book's own fonts.

## Configuration

All settings are environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `NENEKO_NC_URL` | (required) | Nextcloud address, e.g. `https://cloud.example.org` |
| `NENEKO_NC_USER` | (required) | Nextcloud login name |
| `NENEKO_NC_APP_PASSWORD` / `_FILE` | (required) | App password, or a file containing it |
| `NENEKO_PUBLIC_URL` | (required) | How the Kobo reaches Neneko, e.g. `http://neneko-host:8089` |
| `NENEKO_KOBO_TOKEN` / `_FILE` | (required) | Secret in the Kobo's `api_endpoint`; at least 16 of `A-Z a-z 0-9 - _` |
| `NENEKO_NC_FOLDERS` | all | Comma-separated News folder names to sync |
| `NENEKO_MAX_ITEMS` | `50` | Most articles on the Kobo at once |
| `NENEKO_MARK_READ_ON` | `opened` | When an article is marked read in Nextcloud: `opened` (as soon as you open it on the Kobo), `finished`, or `delivered` (as soon as it reaches the Kobo) |
| `NENEKO_FINISHED_GRACE_HOURS` | `24` | How long finished articles stay on the Kobo |
| `NENEKO_DELETE_ACTION` | `mark_read` | Deleting on the Kobo: `mark_read` or `forget` |
| `NENEKO_COLLECTIONS` | `all,folder` | Which collections to create: `all`, `folder`, `feed` |
| `NENEKO_COLLECTION_ALL_NAME` | `Neneko` | Name of the collection with every article |
| `NENEKO_COLLECTION_FOLDER_TEMPLATE` | `Neneko · {folder}` | Per-folder collection name |
| `NENEKO_COLLECTION_FEED_TEMPLATE` | `{feed}` | Per-feed collection name (`{folder}` also available) |
| `NENEKO_FULLTEXT_NEVER_EXTRACT` | | Comma-separated feed titles or IDs whose feed text is used as-is |
| `NENEKO_COOKIES_FILE` | | Netscape `cookies.txt` for fetching pages |
| `NENEKO_USER_AGENT` | a desktop Chrome | User-Agent for fetching pages and images |
| `NENEKO_LANGUAGE` | `en` | Book language when the page doesn't declare one |
| `NENEKO_IMAGE_GRAYSCALE` | `true` | Convert article images to greyscale |
| `NENEKO_IMAGE_MAX_WIDTH` | `1264` | Downscale wider images to this many pixels |
| `NENEKO_IMAGE_DITHER` | `off` | `ordered` dithers article images to the 16 e-ink grey levels |
| `NENEKO_COVER_DITHER` | `ordered` | Same for the generated covers (`off` for plain JPEG) |
| `NENEKO_MAX_IMAGES` | `20` | Images per article |
| `NENEKO_STORE_PROXY` | `true` | Pass other requests on to the real Kobo store. With `false` your purchased books and Instapaper stop syncing while the Kobo points at Neneko |
| `NENEKO_BUILD_WORKERS` | `4` | Articles built in parallel |
| `NENEKO_RECOVERY_DAYS` | `14` | After the database was lost, remove articles read within this many days from the Kobo |
| `NENEKO_LOG_LEVEL` | `INFO` | `DEBUG` for more detail |
| `TZ` | `UTC` | Time zone for the log, e.g. `Europe/Berlin` (the status page uses your browser's) |

The dithering is a port of Calibre's "Upload dithered covers" option (ImageMagick's `o8x8`
ordered dither), verified pixel-identical against Calibre's own output (`tests/test_images.py`). It suits covers, which the Kobo shows at their exact size; article
images get rescaled by the reader, which can make a dither pattern look grainy, so it is off by
default for those.

## Data

The `/data` volume holds `neneko.sqlite` and a cache of built articles and covers. The
database only records what is on the Kobo and which read/unread changes still have to reach
Nextcloud (when Nextcloud is unreachable they are kept and sent at the next sync). If you lose
it, nothing breaks: each article's Kobo ID is derived from its News item ID, so the next sync
re-sends the current articles without creating duplicates, and articles read in the meantime
are removed.

## Troubleshooting

- **Nextcloud News not reachable**: the startup log and the status page say what failed and
  what to check (host name, login, `trusted_domains`, News app). `GET /healthz?nextcloud=1`
  runs the same check on demand (HTTP 503 when it fails). The container's own health check
  doesn't depend on Nextcloud.
- **Sync fails on the Kobo**: the Kobo must reach `NENEKO_PUBLIC_URL`. Open
  `NENEKO_PUBLIC_URL/healthz` from another device on the same Wi-Fi.
- **An article only has the teaser text**: the site couldn't be fetched or extracted (paywall,
  JavaScript-only page); the article ends with a note and a link to the original. A log line
  "refuses automated requests" (HTTP 403) means the site blocks anything that isn't a real
  browser; cookies (`NENEKO_COOKIES_FILE`) sometimes help.
- **Switching from calibre-web**: books that calibre-web synced stay on the Kobo; remove
  them there if you no longer want them.
- `NENEKO_LOG_LEVEL=DEBUG` logs every step. The Kobo token is masked in the logs.
- To see what the Kobo itself does during a Sync (which books it downloads, which requests
  fail), see [`docs/kobo-debugging.md`](docs/kobo-debugging.md).

## Versions

The version lives in one place, `src/neneko/__init__.py`. Image tags on `ghcr.io/keunes/neneko`:

- `latest`: the newest stable release;
- `0.2.1`, `0.2.1-dev.1`, …: one tag per release, never changed afterwards;
- `main`: the current development state.

To release, change `__version__` and push to `main`. CI then tags the image with the new version
and creates a GitHub release; versions with a suffix such as `-dev.1` become pre-releases and
don't move `latest`.

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
go install github.com/pgaskin/kepubify/v4/cmd/kepubify@v4.0.4   # optional, for the KEPUB tests
pytest
```

The tests run against an in-memory fake of the Nextcloud News API and a simulated Kobo; no
network access is needed.

## Credits

- The Kobo store protocol handling follows [calibre-web](https://github.com/janeczku/calibre-web)'s
  `cps/kobo.py` (GPL-3.0).
- The e-ink dither is ported from [Calibre](https://calibre-ebook.com) (GPL-3.0), which took it
  from [ImageMagick](https://imagemagick.org) (ImageMagick License).
- KEPUB conversion by [kepubify](https://github.com/pgaskin/kepubify) (MIT).

## License

[AGPL-3.0-or-later](LICENSE).
