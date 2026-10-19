"""SQLite storage that mirrors Notion's record tables."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from notion_backup.client import Record, RecordMap, iter_records

PAGE_TYPES = ("page", "collection_view_page")
# Blocks that own a crawlable subtree of their own (pages and databases).
CONTAINER_TYPES = (*PAGE_TYPES, "collection_view")


def collection_id_of(value: dict[str, Any]) -> str | None:
    return value.get("collection_id") or ((value.get("format") or {}).get("collection_pointer") or {}).get("id")


Source = str | Callable[[dict[str, Any]], Any]
# table -> [(column, source key or extractor, kind)]; kind: text | int | json | bool
COLUMNS: dict[str, list[tuple[str, Source, str]]] = {
    "block": [
        ("type", "type", "text"),
        ("parent_id", "parent_id", "text"),
        ("parent_table", "parent_table", "text"),
        ("properties", "properties", "json"),
        ("content", "content", "json"),
        ("format", "format", "json"),
        ("view_ids", "view_ids", "json"),
        ("collection_id", collection_id_of, "text"),
        ("discussions", "discussions", "json"),
        ("file_ids", "file_ids", "json"),
        ("created_time", "created_time", "int"),
        ("last_edited_time", "last_edited_time", "int"),
        ("created_by_id", "created_by_id", "text"),
        ("last_edited_by_id", "last_edited_by_id", "text"),
    ],
    "collection": [
        ("parent_id", "parent_id", "text"),
        ("parent_table", "parent_table", "text"),
        ("name", "name", "json"),
        ("schema", "schema", "json"),
        ("format", "format", "json"),
        ("template_pages", "template_pages", "json"),
    ],
    "collection_view": [
        ("type", "type", "text"),
        ("name", "name", "text"),
        ("parent_id", "parent_id", "text"),
        ("parent_table", "parent_table", "text"),
        ("query2", "query2", "json"),
        ("format", "format", "json"),
    ],
    "space": [("name", "name", "text"), ("icon", "icon", "text"), ("pages", "pages", "json")],
    "space_view": [
        ("parent_id", "parent_id", "text"),
        ("bookmarked_pages", "bookmarked_pages", "json"),
        ("private_pages", "private_pages", "json"),
        ("joined_teams", "joined_teams", "json"),
    ],
    "team": [("name", "name", "text"), ("pages", "pages", "json")],
    "notion_user": [("name", "name", "text"), ("email", "email", "text"), ("profile_photo", "profile_photo", "text")],
    "discussion": [
        ("parent_id", "parent_id", "text"),
        ("parent_table", "parent_table", "text"),
        ("resolved", "resolved", "bool"),
        ("context", "context", "json"),
        ("comments", "comments", "json"),
    ],
    "comment": [
        ("parent_id", "parent_id", "text"),
        ("parent_table", "parent_table", "text"),
        ("text", "text", "json"),
        ("created_time", "created_time", "int"),
        ("last_edited_time", "last_edited_time", "int"),
        ("created_by_id", "created_by_id", "text"),
    ],
}
COMMON = ("id", "space_id", "version", "alive", "role", "raw", "synced_at")


def _convert(raw: Any, kind: str) -> Any:
    if raw is None:
        return None
    if kind == "json":
        return json.dumps(raw, ensure_ascii=False)
    if kind == "bool":
        return int(bool(raw))
    if kind == "int":
        return int(raw) if isinstance(raw, (int, float)) else None
    return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)


def _upsert_sql(table: str) -> str:
    cols = [*COMMON, *(c for c, _, _ in COLUMNS[table])]
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
    return (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates} "
        f"WHERE excluded.version >= COALESCE({table}.version, -1)"
    )


_GENERIC_SQL = (
    "INSERT INTO record (table_name, id, space_id, version, alive, role, raw, synced_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(table_name, id) DO UPDATE SET "
    "space_id = excluded.space_id, version = excluded.version, alive = excluded.alive, "
    "role = excluded.role, raw = excluded.raw, synced_at = excluded.synced_at "
    "WHERE excluded.version >= COALESCE(record.version, -1)"
)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(resources.files("notion_backup").joinpath("schema.sql").read_text("utf-8"))
        self._sql = {t: _upsert_sql(t) for t in COLUMNS}

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # ---- records ------------------------------------------------------------

    def upsert_record(self, rec: Record, now: int | None = None) -> None:
        now = now or int(time.time())
        v = rec.value
        common = (
            rec.id,
            rec.space_id or v.get("space_id"),
            int(v.get("version") or 0),
            int(v.get("alive", True) is not False),
            rec.role,
            json.dumps(v, ensure_ascii=False),
            now,
        )
        spec = COLUMNS.get(rec.table)
        if spec is None:
            self.conn.execute(_GENERIC_SQL, (rec.table, *common))
            return
        extra = [_convert(src(v) if callable(src) else v.get(src), kind) for _, src, kind in spec]
        self.conn.execute(self._sql[rec.table], (*common, *extra))

    def upsert_record_map(self, record_map: RecordMap) -> int:
        now = int(time.time())
        n = 0
        for rec in iter_records(record_map):
            self.upsert_record(rec, now)
            n += 1
        return n

    def get_value(self, table: str, id: str) -> dict[str, Any] | None:
        row = self.conn.execute(f"SELECT raw FROM {table} WHERE id = ?", (id,)).fetchone()
        return json.loads(row["raw"]) if row else None

    def set_page_ids(self, page_id: str, block_ids: Iterable[str]) -> None:
        self.conn.execute(
            "UPDATE block SET page_id = ? WHERE id IN (SELECT value FROM json_each(?))",
            (page_id, json.dumps(list(block_ids))),
        )

    def soft_delete_missing(self, page_id: str, keep_ids: Iterable[str]) -> int:
        """Blocks that used to be in this page's tree but no longer are.

        Sub-pages are left alone: one that disappeared from here may have moved to the
        sidebar root; deleted pages are detected from their own record instead.
        """
        cur = self.conn.execute(
            f"UPDATE block SET alive = 0 WHERE page_id = ? AND alive = 1 "
            f"AND type NOT IN ({', '.join('?' * len(PAGE_TYPES))}) "
            f"AND id NOT IN (SELECT value FROM json_each(?))",
            (page_id, *PAGE_TYPES, json.dumps(list(keep_ids))),
        )
        return cur.rowcount

    def mark_page_dead(self, page_id: str) -> int:
        cur = self.conn.execute(
            "UPDATE block SET alive = 0 WHERE (id = ? OR page_id = ?) AND alive = 1", (page_id, page_id)
        )
        return cur.rowcount

    def known_containers(self, page_id: str) -> list[sqlite3.Row]:
        """Sub-pages and inline databases in a page's tree, from the last time it was loaded."""
        return self.conn.execute(
            f"SELECT id, space_id, type, raw FROM block WHERE page_id = ? AND alive = 1 "
            f"AND type IN ({', '.join('?' * len(CONTAINER_TYPES))})",
            (page_id, *CONTAINER_TYPES),
        ).fetchall()

    # ---- page sync state ----------------------------------------------------

    def page_states(self, page_ids: Iterable[str]) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM page_sync_state WHERE page_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(page_ids)),),
        ).fetchall()
        return {r["page_id"]: r for r in rows}

    def set_page_state(self, page_id: str, space_id: str | None, value: dict[str, Any], run_id: int) -> None:
        self.conn.execute(
            "INSERT INTO page_sync_state (page_id, space_id, version, last_edited_time, synced_at, last_seen_run, "
            "unreachable) VALUES (?, ?, ?, ?, ?, ?, 0) ON CONFLICT(page_id) DO UPDATE SET "
            "space_id = excluded.space_id, version = excluded.version, last_edited_time = excluded.last_edited_time, "
            "synced_at = excluded.synced_at, last_seen_run = excluded.last_seen_run, unreachable = 0",
            (page_id, space_id, value.get("version"), value.get("last_edited_time"), int(time.time()), run_id),
        )

    def touch_page_state(self, page_ids: Iterable[str], run_id: int) -> None:
        self.conn.execute(
            "UPDATE page_sync_state SET last_seen_run = ?, unreachable = 0 "
            "WHERE page_id IN (SELECT value FROM json_each(?))",
            (run_id, json.dumps(list(page_ids))),
        )

    def unseen_pages(self, run_id: int) -> list[sqlite3.Row]:
        """Backed-up, still-alive pages that this run did not reach."""
        return self.conn.execute(
            "SELECT s.page_id, s.space_id FROM page_sync_state s JOIN block b ON b.id = s.page_id "
            "WHERE b.alive = 1 AND COALESCE(s.last_seen_run, -1) != ?",
            (run_id,),
        ).fetchall()

    def mark_unreachable(self, page_id: str) -> None:
        self.conn.execute("UPDATE page_sync_state SET unreachable = 1 WHERE page_id = ?", (page_id,))

    # ---- files --------------------------------------------------------------

    def add_file_ref(
        self, url_key: str, source_url: str, block_id: str, space_id: str | None, filename: str | None, hosted: bool
    ) -> None:
        self.conn.execute(
            "INSERT INTO file_ref (url_key, source_url, block_id, space_id, filename, hosted, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(url_key) DO NOTHING",
            (url_key, source_url, block_id, space_id, filename, int(hosted), "pending" if hosted else "external"),
        )

    def files_to_download(self, include_external: bool, max_attempts: int = 3) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM file_ref WHERE status IN ('pending', 'failed', 'external') AND attempts < ? "
            "AND (hosted = 1 OR ?)",
            (max_attempts, int(include_external)),
        ).fetchall()

    def reset_failed_files(self) -> int:
        with self.transaction():
            cur = self.conn.execute("UPDATE file_ref SET attempts = 0 WHERE status = 'failed'")
        return cur.rowcount

    def save_file(self, url_key: str, data: bytes, mime_type: str | None) -> str:
        digest = hashlib.sha256(data).hexdigest()
        with self.transaction():
            self.conn.execute(
                "INSERT INTO file_blob (sha256, size, mime_type, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(sha256) DO NOTHING",
                (digest, len(data), mime_type, data),
            )
            self.conn.execute(
                "UPDATE file_ref SET status = 'ok', sha256 = ?, error = NULL, attempts = attempts + 1, "
                "downloaded_at = ? WHERE url_key = ?",
                (digest, int(time.time()), url_key),
            )
        return digest

    def file_failed(self, url_key: str, status: str, error: str) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE file_ref SET status = ?, error = ?, attempts = attempts + 1 WHERE url_key = ?",
                (status, error[:1000], url_key),
            )

    def find_files(self, key: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.url_key, r.filename, r.block_id, b.mime_type, b.data FROM file_ref r "
            "JOIN file_blob b ON b.sha256 = r.sha256 WHERE r.url_key = ? OR r.block_id = ?",
            (key, key),
        ).fetchall()

    # ---- runs ---------------------------------------------------------------

    def start_run(self, mode: str) -> int:
        with self.transaction():
            cur = self.conn.execute("INSERT INTO sync_run (started_at, mode) VALUES (?, ?)", (int(time.time()), mode))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict[str, Any], error: str | None = None) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE sync_run SET finished_at = ?, stats = ?, error = ? WHERE id = ?",
                (int(time.time()), json.dumps(stats), error, run_id),
            )
