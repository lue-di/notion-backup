"""Walk every reachable page of the account and mirror it into the Store."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from notion_backup import files
from notion_backup.client import NotionAuthError, NotionClient, NotionError, RecordMap, iter_records, merge_record_maps, pointer
from notion_backup.db import CONTAINER_TYPES, PAGE_TYPES, Store, collection_id_of

log = logging.getLogger(__name__)

WAVE_SIZE = 50
MAX_CLOSURE_ROUNDS = 20


@dataclass
class SyncOptions:
    full: bool = False
    space_ids: set[str] = field(default_factory=set)
    download_files: bool = True
    external_files: bool = False
    max_file_bytes: int = 200 * 1024 * 1024


@dataclass
class PageTree:
    block_ids: list[str]  # every block in the page's content tree, in document order
    containers: list[dict[str, Any]]  # sub-pages and inline databases within the tree
    missing: set[str]


def walk_page(page_id: str, blocks: dict[str, dict[str, Any]]) -> PageTree:
    """Depth-first over `content`, stopping at sub-pages (they are crawled on their own)."""
    tree = PageTree([], [], set())
    page = blocks.get(page_id) or {}
    stack = list(reversed(page.get("content") or []))
    seen = {page_id}
    while stack:
        bid = stack.pop()
        if bid in seen:
            continue
        seen.add(bid)
        block = blocks.get(bid)
        if block is None:
            tree.missing.add(bid)
            continue
        tree.block_ids.append(bid)
        if block.get("type") in CONTAINER_TYPES:
            tree.containers.append(block)
        if block.get("type") not in PAGE_TYPES:
            stack.extend(reversed(block.get("content") or []))
    return tree


class Crawler:
    def __init__(
        self,
        client: NotionClient,
        store: Store,
        options: SyncOptions | None = None,
        on_progress: Callable[[Counter, str], None] | None = None,
    ):
        self.client = client
        self.store = store
        self.opts = options or SyncOptions()
        self.on_progress = on_progress
        self.stats: Counter = Counter()
        self.run_id = 0
        # Latest values of page-like blocks seen during this run; saves a round trip per page.
        self._fresh: dict[str, dict[str, Any]] = {}
        self._queue: deque[tuple[str, str | None]] = deque()
        self._queued: set[str] = set()
        self._collection_queue: list[tuple[dict[str, Any], str | None]] = []
        self._collections_done: set[str] = set()
        # Failures (usually rate limiting) get one more try at the end of the run.
        self._failed_pages: list[tuple[str, str | None]] = []
        self._failed_collections: list[tuple[dict[str, Any], str | None]] = []
        self._retry_round = False

    # ---- entry point --------------------------------------------------------

    async def run(self) -> Counter:
        self.run_id = self.store.start_run("full" if self.opts.full else "incremental")
        try:
            await self._discover()
            await self._drain()
            if self._failed_pages or self._failed_collections:
                self._retry_round = True
                self._progress(f"重试 {len(self._failed_pages) + len(self._failed_collections)} 个失败项")
                self._queue.extend(self._failed_pages)
                self._collection_queue.extend(self._failed_collections)
                await self._drain()
            if not self.opts.space_ids:
                await self._reconcile()
            if self.opts.download_files:
                self._progress("下载附件")
                self.stats.update(
                    await files.download_pending(
                        self.client,
                        self.store,
                        include_external=self.opts.external_files,
                        max_bytes=self.opts.max_file_bytes,
                        on_progress=lambda name: self._progress(f"附件 {name}"),
                    )
                )
        except BaseException as e:
            self.store.finish_run(self.run_id, dict(self.stats), error=repr(e))
            raise
        self.store.finish_run(self.run_id, dict(self.stats))
        return self.stats

    def _progress(self, msg: str) -> None:
        if self.on_progress:
            self.on_progress(self.stats, msg)

    # ---- bookkeeping --------------------------------------------------------

    def _failed(self, kind: str, id: str | None, error: Exception) -> None:
        if self._retry_round:
            log.error("%s %s failed: %s", kind, id, error)
            self.stats["errors"] += 1
        else:
            log.warning("%s %s failed, will retry at the end of this run: %s", kind, id, error)

    def _remember(self, record_map: RecordMap) -> None:
        for rec in iter_records(record_map):
            if rec.table == "block" and rec.value.get("type") in CONTAINER_TYPES:
                self._fresh[rec.id] = rec.value

    def _ingest(self, record_map: RecordMap) -> None:
        with self.store.transaction():
            self.stats["records_written"] += self.store.upsert_record_map(record_map)
        self._remember(record_map)

    def _enqueue_page(self, page_id: str, space_id: str | None) -> None:
        if page_id and page_id not in self._queued:
            self._queued.add(page_id)
            self._queue.append((page_id, space_id))

    def _enqueue_container(self, block: dict[str, Any], space_id: str | None) -> None:
        space_id = block.get("space_id") or space_id
        if block.get("type") == "collection_view":
            self._enqueue_collection(block, space_id)
        else:
            self._enqueue_page(block["id"], space_id)

    def _enqueue_collection(self, block: dict[str, Any], space_id: str | None) -> None:
        cid = collection_id_of(block)
        if cid and cid not in self._collections_done:
            self._collections_done.add(cid)
            self._collection_queue.append((block, space_id))

    # ---- discovery ----------------------------------------------------------

    async def _discover(self) -> None:
        self._progress("读取工作区")
        rm = await self.client.get_spaces()
        self._ingest(rm)
        spaces = {r.id: r.value for r in iter_records(rm) if r.table == "space"}
        if self.opts.space_ids:
            unknown = self.opts.space_ids - spaces.keys()
            if unknown:
                raise NotionError(f"找不到工作区: {', '.join(sorted(unknown))}")
            spaces = {k: v for k, v in spaces.items() if k in self.opts.space_ids}
        self.stats["spaces"] = len(spaces)

        # Sidebars live in space_view records; fetch any that getSpaces did not include.
        view_ptrs = [
            p
            for r in iter_records(rm)
            if r.table == "user_root"
            for p in r.value.get("space_view_pointers") or []
            if isinstance(p, dict) and p.get("id")
        ]
        known_views = {r.id for r in iter_records(rm) if r.table == "space_view"}
        missing_views = [pointer("space_view", p["id"], p.get("spaceId")) for p in view_ptrs if p["id"] not in known_views]
        if missing_views:
            extra = await self.client.sync_record_values(missing_views)
            self._ingest(extra)
            merge_record_maps(rm, extra)

        team_ids: dict[str, str] = {r.id: r.value.get("space_id") for r in iter_records(rm) if r.table == "team"}
        for space_id, space in spaces.items():
            for pid in space.get("pages") or []:
                self._enqueue_page(pid, space_id)
        for r in iter_records(rm):
            if r.table != "space_view" or r.value.get("space_id") not in spaces:
                continue
            sid = r.value["space_id"]
            for key in ("private_pages", "bookmarked_pages"):
                for pid in r.value.get(key) or []:
                    self._enqueue_page(pid, sid)
            for tid in r.value.get("joined_teams") or []:
                team_ids.setdefault(tid, sid)

        # Teamspaces: each team record lists its top-level pages.
        team_ids = {t: s for t, s in team_ids.items() if s in spaces}
        if team_ids:
            teams = await self.client.sync_record_values(pointer("team", t, s) for t, s in team_ids.items())
            self._ingest(teams)
            merge_record_maps(rm, teams)
        for r in iter_records(rm):
            if r.table == "team" and r.id in team_ids:
                for pid in r.value.get("pages") or []:
                    self._enqueue_page(pid, team_ids[r.id])

        # Safety net for pages not linked from any sidebar (e.g. pages shared with you).
        for space_id in spaces:
            try:
                ids, search_rm = await self.client.search_blocks(space_id)
            except NotionAuthError:
                raise
            except NotionError as e:
                log.warning("search in space %s failed: %s", space_id, e)
                continue
            self._remember(search_rm)
            for pid in ids:
                self._enqueue_page(pid, space_id)
        self.stats["roots"] = len(self._queue)

    # ---- main loop ----------------------------------------------------------

    async def _drain(self) -> None:
        while self._queue or self._collection_queue:
            if self._queue:
                wave = [self._queue.popleft() for _ in range(min(WAVE_SIZE, len(self._queue)))]
                await self._process_wave(wave)
            if self._collection_queue:
                batch, self._collection_queue = self._collection_queue, []
                await asyncio.gather(*(self._process_collection(b, s) for b, s in batch))

    async def _process_wave(self, wave: list[tuple[str, str | None]]) -> None:
        need = [pointer("block", pid, sid) for pid, sid in wave if pid not in self._fresh]
        if need:
            rm = await self.client.sync_record_values(need)
            with self.store.transaction():
                self.store.upsert_record_map(rm)
            for rec in iter_records(rm):
                if rec.table == "block":
                    self._fresh[rec.id] = rec.value
        states = self.store.page_states(pid for pid, _ in wave)

        changed: list[tuple[str, str | None, dict[str, Any]]] = []
        touched: list[str] = []
        with self.store.transaction():
            for pid, sid in wave:
                value = self._fresh.get(pid)
                if value is None or value.get("alive") is False:
                    if pid in states:
                        self.stats["blocks_deleted"] += self.store.mark_page_dead(pid)
                        self.stats["pages_deleted"] += 1
                        touched.append(pid)
                    continue
                sid = value.get("space_id") or sid
                btype = value.get("type")
                if btype == "collection_view":
                    self._enqueue_collection(value, sid)
                    continue
                if btype not in PAGE_TYPES:
                    continue
                if btype == "collection_view_page":
                    self._enqueue_collection(value, sid)
                state = states.get(pid)
                if (
                    self.opts.full
                    or state is None
                    or state["version"] != value.get("version")
                    or state["last_edited_time"] != value.get("last_edited_time")
                ):
                    changed.append((pid, sid, value))
                    continue
                # Unchanged: reuse the sub-pages/databases recorded last time.
                self.stats["pages_unchanged"] += 1
                touched.append(pid)
                for row in self.store.known_containers(pid):
                    self._enqueue_container(json.loads(row["raw"]), row["space_id"] or sid)
            self.store.touch_page_state(touched, self.run_id)

        await asyncio.gather(*(self._load_page(pid, sid, value) for pid, sid, value in changed))
        self._progress(f"队列剩余 {len(self._queue)}")

    async def _load_page(self, page_id: str, space_id: str | None, page_value: dict[str, Any]) -> None:
        try:
            rm = await self.client.load_page_chunk(page_id, space_id)
            blocks = {r.id: r.value for r in iter_records(rm) if r.table == "block"}
            blocks.setdefault(page_id, page_value)
            if page_id not in rm.get("block", {}):
                merge_record_maps(rm, {"block": {page_id: {"value": page_value}}})

            # Large pages: loadPageChunk may leave nested blocks out; fetch until the tree is closed.
            tree = walk_page(page_id, blocks)
            for _ in range(MAX_CLOSURE_ROUNDS):
                if not tree.missing:
                    break
                extra = await self.client.sync_record_values(pointer("block", b, space_id) for b in tree.missing)
                got = {r.id: r.value for r in iter_records(extra) if r.table == "block"}
                if not got:
                    log.warning("page %s: %d blocks are not accessible", page_id, len(tree.missing))
                    break
                merge_record_maps(rm, extra)
                blocks.update(got)
                tree = walk_page(page_id, blocks)

            await self._fetch_related(rm, [blocks[page_id], *(blocks[b] for b in tree.block_ids)], space_id)
        except NotionAuthError:
            raise
        except NotionError as e:
            self._failed("page", page_id, e)
            self._failed_pages.append((page_id, space_id))
            return

        page_value = blocks[page_id]
        with self.store.transaction():
            self.stats["records_written"] += self.store.upsert_record_map(rm)
            self.store.set_page_ids(page_id, tree.block_ids)
            self.stats["blocks_deleted"] += self.store.soft_delete_missing(page_id, tree.block_ids)
            for bid in (page_id, *tree.block_ids):
                urls = files.block_file_urls(blocks[bid])
                self.stats["files_registered"] += files.register_files(self.store, urls, bid, space_id)
            for rec in iter_records(rm):
                if rec.table == "comment":
                    urls = files.comment_file_urls(rec.value)
                    self.stats["files_registered"] += files.register_files(
                        self.store, urls, rec.value.get("parent_id") or page_id, space_id
                    )
            self.store.set_page_state(page_id, space_id, page_value, self.run_id)
        self._remember(rm)
        self.stats["pages_loaded"] += 1
        self.stats["blocks"] += len(tree.block_ids) + 1

        for block in tree.containers:
            self._enqueue_container(block, space_id)
        if page_value.get("type") == "collection_view_page":
            self._enqueue_collection(page_value, space_id)

    async def _fetch_related(self, rm: RecordMap, blocks: list[dict[str, Any]], space_id: str | None) -> None:
        """Make sure databases, views, discussions and comments referenced by the page are in `rm`."""
        have = lambda table, id: id in rm.get(table, {})  # noqa: E731
        need: list[dict[str, str]] = []
        for b in blocks:
            if b.get("type") in ("collection_view", "collection_view_page"):
                cid = collection_id_of(b)
                if cid and not have("collection", cid):
                    need.append(pointer("collection", cid, space_id))
                need += [pointer("collection_view", v, space_id) for v in b.get("view_ids") or [] if not have("collection_view", v)]
            need += [pointer("discussion", d, space_id) for d in b.get("discussions") or [] if not have("discussion", d)]
        if need:
            merge_record_maps(rm, await self.client.sync_record_values(need))
        comments = [
            pointer("comment", c, space_id)
            for r in iter_records(rm)
            if r.table == "discussion"
            for c in r.value.get("comments") or []
            if not have("comment", c)
        ]
        if comments:
            merge_record_maps(rm, await self.client.sync_record_values(comments))

    async def _process_collection(self, block: dict[str, Any], space_id: str | None) -> None:
        cid = collection_id_of(block)
        view_ids = block.get("view_ids") or []
        try:
            # Re-fetch every run: row membership and schema changes do not bump the parent page.
            rm = await self.client.sync_record_values(
                [pointer("collection", cid, space_id), *(pointer("collection_view", v, space_id) for v in view_ids)]
            )
            if not view_ids:
                log.warning("database %s has no views; cannot list its rows", cid)
                row_ids: list[str] = []
            else:
                row_ids, rows_rm = await self.client.query_collection(cid, view_ids[0], space_id)
                merge_record_maps(rm, rows_rm)
        except NotionAuthError:
            raise
        except NotionError as e:
            self._failed("database", cid, e)
            self._failed_collections.append((block, space_id))
            return
        self._ingest(rm)
        self.stats["collections"] += 1
        collection = next((r.value for r in iter_records(rm) if r.table == "collection" and r.id == cid), {})
        for pid in [*row_ids, *(collection.get("template_pages") or [])]:
            self._enqueue_page(pid, space_id)

    # ---- deletions ----------------------------------------------------------

    async def _reconcile(self) -> None:
        """Pages backed up before but not reached this run: deleted, or just unreachable."""
        unseen = self.store.unseen_pages(self.run_id)
        if not unseen:
            return
        self._progress(f"核对 {len(unseen)} 个未访问到的页面")
        rm = await self.client.sync_record_values(pointer("block", r["page_id"], r["space_id"]) for r in unseen)
        current = {r.id: r.value for r in iter_records(rm) if r.table == "block"}
        with self.store.transaction():
            self.store.upsert_record_map(rm)
            for row in unseen:
                value = current.get(row["page_id"])
                if value is None or value.get("alive") is False:
                    self.stats["blocks_deleted"] += self.store.mark_page_dead(row["page_id"])
                    self.stats["pages_deleted"] += 1
                    self.store.touch_page_state([row["page_id"]], self.run_id)
                else:
                    # Still exists (e.g. under a page in the trash, or access was removed); keep the backup.
                    self.store.mark_unreachable(row["page_id"])
                    self.stats["pages_unreachable"] += 1
