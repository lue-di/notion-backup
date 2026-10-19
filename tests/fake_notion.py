"""In-memory stand-in for NotionClient, serving a small workspace."""

from __future__ import annotations

import copy
from typing import Any

from notion_backup.client import NotionClient, NotionError

SPACE = "00000000-0000-0000-0000-00000000000s"
USER = "00000000-0000-0000-0000-00000000000u"


def wrap(value: dict[str, Any]) -> dict[str, Any]:
    """New-style recordMap entry."""
    return {"spaceId": value.get("space_id"), "value": {"value": copy.deepcopy(value), "role": "editor"}}


class FakeNotion:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, dict[str, Any]]] = {}
        self.loaded: list[str] = []
        self.chunk_block_limit: int | None = None  # simulate loadPageChunk leaving blocks out
        self.files: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.queried_collections: list[str] = []
        self.fail_once: set[str] = set()  # page ids whose first loadPageChunk is rate limited

    # ---- building the workspace --------------------------------------------

    def put(self, table: str, value: dict[str, Any]) -> dict[str, Any]:
        value.setdefault("version", 1)
        value.setdefault("alive", True)
        value.setdefault("space_id", SPACE)
        self.records.setdefault(table, {})[value["id"]] = value
        return value

    def block(self, id: str, type: str, parent: str, title: str = "", **extra) -> dict[str, Any]:
        value = {
            "id": id,
            "type": type,
            "parent_id": parent,
            "parent_table": extra.pop("parent_table", "block"),
            "properties": {"title": [[title]]} if title else {},
            "created_time": 1,
            "last_edited_time": 1,
            **extra,
        }
        self.put("block", value)
        parent_block = self.records["block"].get(parent)
        if parent_block is not None and value["parent_table"] == "block":
            parent_block.setdefault("content", []).append(id)
        return value

    def edit(self, id: str, **changes) -> None:
        b = self.records["block"][id]
        b.update(changes)
        b["version"] += 1
        b["last_edited_time"] += 1

    def touch_page(self, page_id: str) -> None:
        """Notion bumps the page's last_edited_time when content inside it changes."""
        self.records["block"][page_id]["last_edited_time"] += 1

    # ---- NotionClient API --------------------------------------------------

    def _rm(self, items: list[tuple[str, str]]) -> dict[str, Any]:
        rm: dict[str, Any] = {}
        for table, id in items:
            value = self.records.get(table, {}).get(id)
            rm.setdefault(table, {})[id] = wrap(value) if value else {"role": "none"}
        return rm

    async def get_spaces(self):
        items = [(t, i) for t in ("space", "space_view", "notion_user", "user_root") for i in self.records.get(t, {})]
        return {k: v for k, v in self._rm(items).items()}

    async def sync_record_values(self, pointers):
        return self._rm([(p["table"], p["id"]) for p in pointers])

    async def load_page_chunk(self, page_id, space_id=None):
        if page_id in self.fail_once:
            self.fail_once.discard(page_id)
            raise NotionError("loadPageChunk: HTTP 429", 429)
        self.loaded.append(page_id)
        blocks = self.records["block"]
        items = [("block", page_id)]
        stack = list(blocks[page_id].get("content") or [])
        while stack:
            bid = stack.pop(0)
            items.append(("block", bid))
            child = blocks[bid]
            if child["type"] not in ("page", "collection_view_page"):
                stack.extend(child.get("content") or [])
        if self.chunk_block_limit is not None:
            items = items[: self.chunk_block_limit]
        return self._rm(items)

    async def query_collection(self, collection_id, view_id, space_id, limit=10_000):
        self.queried_collections.append(collection_id)
        rows = [
            b["id"]
            for b in self.records["block"].values()
            if b.get("parent_table") == "collection" and b["parent_id"] == collection_id and b["alive"]
        ]
        return rows, self._rm([("block", r) for r in rows])

    async def search_blocks(self, space_id, limit=1000):
        return [], {}

    async def get_signed_file_urls(self, refs):
        return [f"https://signed.example/{url}" for url, _, _ in refs]

    user_id = None
    has_file_token = True
    signed_proxy_url = NotionClient.signed_proxy_url

    async def download(self, url, max_bytes):
        self.downloads.append(url)
        return self.files.get(url.removeprefix("https://signed.example/"), b"data:" + url.encode()), "image/png"


def sample_workspace() -> FakeNotion:
    """
    Home (page)
      ├─ "intro" text
      ├─ toggle ─ nested text (with a comment)
      ├─ image (Notion-hosted)
      ├─ Child (page) ─ "child text"
      └─ Tasks (inline database) ─ rows: Task 1, Task 2 ("row body")
    """
    n = FakeNotion()
    n.put("notion_user", {"id": USER, "name": "Me", "email": "me@example.com"})
    n.put("space", {"id": SPACE, "name": "My Space", "pages": ["home"]})
    n.put("space_view", {"id": "sv", "parent_id": USER, "private_pages": []})
    n.block("home", "page", SPACE, "Home", parent_table="space")
    n.block("intro", "text", "home", "intro")
    n.block("toggle", "toggle", "home", "toggle")
    n.block("nested", "text", "toggle", "nested", discussions=["disc"])
    n.put("discussion", {"id": "disc", "parent_id": "nested", "parent_table": "block", "comments": ["c1"]})
    n.put("comment", {"id": "c1", "parent_id": "disc", "parent_table": "discussion", "text": [["nice"]]})
    n.block(
        "img",
        "image",
        "home",
        properties={"source": [["attachment:abc-123:photo.png"]]},
        format={"display_source": "attachment:abc-123:photo.png"},
    )
    n.block("child", "page", "home", "Child")
    n.block("child_text", "text", "child", "child text")
    n.block("db", "collection_view", "home", collection_id="coll", view_ids=["view"])
    n.put(
        "collection",
        {
            "id": "coll",
            "parent_id": "db",
            "parent_table": "block",
            "name": [["Tasks"]],
            "schema": {"title": {"name": "Name", "type": "title"}, "st": {"name": "Status", "type": "select"}},
        },
    )
    n.put("collection_view", {"id": "view", "type": "table", "parent_id": "db", "parent_table": "block"})
    n.block("row1", "page", "coll", "Task 1", parent_table="collection")
    n.records["block"]["row1"]["properties"]["st"] = [["Done"]]
    n.block("row2", "page", "coll", "Task 2", parent_table="collection")
    n.block("row2_text", "text", "row2", "row body")
    return n
