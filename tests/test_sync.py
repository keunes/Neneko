import time

from neneko.ids import item_id_from_uuid, item_uuid

from .conftest import TOKEN, entitlements, ids_of


def uuid_of(kobo, item_id: int) -> str:
    return item_uuid(kobo.ctx.prefix, item_id)


def flush(kobo) -> None:
    kobo.client.portal.call(kobo.ctx.engine.flush_outbox)


def tags(results, kind="NewTag"):
    return {r[kind]["Tag"]["Name"]: r[kind]["Tag"] for r in results if kind in r}


def test_first_sync_delivers_unread_items_with_collections(make_kobo, news):
    for _ in range(3):
        news.add_item(51)
    news.add_item(35)
    news.add_item(60)  # feed without a folder
    news.add_item(51, unread=False)
    kobo = make_kobo()

    results = kobo.sync()

    new = entitlements(results, "NewEntitlement")
    assert len(new) == 5
    metadata = new[0]["BookMetadata"]
    assert metadata["DownloadUrls"][0]["Url"].startswith(f"http://neneko.local:8089/kobo/{TOKEN}/download/")
    assert metadata["Publisher"]["Name"] in {"Example Tech Daily", "Podcast Weekly", "Loose feed"}
    names = tags(results)
    # Default collections: everything, plus one per folder (the loose feed has no folder).
    assert set(names) == {"Neneko", "Neneko · Tech", "Neneko · Long reads"}
    assert len(names["Neneko"]["Items"]) == 5
    assert len(names["Neneko · Tech"]["Items"]) == 3
    # Every ID maps back to a Nextcloud item.
    assert {item_id_from_uuid(kobo.ctx.prefix, e["BookEntitlement"]["Id"]) for e in new} == news.unread_ids()


def test_second_sync_is_empty_once_acknowledged(make_kobo, news):
    news.add_item(51)
    kobo = make_kobo()
    assert kobo.sync()
    assert kobo.sync() == []


def test_unacknowledged_response_is_resent(make_kobo, news):
    item = news.add_item(51)
    kobo = make_kobo()
    first = kobo.sync(keep_token=False)  # the Kobo never got this response
    second = kobo.sync()
    assert ids_of(first, "NewEntitlement") == ids_of(second, "NewEntitlement") == [uuid_of(kobo, item["id"])]
    assert "NewTag" in str(second)
    assert kobo.sync() == []


def test_collections_are_configurable(make_kobo, news):
    news.add_item(51)
    kobo = make_kobo(collections=["feed"], collection_feed_template="📰 {feed} ({folder})")
    assert set(tags(kobo.sync())) == {"📰 Example Tech Daily (Tech)"}


def test_switching_collection_settings_renames_and_deletes(make_kobo, news):
    news.add_item(51)
    news.add_item(35)
    old = make_kobo(collections=["all", "folder", "feed"], collection_all_name="Neneko Sync")
    first = old.sync()
    assert set(tags(first)) == {"Neneko Sync", "Neneko · Tech", "Neneko · Long reads", "Example Tech Daily",
                                "Podcast Weekly"}
    old_ids = {name: tag["Id"] for name, tag in tags(first).items()}
    old.client.__exit__(None, None, None)

    new = make_kobo()  # same database, default collections
    new.token = old.token
    results = new.sync()
    assert {tag["Name"]: tag["Id"] for tag in tags(results, "ChangedTag").values()} == {
        "Neneko": old_ids["Neneko Sync"]}
    deleted = {r["DeletedTag"]["Tag"]["Id"] for r in results if "DeletedTag" in r}
    assert deleted == {old_ids["Example Tech Daily"], old_ids["Podcast Weekly"]}
    assert new.sync() == []


def test_folder_filter(make_kobo, news):
    tech = news.add_item(51)
    news.add_item(35)
    kobo = make_kobo(nc_folders=["tech"])
    assert ids_of(kobo.sync(), "NewEntitlement") == [uuid_of(kobo, tech["id"])]


def test_window_keeps_newest_and_drops_untouched_old_ones(make_kobo, news):
    items = [news.add_item(51) for _ in range(3)]
    kobo = make_kobo(max_items=3)
    kobo.sync()
    newer = news.add_item(51)
    results = kobo.sync()
    assert ids_of(results, "NewEntitlement") == [uuid_of(kobo, newer["id"])]
    assert ids_of(results, "ChangedEntitlement", removed=True) == [uuid_of(kobo, items[0]["id"])]
    assert news.items[items[0]["id"]]["unread"]  # still unread in Nextcloud


def test_article_being_read_is_kept_outside_the_window(make_kobo, news):
    items = [news.add_item(51) for _ in range(2)]
    kobo = make_kobo(max_items=2)
    kobo.sync()
    assert kobo.put_state(uuid_of(kobo, items[0]["id"]), "Reading").status_code == 200
    newer = news.add_item(51)
    results = kobo.sync()
    # One slot is taken by the article being read, so only the newest unread fits.
    assert ids_of(results, "NewEntitlement") == [uuid_of(kobo, newer["id"])]
    assert ids_of(results, "ChangedEntitlement", removed=True) == [uuid_of(kobo, items[1]["id"])]


