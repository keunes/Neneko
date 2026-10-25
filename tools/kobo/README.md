# Starting over on the Kobo

`clear_neneko.py` removes every Neneko article from a Kobo connected over USB: the books and
their chapters, their reading and home-screen entries, their collection entries, the collections
that held only Neneko articles, and the book files and covers. Other books stay as they are.

Why not just remove them through Neneko? A book removed by a sync isn't deleted on the Kobo. Its
row stays (`___UserID` = `removed`), and when the article comes back the Kobo doesn't download it
by itself (see [`docs/kobo-notes.md`](../../docs/kobo-notes.md)). For a clean start, clear both
sides:

1. Stop Neneko and clear its database (the built articles in its cache can stay):

   ```sh
   docker compose stop neneko
   docker compose run --rm --no-deps --entrypoint sh neneko -c 'rm -f /data/neneko.sqlite*'
   ```

2. Connect the Kobo over USB and check what would be removed. Nothing is changed yet:

   ```sh
   python3 tools/kobo/clear_neneko.py /media/$USER/KOBOeReader
   ```

   It lists the counts and a few titles. The Neneko books are found by their IDs (see the
   script); with articles from more than one Nextcloud account it asks for `--prefix`.

3. Remove them. The script first copies `KoboReader.sqlite` into the current folder:

   ```sh
   python3 tools/kobo/clear_neneko.py /media/$USER/KOBOeReader --apply
   ```

   Neneko's collections can be empty by then (all their articles removed earlier); add
   `--include-empty-shelves` to remove empty collections too, after checking the list in step 2
   (it would also remove empty collections of your own).

4. Eject the Kobo, start Neneko again (`docker compose up -d`), and sync.

The script needs only Python 3. Run it while the Kobo is in USB mode (it doesn't use its database
then); to undo, copy the backup back to `.kobo/KoboReader.sqlite` the same way.
