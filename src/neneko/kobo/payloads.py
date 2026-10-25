"""JSON shapes the Kobo expects, modelled on calibre-web's cps/kobo.py (GPL-3.0)."""

import json
from datetime import datetime, timezone

from ..builder.sanitize import plain_excerpt
from ..nextcloud import modified_at

GENERIC_CATEGORY = "00000000-0000-0000-0000-000000000001"


def timestamp(value: float | int | None = None) -> str:
    moment = datetime.now(timezone.utc) if value is None else datetime.fromtimestamp(value, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def arrival(row: dict) -> float | None:
    """When Neneko first saw the article, in Nextcloud's order (see sync.arrival_times). The Kobo
    sorts "Date added" by the entitlement's Created and "Recent" by the reading state's
    PriorityTimestamp, so this keeps Nextcloud's newest-first order instead of grouping articles
    by sync page (docs/kobo-notes.md). Rows from before 0.2.1-dev.8 fall back to lastModified."""
    if row.get("arrived_at"):
        return row["arrived_at"]
    item = json.loads(row["item_json"]) if row.get("item_json") else {}
    return modified_at(item) or row.get("delivered_at")


def entitlement(row: dict, *, removed: bool) -> dict:
    uuid = row["uuid"]
    return {
        "Accessibility": "Full",
        "ActivePeriod": {"From": timestamp()},
        "Created": timestamp(arrival(row)),
        "CrossRevisionId": uuid,
        "Id": uuid,
        "IsRemoved": removed,
        "IsHiddenFromArchive": False,
        "IsLocked": False,
        "LastModified": timestamp(),
        "OriginCategory": "Imported",
        "RevisionId": uuid,
        "Status": "Active",
    }


def metadata(row: dict, *, download_url: str, download_format: str, size: int, language: str) -> dict:
    uuid = row["uuid"]
    item = json.loads(row["item_json"]) if row.get("item_json") else {}
    feed = row.get("feed_title") or ""
    return {
        "Categories": [GENERIC_CATEGORY],
        "ContributorRoles": [{"Name": feed}] if feed else [],
        "Contributors": [feed] if feed else None,
        "CoverImageId": uuid,
        "CrossRevisionId": uuid,
        "CurrentDisplayPrice": {"CurrencyCode": "USD", "TotalAmount": 0},
        "CurrentLoveDisplayPrice": {"TotalAmount": 0},
        "Description": plain_excerpt(item.get("body") or "") or None,
        "DownloadUrls": [{
            "Format": download_format,
            "Size": size,
            "Url": download_url,
            "Platform": "Generic",
        }],
        "EntitlementId": uuid,
        "ExternalIds": [],
        "Genre": GENERIC_CATEGORY,
        "IsEligibleForKoboLove": False,
        "IsInternetArchive": False,
        "IsPreOrder": False,
        "IsSocialEnabled": True,
        "Language": language,
        "PhoneticPronunciations": {},
        "PublicationDate": timestamp(item.get("pubDate")),
        "Publisher": {"Imprint": "", "Name": feed},
        "RevisionId": uuid,
        "Title": row.get("title") or item.get("title") or "",
        "WorkId": uuid,
    }


def reading_state(row: dict) -> dict:
    # The Kobo's DateLastRead ("Recent") becomes the latest of PriorityTimestamp and the three
    # LastModified below (ReadingStateParser::parseReadingState), so until the Kobo reports on the
    # article, all four are its arrival time.
    status_info = {
        "LastModified": timestamp(row.get("status_at") or arrival(row)),
        "Status": row.get("kobo_status") or "ReadyToRead",
        "TimesStartedReading": row.get("times_started") or 0,
    }
    if row.get("last_started_at"):
        status_info["LastTimeStartedReading"] = timestamp(row["last_started_at"])
    bookmark = json.loads(row["bookmark_json"]) if row.get("bookmark_json") else {}
    statistics = json.loads(row["statistics_json"]) if row.get("statistics_json") else {}
    reported = timestamp(row.get("state_at") or arrival(row))
    return {
        "EntitlementId": row["uuid"],
        "Created": timestamp(arrival(row)),
        "LastModified": timestamp(row.get("state_at") or row.get("delivered_at")),
        "PriorityTimestamp": reported,
        "StatusInfo": status_info,
        "Statistics": {"LastModified": reported, **statistics},
        "CurrentBookmark": {"LastModified": reported, **bookmark},
    }


def tag(*, uuid: str, name: str, items: list[str], created: float, modified: float) -> dict:
    return {
        "Tag": {
            "Created": timestamp(created),
            "Id": uuid,
            "Items": [{"RevisionId": item, "Type": "ProductRevisionTagItem"} for item in items],
            "LastModified": timestamp(modified),
            "Name": name,
            "Type": "UserTag",
        }
    }


def deleted_tag(*, uuid: str, modified: float) -> dict:
    return {"Tag": {"Id": uuid, "LastModified": timestamp(modified)}}
