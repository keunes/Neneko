"""Make Neneko articles on a Kobo connected over USB download again, e.g. after a rebuild.

    python3 tools/kobo/redownload.py /media/you/KOBOeReader 30654 30642            # report only
    python3 tools/kobo/redownload.py /media/you/KOBOeReader 30654 30642 --apply    # reset

The Kobo never downloads a book again that it lists as downloaded, whatever the server sends
(docs/kobo-notes.md), so a rebuilt article doesn't reach it by itself. This puts the given
articles back in the state of one that was never downloaded: the book file goes, its chapter
rows and cover record go, and IsDownloaded becomes 'false'. The book's own row stays, so the
article stays in the library and its collections, with its reading progress. Tapping it then
downloads the current build from Neneko. Delete the old build from Neneko's cache first,
otherwise the Kobo gets the same file again.

Articles are given by their Nextcloud News item ID (as in Neneko's log, "built item 30654") or
by their Kobo book ID. Before --apply it copies the database to the current folder. Needs only
Python 3. See tools/kobo/README.md.
"""

import argparse
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path


def neneko_prefixes(db: sqlite3.Connection) -> set[str]:
    """The fixed start of Neneko book IDs, as in clear_neneko.py (kept here so this file works
    on its own)."""
    rows = db.execute("SELECT ContentID FROM content WHERE ContentType = 6 AND WorkId = ContentID")
    return {cid[:19] for (cid,) in rows if len(cid) == 36 and cid[14] == "8" and cid[19] in "89ab"}


def book_id(prefix: str, item_id: int) -> str:
    """The Kobo ID of a News item (src/neneko/ids.py): the account prefix, then the item ID
    with the UUID variant in its top bits."""
    raw = bytearray(bytes.fromhex(prefix.replace("-", "")) + item_id.to_bytes(8, "big"))
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("kobo", type=Path, help="where the Kobo is mounted")
    parser.add_argument("articles", nargs="+", help="News item IDs or Kobo book IDs")
    parser.add_argument("--apply", action="store_true", help="reset (otherwise only report)")
    parser.add_argument("--prefix", help="start of Neneko's book IDs, e.g. f1a68315-8586-8980-")
    args = parser.parse_args()

    db_path = args.kobo / ".kobo" / "KoboReader.sqlite"
    if not db_path.is_file():
        sys.exit(f"no Kobo database at {db_path}")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    if all(len(a) == 36 for a in args.articles):
        prefix = None
    else:
        prefixes = {args.prefix} if args.prefix else neneko_prefixes(db)
        if len(prefixes) != 1:
            sys.exit(f"found {len(prefixes)} Neneko ID prefixes ({', '.join(sorted(prefixes)) or 'none'}); "
                     "pass one with --prefix")
        prefix = prefixes.pop()

    resets = []
    for article in args.articles:
        cid = article if len(article) == 36 else book_id(prefix, int(article))
        row = db.execute("SELECT Title, IsDownloaded, ___UserID FROM content WHERE ContentID = ? AND ContentType = 6",
                         (cid,)).fetchone()
        path = args.kobo / ".kobo" / "kepub" / cid
        if row is None:
            print(f"{article}: not on this Kobo ({cid})")
        elif row[2] == "removed":
            print(f"{article}: {row[0]!r} was removed from the library; it comes back when it is unread in "
                  "Nextcloud, and downloads when tapped")
        elif row[1] != "true" and not path.exists():
            print(f"{article}: {row[0]!r} isn't downloaded; tapping it downloads the current build")
        else:
            chapters = db.execute("SELECT count(*) FROM content WHERE BookID = ?", (cid,)).fetchone()[0]
            print(f"{article}: {row[0]!r}: {chapters} chapter rows, "
                  f"{'book file' if path.exists() else 'no book file'}")
            resets.append((cid, path))
    db.close()
    if not resets:
        print("Nothing to reset.")
        return
    if not args.apply:
        print(f"Nothing changed; add --apply to reset {len(resets)} article(s).")
        return

    backup = Path.cwd() / f"KoboReader-{time.strftime('%Y%m%d-%H%M%S')}.sqlite"
    shutil.copy2(db_path, backup)
    print(f"backup: {backup}")
    db = sqlite3.connect(db_path)
    with db:
        for cid, _ in resets:
            db.execute("DELETE FROM content WHERE BookID = ?", (cid,))
            db.execute("DELETE FROM volume_shortcovers WHERE volumeId = ?", (cid,))
            db.execute("UPDATE content SET IsDownloaded = 'false' WHERE ContentID = ?", (cid,))
    check = db.execute("PRAGMA integrity_check").fetchone()[0]
    db.close()
    for _, path in resets:
        path.unlink(missing_ok=True)
    print(f"reset {len(resets)} article(s) (database check: {check}). Eject the Kobo before unplugging it, "
          "then tap each article to download it.")


if __name__ == "__main__":
    main()
