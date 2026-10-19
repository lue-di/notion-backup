from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    token_v2: str = field(repr=False)
    user_id: str | None
    file_token: str | None = field(repr=False)
    db_path: Path


def load_db_path(override: str | None = None) -> Path:
    load_dotenv()
    return Path(override or os.environ.get("NOTION_DB_PATH") or "notion_backup.db")


def load_config(db_override: str | None = None) -> Config:
    load_dotenv()
    token = os.environ.get("NOTION_TOKEN_V2", "").strip()
    if not token:
        raise ConfigError(
            "缺少 NOTION_TOKEN_V2：请在 .env 中填入浏览器 notion.so 的 token_v2 cookie（参考 .env.example）"
        )
    user_id = os.environ.get("NOTION_USER_ID", "").strip() or None
    file_token = os.environ.get("NOTION_FILE_TOKEN", "").strip() or None
    return Config(token_v2=token, user_id=user_id, file_token=file_token, db_path=load_db_path(db_override))