def test_read_elsewhere_is_removed(make_kobo, news):
    item = news.add_item(51)
    kobo = make_kobo()
    kobo.sync()
    news.set_read(item["id"])
    results = kobo.sync()
    assert ids_of(results, "ChangedEntitlement", removed=True) == [uuid_of(kobo, item["id"])]
    assert "DeletedTag" in str(results)  # collections that became empty
    assert kobo.sync() == []


def test_finished_marks_read_then_leaves_after_grace(make_kobo, news):
    item = news.add_item(51)
    kobo = make_kobo(finished_grace_hours=1)
    kobo.sync()
    response = kobo.put_state(uuid_of(kobo, item["id"]), "Finished",
                              bookmark={"ProgressPercent": 100, "Location": {"Value": "x", "Type": "KoboSpan", "Source": "y"}})
    assert response.json()["RequestResult"] == "Success"
    flush(kobo)
    assert ("read", [item["id"]]) in news.marks
    assert kobo.sync() == []  # read in Nextcloud by Neneko itself: stays during the grace period

    state = kobo.client.get(kobo.url(f"/v1/library/{uuid_of(kobo, item['id'])}/state")).json()[0]
    assert state["StatusInfo"]["Status"] == "Finished"
    assert state["CurrentBookmark"]["ProgressPercent"] == 100

    kobo.ctx.state._exec("UPDATE ledger SET finished_at = ?", (time.time() - 7200,))
    assert ids_of(kobo.sync(), "ChangedEntitlement", removed=True) == [uuid_of(kobo, item["id"])]


def test_mark_as_unread_on_kobo(make_kobo, news):
    item = news.add_item(51)
    kobo = make_kobo()
    kobo.sync()
    kobo.put_state(uuid_of(kobo, item["id"]), "Finished")
    flush(kobo)
    kobo.put_state(uuid_of(kobo, item["id"]), "ReadyToRead")
    flush(kobo)
    assert news.marks == [("read", [item["id"]]), ("unread", [item["id"]])]
    assert news.items[item["id"]]["unread"]
    assert kobo.sync() == []  # still on the Kobo, nothing to change


def test_mark_read_on_opened_is_the_default(make_kobo, news):
    item = news.add_item(51)
    kobo = make_kobo()
    assert kobo.ctx.settings.mark_read_on == "opened"
    kobo.sync()
    kobo.put_state(uuid_of(kobo, item["id"]), "Reading")
    flush(kobo)
    assert news.marks == [("read", [item["id"]])]
    assert kobo.sync() == []  # read by Neneko, being read: stays


def test_mark_read_on_finished_waits_for_the_end(make_kobo, news):
    item = news.add_item(51)
    kobo = make_kobo(mark_read_on="finished")
    kobo.sync()
    kobo.put_state(uuid_of(kobo, item["id"]), "Reading")
    flush(kobo)
    assert news.marks == []
    kobo.put_state(uuid_of(kobo, item["id"]), "Finished")
    flush(kobo)
    assert news.marks == [("read", [item["id"]])]


def test_mark_read_on_delivered_keeps_inbox_bounded(make_kobo, news):
    items = [news.add_item(51) for _ in range(2)]
    kobo = make_kobo(mark_read_on="delivered", max_items=2)
    kobo.sync()
    flush(kobo)
    assert sorted(news.marks[0][1]) == sorted(i["id"] for i in items)
    assert not news.unread_ids()
    assert kobo.sync() == []  # read by Neneko: kept
    newer = news.add_item(51)
    results = kobo.sync()
    assert ids_of(results, "NewEntitlement") == [uuid_of(kobo, newer["id"])]
    assert ids_of(results, "ChangedEntitlement", removed=True) == [uuid_of(kobo, items[0]["id"])]


def test_outbox_survives_nextcloud_outage(make_kobo, news):
    item = news.add_item(51)
    kobo = make_kobo()
    kobo.sync()
    news.down = True
    kobo.put_state(uuid_of(kobo, item["id"]), "Finished")
    flush(kobo)
    assert len(kobo.ctx.state.outbox_pending()) == 1
    assert kobo.sync() == []  # sync still answers while Nextcloud is down
    news.down = False
    kobo.sync()
    assert ("read", [item["id"]]) in news.marks
    assert kobo.ctx.state.outbox_pending() == []


def test_delete_on_kobo(make_kobo, news):
    item = news.add_item(51)
    kobo = make_kobo(delete_action="forget")
    kobo.sync()
    assert kobo.client.delete(kobo.url(f"/v1/library/{uuid_of(kobo, item['id'])}")).status_code == 204
    flush(kobo)
    assert news.marks == []  # forget: Nextcloud untouched
    assert ids_of(kobo.sync(), "NewEntitlement") == []  # not sent again while unread


