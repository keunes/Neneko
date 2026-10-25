import base64
import json

import pytest

from neneko.config import Settings
from neneko.ids import instance_prefix, item_id_from_uuid, item_uuid, tag_uuid
from neneko.kobo.synctoken import SyncToken

PREFIX = instance_prefix("https://cloud.example.org", "reader")


@pytest.mark.parametrize("item_id", [0, 1, 1105, 2**40 + 7, (1 << 62) - 1])
def test_item_uuid_round_trip(item_id):
    value = item_uuid(PREFIX, item_id)
    assert value[14] == "8"  # UUID version 8
    assert item_id_from_uuid(PREFIX, value) == item_id


def test_item_uuid_rejects_other_ids():
    assert item_id_from_uuid(PREFIX, "3f0b2c4e-8d7a-4b1e-9c55-1a2b3c4d5e6f") is None
    assert item_id_from_uuid(PREFIX, "not-a-uuid") is None
    other = instance_prefix("https://cloud.example.org", "someone-else")
    assert item_id_from_uuid(other, item_uuid(PREFIX, 5)) is None


def test_item_uuid_is_stable():
    assert item_uuid(PREFIX, 1105) == item_uuid(instance_prefix("https://cloud.example.org/", "reader"), 1105)


def test_tag_uuid_deterministic_and_distinct():
    assert tag_uuid(PREFIX, "feed", 51) == tag_uuid(PREFIX, "feed", "51")
    assert tag_uuid(PREFIX, "feed", 51) != tag_uuid(PREFIX, "folder", 51)


def test_sync_token_round_trip():
    token = SyncToken(epoch="abc", gen=7, store_token="store.blob")
    assert SyncToken.parse(token.serialize()) == token


def test_sync_token_from_store_and_calibre_web():
    assert SyncToken.parse("part1.part2") == SyncToken(store_token="part1.part2")
    calibre = base64.b64encode(json.dumps(
        {"version": "1-1-0", "data": {"raw_kobo_store_token": "s.t", "books_last_modified": 0}}).encode()).decode()
    assert SyncToken.parse(calibre) == SyncToken(store_token="s.t")
    assert SyncToken.parse("@@garbage@@") == SyncToken()
    assert SyncToken.parse(None) == SyncToken()


def test_settings_lists_and_secret_files(tmp_path, monkeypatch):
    (tmp_path / "pw").write_text("secret\n")
    (tmp_path / "token").write_text("abcdefghijklmnop_1234\n")
    monkeypatch.setenv("NENEKO_NC_URL", "https://cloud.example.org/")
    monkeypatch.setenv("NENEKO_NC_USER", "reader")
    monkeypatch.setenv("NENEKO_NC_APP_PASSWORD_FILE", str(tmp_path / "pw"))
    monkeypatch.setenv("NENEKO_KOBO_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("NENEKO_PUBLIC_URL", "http://pi:8089")
    monkeypatch.setenv("NENEKO_NC_FOLDERS", "Tech, Long reads")
    monkeypatch.setenv("NENEKO_COLLECTIONS", "all,feed")
    settings = Settings()
    assert settings.nc_url == "https://cloud.example.org"
    assert settings.nc_app_password == "secret"
    assert settings.kobo_token == "abcdefghijklmnop_1234"
    assert settings.nc_folders == ["Tech", "Long reads"]
    assert settings.collections == ["all", "feed"]
    assert settings.kobo_base_url == "http://pi:8089/kobo/abcdefghijklmnop_1234"


def test_settings_reject_weak_token(monkeypatch):
    for key, value in {"NENEKO_NC_URL": "https://x", "NENEKO_NC_USER": "u", "NENEKO_NC_APP_PASSWORD": "p",
                       "NENEKO_PUBLIC_URL": "http://pi", "NENEKO_KOBO_TOKEN": "short"}.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        Settings()


def test_page_size_can_cover_a_whole_sync():
    base = dict(nc_url="https://x", nc_user="u", nc_app_password="p", public_url="http://pi",
                kobo_token="abcdefghijklmnop_1234")
    assert Settings(**base, new_per_sync_page=500).new_per_sync_page == 500
    with pytest.raises(ValueError):
        Settings(**base, new_per_sync_page=501)


def test_sync_token_from_a_re_offering_version_still_parses():
    # 0.2.1-dev.1 and dev.5 kept a page session in the token.
    old = base64.b64encode(json.dumps(
        {"neneko": 1, "epoch": "e", "gen": 3, "store": "", "session": 2, "resend_below": 1104}).encode()).decode()
    assert SyncToken.parse(old) == SyncToken(epoch="e", gen=3)


def test_old_database_is_migrated(tmp_path):
    import sqlite3

    from neneko.state import State

    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE ledger (item_id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, "
                     "removed_at REAL, kobo_status TEXT NOT NULL DEFAULT 'ReadyToRead'); PRAGMA user_version=1;")
    db.execute("INSERT INTO ledger (item_id, uuid) VALUES (1, 'u1')")
    db.commit()
    db.close()
    state = State(path)
    state.ledger_set_downloaded(1)
    row = state.ledger_get(1)
    assert row["downloaded_at"] is not None and row["reoffer_count"] == 0


def test_schema_2_database_gains_reoffer_tracking(tmp_path):
    import sqlite3

    from neneko.state import State

    path = tmp_path / "v2.sqlite"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE ledger (item_id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, "
                     "removed_at REAL, downloaded_at REAL); PRAGMA user_version=2;")
    db.execute("INSERT INTO ledger (item_id, uuid, downloaded_at) VALUES (1, 'u1', 123.0)")
    db.commit()
    db.close()
    state = State(path)
    row = state.ledger_get(1)
    assert (row["downloaded_at"], row["reoffer_count"], row["last_reoffered_at"]) == (123.0, 0, None)
    assert state._one("PRAGMA user_version")["user_version"] == 3


def test_version_has_a_single_source():
    import re
    from pathlib import Path

    import neneko

    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in pyproject and not re.search(r'^version\s*=\s*"', pyproject, re.M)
    assert re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", neneko.__version__)
