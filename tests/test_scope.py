import pytest

from neneko.scope import Scope

FOLDERS = {1: "Tech", 2: "News", 3: "Sport"}
FEEDS = {
    10: {"id": 10, "title": "Ars Technica", "folderId": 1},
    11: {"id": 11, "title": "The Verge", "folderId": 1},
    20: {"id": 20, "title": "BBC News", "folderId": 2},
    21: {"id": 21, "title": "CNN", "folderId": 2},
    30: {"id": 30, "title": "ESPN", "folderId": 3},
    40: {"id": 40, "title": "A feed without folder", "folderId": None},
}


def synced(**rules) -> set[int]:
    scope = Scope(FOLDERS, FEEDS, **rules)
    return {feed_id for feed_id in FEEDS if scope.allows(feed_id)}


# The examples from the README, section "Which feeds".
@pytest.mark.parametrize("rules, expected", [
    ({}, {10, 11, 20, 21, 30, 40}),
    ({"exclude_feeds": ["BBC News", "CNN"]}, {10, 11, 30, 40}),
    ({"include_folders": ["Tech"]}, {10, 11}),
    ({"include_feeds": ["Ars Technica"]}, {10}),
    ({"include_folders": ["Tech"], "include_feeds": ["BBC News"]}, {10, 11, 20}),
    ({"include_folders": ["Tech", "News"], "exclude_feeds": ["CNN"]}, {10, 11, 20}),
    ({"exclude_folders": ["Sport"]}, {10, 11, 20, 21, 40}),
    ({"exclude_folders": ["News"], "include_feeds": ["BBC News"]}, {10, 11, 20, 30, 40}),
])
def test_readme_examples(rules, expected):
    assert synced(**rules) == expected


def test_names_ignore_case_and_feeds_match_by_id():
    assert synced(include_folders=["tech"], exclude_feeds=["the verge"]) == {10}
    assert synced(include_feeds=["30", "cnn"]) == {21, 30}


def test_exclude_wins_within_the_same_kind():
    assert synced(include_folders=["Tech"], exclude_folders=["Tech"]) == set()
    assert synced(include_feeds=["CNN"], exclude_feeds=["CNN"]) == set()


def test_feeds_without_folder_follow_the_feed_rules():
    assert 40 not in synced(include_folders=["Tech"])
    assert 40 in synced(include_folders=["Tech"], include_feeds=["40"])
    assert 40 not in synced(exclude_feeds=["A feed without folder"])


def test_sources_ask_nextcloud_only_for_what_is_needed():
    assert Scope(FOLDERS, FEEDS).sources() == [("all", 0)]
    assert Scope(FOLDERS, FEEDS, exclude_feeds=["CNN"]).sources() == [("all", 0)]
    assert Scope(FOLDERS, FEEDS, include_folders=["Tech"], include_feeds=["CNN"]).sources() == [
        ("folder", 1), ("feed", 21)]
    assert Scope(FOLDERS, FEEDS, include_feeds=["ESPN"]).sources() == [("feed", 30)]
    # Excluded folders start from everything, so an included feed in one is fetched with it.
    assert Scope(FOLDERS, FEEDS, exclude_folders=["News"], include_feeds=["BBC News"]).sources() == [("all", 0)]


def test_unknown_names_and_description():
    scope = Scope(FOLDERS, FEEDS, include_folders=["Tech", "Nope"], exclude_feeds=["Missing"])
    assert scope.unknown_names() == ["folder 'Nope'", "feed 'Missing'"]
    assert Scope(FOLDERS, FEEDS).describe() == "syncing all feeds"
    assert Scope(FOLDERS, FEEDS, include_folders=["Tech"], include_feeds=["CNN"],
                 exclude_feeds=["The Verge"]).describe() == (
        "syncing folders Tech plus feeds CNN, except feeds The Verge")
    assert Scope(FOLDERS, FEEDS, include_feeds=["CNN"]).describe() == "syncing feeds CNN"
