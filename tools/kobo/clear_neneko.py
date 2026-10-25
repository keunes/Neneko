"""Remove every Neneko article from a Kobo connected over USB, for a fresh start.

    python3 tools/kobo/clear_neneko.py /media/you/KOBOeReader            # report only
    python3 tools/kobo/clear_neneko.py /media/you/KOBOeReader --apply    # remove

Removing articles through a sync isn't enough: the Kobo keeps a removed book's row (with
___UserID 'removed') and won't download it by itself when it comes back (docs/kobo-notes.md).
This removes the rows from the Kobo's database instead: the books and their chapters, their
home-screen and reading events, their collection entries, collections that held only Neneko
articles, and the book files and covers. Other books stay untouched. Before --apply it copies
the database to the current folder.

Neneko's books are found by their IDs: version-8 UUIDs (Kobo's and calibre-web's are version 4)
that are also their own WorkId, sharing a start that encodes the Nextcloud account. --prefix
gives that start instead. Needs only Python 3. See tools/kobo/README.md.
"""

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path


def neneko_prefixes(db: sqlite3.Connection) -> set[str]:
    """The fixed start of Neneko book IDs (src/neneko/ids.py): their first 8 bytes encode the
    Nextcloud account, and the UUID version is 8."""
    rows = db.execute("SELECT ContentID FROM content WHERE ContentType = 6 AND WorkId = ContentID")
    return {cid[:19] for (cid,) in rows if len(cid) == 36 and cid[14] == "8" and cid[19] in "89ab"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("kobo", type=Path, help="where the Kobo is mounted")
    parser.add_argument("--apply", action="store_true", help="remove (otherwise only report)")
    parser.add_argument("--prefix", help="start of Neneko's book IDs, e.g. f1a68315-8586-8980-")
    parser.add_argument("--include-empty-shelves", action="store_true",
                        help="also remove collections without any book (Neneko's can end up empty)")
    args = parser.parse_args()

    db_path = args.kobo / ".kobo" / "KoboReader.sqlite"
    if not db_path.is_file():
        sys.exit(f"no Kobo database at {db_path}")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    prefixes = {args.prefix} if args.prefix else neneko_prefixes(db)
    if not prefixes:
        print("No Neneko books on this Kobo.")
        return
    if len(prefixes) > 1:
        sys.exit(f"found {len(prefixes)} Neneko ID prefixes ({', '.join(sorted(prefixes)) or 'none'}); "
                 "pass one with --prefix")
    like = prefixes.pop() + "%"
    titles = [t for (t,) in db.execute(
        "SELECT Title FROM content WHERE ContentType = 6 AND ContentID LIKE ? LIMIT 3", (like,))]
    print(f"Neneko books: IDs starting with {like[:-1]}, e.g. {'; '.join(titles)}")

    shelves = [name for (name,) in db.execute(
        "SELECT DISTINCT ShelfName FROM ShelfContent WHERE ContentId LIKE ?", (like,))]
    only_ours = [s for s in shelves if not db.execute(
        "SELECT 1 FROM ShelfContent WHERE ShelfName = ? AND ContentId NOT LIKE ?", (s, like)).fetchone()]
    empty = [name for (name,) in db.execute(
        "SELECT Name FROM Shelf WHERE Name NOT IN (SELECT ShelfName FROM ShelfContent)")]
    removed_shelves = only_ours + (empty if args.include_empty_shelves else [])
    deletes = [
        ("books and chapters", "DELETE FROM content WHERE ContentID LIKE ? OR BookID LIKE ?", (like, like)),
        ("cover records", "DELETE FROM volume_shortcovers WHERE volumeId LIKE ?", (like,)),
        ("reading events", "DELETE FROM Event WHERE ContentID LIKE ?", (like,)),
        ("home-screen entries", "DELETE FROM Activity WHERE Id LIKE ?", (like,)),
        ("collection entries", "DELETE FROM ShelfContent WHERE ContentId LIKE ?", (like,)),
    ] + [(f"collection {name!r}", "DELETE FROM Shelf WHERE Name = ?", (name,)) for name in removed_shelves]
    kepubs = sorted((args.kobo / ".kobo" / "kepub").glob(like[:-1] + "*"))
    images = sorted((args.kobo / ".kobo-images").rglob(like[:-1] + "*"))

    for label, sql, params in deletes:
        print(f"{db.execute(sql.replace('DELETE', 'SELECT count(*)', 1), params).fetchone()[0]:6d}  {label}")
    print(f"{len(kepubs):6d}  book files")
    print(f"{len(images):6d}  cover images")
    kept = [s for s in shelves if s not in only_ours]
    if kept:
        print("collections kept (they also hold other books):", ", ".join(kept))
    if empty and not args.include_empty_shelves:
        print("empty collections kept (add --include-empty-shelves to remove):", ", ".join(empty))
    db.close()
    if not args.apply:
        print("Nothing changed; add --apply to remove.")
        return

    backup = Path.cwd() / f"KoboReader-{time.strftime('%Y%m%d-%H%M%S')}.sqlite"
    shutil.copy2(db_path, backup)
    print(f"backup: {backup}")
    db = sqlite3.connect(db_path)
    with db:
        for _, sql, params in deletes:
            db.execute(sql, params)
    check = db.execute("PRAGMA integrity_check").fetchone()[0]
    db.close()
    for path in kepubs + images:
        path.unlink()
    print(f"removed (database check: {check}). Eject the Kobo before unplugging it.")


if __name__ == "__main__":
    main()