def test_delete_on_kobo_marks_read_by_default(make_kobo, news):
    item = news.add_item(51)
    kobo = make_kobo()
    kobo.sync()
    kobo.client.delete(kobo.url(f"/v1/library/{uuid_of(kobo, item['id'])}"))
    flush(kobo)
    assert news.marks == [("read", [item["id"]])]


def test_changed_article_is_resent(make_kobo, news):
    item = news.add_item(51)
    kobo = make_kobo()
    kobo.sync()
    news.items[item["id"]]["contentHash"] = "updated"
    news.items[item["id"]]["title"] = "Updated title"
    results = kobo.sync()
    changed = entitlements(results, "ChangedEntitlement")
    assert [c["BookMetadata"]["Title"] for c in changed] == ["Updated title"]


def test_database_loss_reuses_ids_and_sweeps_read_items(make_kobo, news, make_settings):
    keep = news.add_item(51)
    gone = news.add_item(51)
    kobo = make_kobo()
    kobo.sync()
    old_token = kobo.token
    news.set_read(gone["id"])

    # Same data dir wiped: a fresh Neneko with an empty database, same Kobo token.
    kobo.client.__exit__(None, None, None)
    kobo.ctx.settings.db_path.unlink()
    for suffix in ("-wal", "-shm"):
        kobo.ctx.settings.db_path.with_name(kobo.ctx.settings.db_path.name + suffix).unlink(missing_ok=True)
    fresh = make_kobo()
    fresh.token = old_token
    results = fresh.sync()
    assert ids_of(results, "NewEntitlement") == [uuid_of(fresh, keep["id"])]
    assert uuid_of(fresh, gone["id"]) in ids_of(results, "ChangedEntitlement", removed=True)
    assert uuid_of(fresh, keep["id"]) == uuid_of(kobo, keep["id"])


def test_filtered_items_are_skipped(make_kobo, news):
    news.add_item(51, filtered=True)
    kobo = make_kobo()
    assert ids_of(kobo.sync(), "NewEntitlement") == []


def test_more_than_one_page(make_kobo, news):
    for _ in range(130):
        news.add_item(51)
    kobo = make_kobo(max_items=150)
    results = kobo.sync()
    assert kobo.pages == 2
    assert len(ids_of(results, "NewEntitlement")) == 130


def test_article_being_read_is_not_replaced_when_it_changes(make_kobo, news):
    item = news.add_item(51)
    kobo = make_kobo()
    kobo.sync()
    kobo.put_state(uuid_of(kobo, item["id"]), "Reading")
    news.items[item["id"]]["contentHash"] = "updated"
    assert kobo.sync() == []


def test_recovery_sweep_is_bounded(make_kobo, news):
    kobo = make_kobo(max_items=10)
    kobo.sync()
    old_token = kobo.token
    for _ in range(300):
        news.add_item(51, unread=False)
    kobo.client.__exit__(None, None, None)
    for suffix in ("", "-wal", "-shm"):
        kobo.ctx.settings.db_path.with_name(kobo.ctx.settings.db_path.name + suffix).unlink(missing_ok=True)
    fresh = make_kobo(max_items=10)
    fresh.token = old_token
    removed = ids_of(fresh.sync(), "ChangedEntitlement", removed=True)
    assert len(removed) == 200
    assert removed[0] == uuid_of(fresh, max(news.items))  # newest first


def test_new_books_come_with_a_reading_state(make_kobo, news):
    news.add_item(51)
    kobo = make_kobo()
    entry = entitlements(kobo.sync(), "NewEntitlement")[0]
    state = entry["ReadingState"]
    assert state["EntitlementId"] == entry["BookEntitlement"]["Id"]
    assert state["StatusInfo"]["Status"] == "ReadyToRead"
    assert state["StatusInfo"]["TimesStartedReading"] == 0
    assert state["Created"] == state["LastModified"] == state["PriorityTimestamp"] \
        == state["StatusInfo"]["LastModified"] == state["CurrentBookmark"]["LastModified"]


def test_sync_reports_the_real_file_size(make_kobo, news, monkeypatch):
    from neneko.builder.queue import Builder

    def fake_build(self, row):
        path = self.article_path(row)
        path.write_bytes(b"x" * (1000 + row["item_id"]))
        self.state.ledger_set_size(row["item_id"], path.stat().st_size)
        return path

    monkeypatch.setattr(Builder, "build", fake_build)
    items = [news.add_item(51) for _ in range(3)]
    kobo = make_kobo(start_workers=True)
    results = entitlements(kobo.sync(), "NewEntitlement")
    sizes = {e["BookEntitlement"]["Id"]: e["BookMetadata"]["DownloadUrls"][0]["Size"] for e in results}
    assert sizes == {uuid_of(kobo, i["id"]): 1000 + i["id"] for i in items}
    status = kobo.client.get("/").text
    assert "Downloaded by the Kobo</th><td>3" in status
