"""The sync token: state the Kobo stores for us and sends back on every sync.

Neneko keeps three things in it: the database epoch, the generation of the last sync
response the Kobo received, and the Kobo store's own token (for the store proxy). Between
the pages of one Sync it also says where that Sync started and how far re-offering got.
"""

import base64
import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

HEADER = "x-kobo-synctoken"


@dataclass
class SyncToken:
    epoch: str | None = None
    gen: int = 0
    store_token: str = ""
    # Only while answering "continue" (pages of one Sync): the generation the Sync started at, and
    # where re-offering not-yet-downloaded articles continues (below this item ID).
    session: int | None = None
    resend_below: int | None = None

    @classmethod
    def parse(cls, value: str | None) -> "SyncToken":
        if not value:
            return cls()
        if "." in value:
            # First sync after switching from the real store: a store token (two base64 blobs).
            return cls(store_token=value)
        try:
            data = json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))
        except (ValueError, TypeError):
            log.info("unreadable sync token, starting fresh")
            return cls()
        if not isinstance(data, dict):
            return cls()
        if "neneko" in data:
            return cls(
                epoch=data.get("epoch") or None,
                gen=int(data.get("gen") or 0),
                store_token=data.get("store") or "",
                session=data.get("session"),
                resend_below=data.get("resend_below"),
            )
        # A calibre-web token: keep the store part it carried.
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        return cls(store_token=inner.get("raw_kobo_store_token") or "")

    def serialize(self) -> str:
        payload = {"neneko": 1, "epoch": self.epoch, "gen": self.gen, "store": self.store_token}
        if self.session is not None:
            payload["session"] = self.session
        if self.resend_below is not None:
            payload["resend_below"] = self.resend_below
        return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
