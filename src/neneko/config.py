"""Settings, read from NENEKO_* environment variables."""

import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CollectionKind = Literal["all", "folder", "feed"]


def _split_list(value):
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NENEKO_", extra="ignore")

    # Nextcloud
    nc_url: str
    nc_user: str
    nc_app_password: str = ""
    nc_app_password_file: Path | None = None
    nc_folders: Annotated[list[str], NoDecode] = []

    # Kobo
    public_url: str
    kobo_token: str = ""
    kobo_token_file: Path | None = None
    store_proxy: bool = True
    store_url: str = "https://storeapi.kobo.com"
    image_cdn_url: str = "https://cdn.kobo.com/book-images"

    # What ends up on the Kobo
    max_items: int = Field(50, ge=1, le=500)
    # The Kobo downloads at most MaxDownload (default 5) books per sync page; see docs/kobo-notes.md.
    new_per_sync_page: int = Field(5, ge=1, le=100)
    collections: Annotated[list[CollectionKind], NoDecode] = ["all", "folder"]
    collection_all_name: str = "Neneko"
    collection_folder_template: str = "Neneko · {folder}"
    collection_feed_template: str = "{feed}"

    # Read state
    mark_read_on: Literal["finished", "opened", "delivered"] = "opened"
    finished_grace_hours: float = Field(24, ge=0)
    delete_action: Literal["mark_read", "forget"] = "mark_read"
    recovery_days: int = Field(14, ge=0)

    # Article building
    build_workers: int = Field(4, ge=1, le=16)
    fulltext_never_extract: Annotated[list[str], NoDecode] = []
    cookies_file: Path | None = None
    user_agent: str = ""
    language: str = "en"
    image_grayscale: bool = True
    image_max_width: int = Field(1264, ge=100)
    image_dither: Literal["ordered", "off"] = "off"
    cover_dither: Literal["ordered", "off"] = "ordered"
    max_images: int = Field(20, ge=0)
    kepubify_path: str = "kepubify"

    # Runtime
    data_dir: Path = Path("/data")
    host: str = "0.0.0.0"
    port: int = 8089
    log_level: str = "INFO"

    @field_validator("nc_folders", "fulltext_never_extract", "collections", mode="before")
    @classmethod
    def _lists(cls, value):
        return _split_list(value)

    @field_validator("nc_url", "public_url", "store_url", "image_cdn_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _secrets(self):
        if self.nc_app_password_file:
            self.nc_app_password = self.nc_app_password_file.read_text().strip()
        if self.kobo_token_file:
            self.kobo_token = self.kobo_token_file.read_text().strip()
        if not self.nc_app_password:
            raise ValueError("NENEKO_NC_APP_PASSWORD or NENEKO_NC_APP_PASSWORD_FILE is required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", self.kobo_token):
            raise ValueError(
                "NENEKO_KOBO_TOKEN (or _FILE) must be at least 16 characters of A-Z, a-z, 0-9, '-' or '_'"
            )
        return self

    @property
    def kobo_base_url(self) -> str:
        return f"{self.public_url}/kobo/{self.kobo_token}"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "neneko.sqlite"
