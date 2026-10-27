"""Which feeds Neneko syncs: the NENEKO_INCLUDE_* and NENEKO_EXCLUDE_* rules.

1. Start: the feeds in NENEKO_INCLUDE_FOLDERS if set; nothing if NENEKO_INCLUDE_FEEDS is the
   only folder or include rule; otherwise all feeds.
2. Minus the feeds in NENEKO_EXCLUDE_FOLDERS.
3. Plus NENEKO_INCLUDE_FEEDS (also from an excluded folder: a feed rule is more specific).
4. Minus NENEKO_EXCLUDE_FEEDS.

Folders match by name, feeds by title or ID; both ignore case.
"""

from collections.abc import Iterable

ALL, FOLDER, FEED = "all", "folder", "feed"


class Scope:
    def __init__(self, folder_names: dict[int, str], feeds: dict[int, dict], *,
                 include_folders: Iterable[str] = (), exclude_folders: Iterable[str] = (),
                 include_feeds: Iterable[str] = (), exclude_feeds: Iterable[str] = ()):
        self._feeds = feeds
        self._rules = {
            "include_folders": list(include_folders), "exclude_folders": list(exclude_folders),
            "include_feeds": list(include_feeds), "exclude_feeds": list(exclude_feeds),
        }
        self._unknown: list[str] = []
        self.include_folders = self._folder_ids(folder_names, self._rules["include_folders"])
        self.exclude_folders = self._folder_ids(folder_names, self._rules["exclude_folders"])
        self.include_feeds = self._feed_ids(self._rules["include_feeds"])
        self.exclude_feeds = self._feed_ids(self._rules["exclude_feeds"])
        if self._rules["include_folders"]:
            self._start = FOLDER
        elif self._rules["include_feeds"] and not self._rules["exclude_folders"]:
            self._start = None
        else:
            self._start = ALL

    def _folder_ids(self, folder_names: dict[int, str], names: list[str]) -> set[int]:
        ids = set()
        for name in names:
            found = {fid for fid, folder in folder_names.items() if folder.casefold() == name.casefold()}
            if not found:
                self._unknown.append(f"folder {name!r}")
            ids |= found
        return ids

    def _feed_ids(self, names: list[str]) -> set[int]:
        ids = set()
        for name in names:
            found = {fid for fid, feed in self._feeds.items()
                     if name.casefold() in {str(fid), (feed.get("title") or "").casefold()}}
            if not found:
                self._unknown.append(f"feed {name!r}")
            ids |= found
        return ids

    def allows(self, feed_id: int | None) -> bool:
        if feed_id in self.exclude_feeds:
            return False
        if feed_id in self.include_feeds:
            return True
        feed = self._feeds.get(feed_id) or {}
        folder_id = feed.get("folderId") or None
        if folder_id in self.exclude_folders:
            return False
        if self._start == FOLDER:
            return folder_id in self.include_folders
        return self._start == ALL

    def sources(self) -> list[tuple[str, int]]:
        """What to ask Nextcloud for: everything, or only the included folders and feeds."""
        if self._start == ALL:
            return [(ALL, 0)]
        sources = [(FOLDER, fid) for fid in sorted(self.include_folders)] if self._start == FOLDER else []
        return sources + [(FEED, fid) for fid in sorted(self.include_feeds)]

    def unknown_names(self) -> list[str]:
        return list(self._unknown)

    def describe(self) -> str:
        rules = self._rules
        parts = []
        if rules["include_folders"]:
            parts.append(f"folders {', '.join(rules['include_folders'])}")
        elif self._start == ALL:
            parts.append("all feeds")
        if rules["include_feeds"]:
            parts.append(f"feeds {', '.join(rules['include_feeds'])}")
        text = "syncing " + " plus ".join(parts)
        excluded = []
        if rules["exclude_folders"]:
            excluded.append(f"folders {', '.join(rules['exclude_folders'])}")
        if rules["exclude_feeds"]:
            excluded.append(f"feeds {', '.join(rules['exclude_feeds'])}")
        return text + (f", except {' and '.join(excluded)}" if excluded else "")
