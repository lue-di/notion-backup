"""Thin async wrapper around Notion's internal (undocumented) v3 API."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://www.notion.so/api/v3/"
SIGNED_BASE = "https://www.notion.so/signed/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
RECORD_BATCH = 100
SIGN_BATCH = 50

RecordMap = dict[str, dict[str, Any]]


class NotionError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NotionAuthError(NotionError):
    pass


class RateLimitExhausted(NotionError):
    """Notion kept answering 429 for so long that waiting inside this run is pointless."""


class FileTooLarge(Exception):
    pass


@dataclass
class Record:
    table: str
    id: str
    value: dict[str, Any]
    role: str | None
    space_id: str | None


def iter_records(record_map: RecordMap | None) -> Iterator[Record]:
    """Yield records from a recordMap, accepting both response shapes:

    old: {"block": {id: {"role": r, "value": {...}}}}
    new: {"block": {id: {"spaceId": s, "value": {"role": r, "value": {...}}}}}
    """
    if not record_map:
        return
    for table, entries in record_map.items():
        if not isinstance(entries, dict):
            continue
        for rid, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            value = entry.get("value")
            role = entry.get("role")
            space_id = entry.get("spaceId")
            if isinstance(value, dict) and "id" not in value and isinstance(value.get("value"), dict):
                role = value.get("role", role)
                value = value["value"]
            if not isinstance(value, dict):
                # No access, or the record does not exist.
                continue
            yield Record(table, value.get("id", rid), value, role, space_id or value.get("space_id"))


def merge_record_maps(dst: RecordMap, src: RecordMap | None) -> RecordMap:
    if src:
        for table, entries in src.items():
            if isinstance(entries, dict):
                dst.setdefault(table, {}).update(entries)
    return dst


def pointer(table: str, id: str, space_id: str | None = None) -> dict[str, str]:
    p = {"table": table, "id": id}
    if space_id:
        p["spaceId"] = space_id
    return p


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class RateLimiter:
    """Pacing shared by one group of requests (API calls, or file downloads).

    Request starts are spaced 1/rate seconds apart. A 429 pauses every request in the
    group and halves the rate; further 429s keep halving (at most once per request
    interval, so a burst of in-flight failures counts once) and double the pause.
    Each success nudges the rate back up towards the configured maximum.
    """

    def __init__(self, rate: float, name: str = "Notion", min_rate: float = 0.05):
        self.name = name
        self.max_rate = rate
        self.min_rate = min(min_rate, rate)
        self.rate = rate
        self._next = 0.0
        self._paused_until = 0.0
        self._last_cut = float("-inf")
        self._strikes = 0
        self._throttled_since: float | None = None
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            start = max(now, self._next, self._paused_until)
            self._next = start + 1.0 / self.rate
        if start > now:
            await asyncio.sleep(start - now)

    def throttled(self, retry_after: float | None = None) -> None:
        now = asyncio.get_running_loop().time()
        if self._throttled_since is None:
            self._throttled_since = now
        if now - self._last_cut >= 1.0 / self.rate:
            self._last_cut = now
            self._strikes += 1
            self.rate = max(self.min_rate, self.rate / 2)
            pause = max(retry_after or 0.0, min(60.0, 2.0 ** (self._strikes - 1)))
            self._paused_until = max(self._paused_until, now + pause)
            log.warning("%s 限流 (429)：暂停 %.0fs，速率降至 %.2f 次/秒", self.name, pause, self.rate)

    def blocked_for(self) -> float:
        """Seconds since the last success during which every request was rate limited."""
        if self._throttled_since is None:
            return 0.0
        return asyncio.get_running_loop().time() - self._throttled_since

    def succeeded(self) -> None:
        self._throttled_since = None
        self._strikes = 0
        self.rate = min(self.max_rate, self.rate * 1.05 + 0.01)


class NotionClient:
    def __init__(
        self,
        token_v2: str,
        user_id: str | None = None,
        file_token: str | None = None,
        *,
        concurrency: int = 4,
        rate: float = 3.0,
        file_rate: float = 1.0,
        file_block_timeout: float = 300.0,
        max_retries: int = 10,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        cookies = httpx.Cookies()
        cookies.set("token_v2", token_v2, domain=".notion.so")
        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        if user_id:
            cookies.set("notion_user_id", user_id, domain=".notion.so")
            headers["x-notion-active-user-header"] = user_id
        if file_token:
            # file.notion.so only serves workspace files to requests carrying this cookie.
            cookies.set("file_token", file_token, domain=".notion.so")
        self.user_id = user_id
        self._api = httpx.AsyncClient(
            base_url=API_BASE, cookies=cookies, headers=headers, timeout=timeout, transport=transport
        )
        # Downloads follow redirects to S3; cookies are scoped to .notion.so so they never leak there.
        self._dl = httpx.AsyncClient(
            cookies=cookies,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._api_limiter = RateLimiter(rate, "Notion API")
        # file.notion.so enforces its own, much stricter per-user limit.
        self._file_limiter = RateLimiter(file_rate, "附件下载")
        self._file_block_timeout = file_block_timeout
        self._max_retries = max_retries
        self._use_page_chunk_v2 = False

    @property
    def has_file_token(self) -> bool:
        return self._dl.cookies.get("file_token", domain=".notion.so") is not None

    async def __aenter__(self) -> NotionClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._api.aclose()
        await self._dl.aclose()

    async def _backoff(self, attempt: int, retry_after: str | None, limiter: RateLimiter | None = None) -> None:
        """Wait before a retry. For a 429 the shared limiter pauses the whole group instead."""
        try:
            seconds = float(retry_after) if retry_after else None
        except ValueError:
            seconds = None
        if limiter is not None:
            limiter.throttled(seconds)
            return
        await asyncio.sleep(max(seconds or 0.0, min(60.0, 2.0**attempt) + random.uniform(0, 1)))

    async def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            await self._api_limiter.wait()
            try:
                async with self._sem:
                    resp = await self._api.post(endpoint, json=payload)
            except httpx.TransportError as e:
                if attempt == self._max_retries:
                    raise NotionError(f"{endpoint}: network error {e!r}") from e
                log.warning("%s: network error %r, retrying", endpoint, e)
                await self._backoff(attempt, None)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self._max_retries:
                    raise NotionError(f"{endpoint}: HTTP {resp.status_code}", resp.status_code)
                if resp.status_code != 429:
                    log.warning("%s: HTTP %s, retrying", endpoint, resp.status_code)
                await self._backoff(
                    attempt, resp.headers.get("Retry-After"), self._api_limiter if resp.status_code == 429 else None
                )
                continue
            if resp.status_code == 401:
                raise NotionAuthError("token_v2 无效或已过期，请重新从浏览器获取", 401)
            if resp.status_code >= 400:
                raise NotionError(f"{endpoint}: HTTP {resp.status_code} {resp.text[:300]}", resp.status_code)
            self._api_limiter.succeeded()
            return resp.json()
        raise AssertionError("unreachable")

    # ---- account / workspaces -------------------------------------------------

    async def get_spaces(self) -> RecordMap:
        """All workspaces of the current user, merged into one recordMap."""
        record_map: RecordMap = {}
        try:
            data = await self.post("getSpaces", {})
        except NotionAuthError:
            raise
        except NotionError as e:
            log.warning("getSpaces failed (%s), falling back to loadUserContent", e)
            data = {}
        if "recordMap" in data:
            merge_record_maps(record_map, data["recordMap"])
        else:
            # {user_id: {"notion_user": {...}, "space": {...}, ...}}
            for per_user in data.values():
                if isinstance(per_user, dict):
                    merge_record_maps(record_map, per_user)
        if not record_map.get("space"):
            data = await self.post("loadUserContent", {})
            merge_record_maps(record_map, data.get("recordMap"))
        return record_map

    # ---- records ------------------------------------------------------------

    async def sync_record_values(self, pointers: Iterable[dict[str, str]]) -> RecordMap:
        pointers = list(pointers)
        record_map: RecordMap = {}

        async def one(batch: list[dict[str, str]]) -> None:
            data = await self.post(
                "syncRecordValues", {"requests": [{"pointer": p, "version": -1} for p in batch]}
            )
            merge_record_maps(record_map, data.get("recordMap"))

        await asyncio.gather(*(one(b) for b in _chunks(pointers, RECORD_BATCH)))
        return record_map

    async def load_page_chunk(self, page_id: str, space_id: str | None = None) -> RecordMap:
        """All blocks of one page (not descending into sub-pages), following the cursor."""
        record_map: RecordMap = {}
        cursor: dict[str, Any] = {"stack": []}
        for chunk_number in range(10_000):
            payload: dict[str, Any] = {
                "limit": 100,
                "cursor": cursor,
                "chunkNumber": chunk_number,
                "verticalColumns": False,
            }
            if self._use_page_chunk_v2:
                data = await self.post("loadCachedPageChunkV2", {**payload, "page": pointer("block", page_id, space_id)})
            else:
                try:
                    data = await self.post("loadPageChunk", {**payload, "pageId": page_id})
                except NotionAuthError:
                    raise
                except NotionError as e:
                    if e.status not in (400, 404, 410):
                        raise
                    log.warning("loadPageChunk unavailable (%s), switching to loadCachedPageChunkV2", e)
                    self._use_page_chunk_v2 = True
                    continue
            merge_record_maps(record_map, data.get("recordMap"))
            cursor = data.get("cursor") or {"stack": []}
            if not cursor.get("stack"):
                break
        return record_map

    async def query_collection(
        self, collection_id: str, view_id: str, space_id: str | None, limit: int = 10_000
    ) -> tuple[list[str], RecordMap]:
        """All row ids of a database. The empty filter overrides the view's own filters."""
        space = {"spaceId": space_id} if space_id else {}
        while True:
            payload = {
                "source": {"type": "collection", "id": collection_id, **space},
                "collectionView": {"id": view_id, **space},
                "loader": {
                    "type": "reducer",
                    "reducers": {"collection_group_results": {"type": "results", "limit": limit}},
                    "filter": {"operator": "and", "filters": []},
                    "sort": [],
                    "searchQuery": "",
                    "userTimeZone": "UTC",
                },
            }
            data = await self.post("queryCollection", payload)
            result = (data.get("result") or {}).get("reducerResults", {}).get("collection_group_results", {})
            ids = list(result.get("blockIds") or [])
            if result.get("hasMore") and limit < 1_000_000:
                log.warning("collection %s has more than %d rows, retrying with larger limit", collection_id, limit)
                limit *= 4
                continue
            return ids, data.get("recordMap") or {}

    async def search_blocks(self, space_id: str, limit: int = 1000) -> tuple[list[str], RecordMap]:
        payload = {
            "type": "BlocksInSpace",
            "query": "",
            "spaceId": space_id,
            "limit": limit,
            "filters": {
                "isDeletedOnly": False,
                "excludeTemplates": False,
                "navigableBlockContentOnly": True,
                "requireEditPermissions": False,
                "includePublicPagesWithoutExplicitAccess": False,
                "ancestors": [],
                "createdBy": [],
                "editedBy": [],
                "lastEditedTime": {},
                "createdTime": {},
                "inTeams": [],
            },
            "sort": {"field": "lastEdited", "direction": "desc"},
            "source": "quick_find",
        }
        data = await self.post("search", payload)
        ids = [r["id"] for r in data.get("results") or [] if isinstance(r, dict) and r.get("id")]
        return ids, data.get("recordMap") or {}

    # ---- files --------------------------------------------------------------

    async def get_signed_file_urls(self, refs: list[tuple[str, str, str | None]]) -> list[str | None]:
        """refs: (url, block_id, space_id) -> signed download URL (None when Notion refuses)."""
        out: list[str | None] = []
        for batch in _chunks(refs, SIGN_BATCH):
            out += await self._sign(batch)
        return out

    async def _sign(self, batch: list[tuple[str, str, str | None]]) -> list[str | None]:
        urls = [{"url": url, "permissionRecord": pointer("block", block_id, space_id)} for url, block_id, space_id in batch]
        try:
            data = await self.post("getSignedFileUrls", {"urls": urls})
        except NotionAuthError:
            raise
        except NotionError as e:
            # One invalid URL fails the whole request; split to isolate it.
            if e.status == 400 and len(batch) > 1:
                mid = len(batch) // 2
                return await self._sign(batch[:mid]) + await self._sign(batch[mid:])
            log.warning("getSignedFileUrls refused %s: %s", batch[0][0] if len(batch) == 1 else f"{len(batch)} urls", e)
            return [None] * len(batch)
        signed = list(data.get("signedUrls") or [])[: len(batch)]
        return [s or None for s in signed] + [None] * (len(batch) - len(signed))

    def signed_proxy_url(self, url: str, block_id: str, space_id: str | None) -> str:
        """Fallback: notion.so/signed/... redirects to the file when called with our cookies."""
        params = {"table": "block", "id": block_id, "spaceId": space_id, "userId": self.user_id, "cache": "v2"}
        return SIGNED_BASE + quote(url, safe="") + "?" + "&".join(f"{k}={v}" for k, v in params.items() if v)

    async def download(self, url: str, max_bytes: int) -> tuple[bytes, str | None]:
        """GET a file into memory. Raises FileTooLarge, httpx errors, or NotionError."""
        paced = httpx.URL(url).host.endswith("notion.so")
        for attempt in range(self._max_retries + 1):
            retry_after: str | None = None
            if paced:
                await self._file_limiter.wait()
            try:
                async with self._sem, self._dl.stream("GET", url) as resp:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        retry_after = resp.headers.get("Retry-After")
                        error = NotionError(f"download: HTTP {resp.status_code}", resp.status_code)
                    else:
                        resp.raise_for_status()
                        if int(resp.headers.get("Content-Length") or 0) > max_bytes:
                            raise FileTooLarge(resp.headers["Content-Length"])
                        buf = bytearray()
                        async for part in resp.aiter_bytes():
                            buf += part
                            if len(buf) > max_bytes:
                                raise FileTooLarge(len(buf))
                        if paced:
                            self._file_limiter.succeeded()
                        ctype = resp.headers.get("Content-Type")
                        return bytes(buf), ctype.split(";")[0].strip() if ctype else None
            except httpx.TransportError as e:
                error = e
            if attempt == self._max_retries:
                raise error
            limited = paced and getattr(error, "status", None) == 429
            if limited and self._file_limiter.blocked_for() > self._file_block_timeout:
                raise RateLimitExhausted("file.notion.so 持续限流，本次下载额度已用完", 429)
            await self._backoff(attempt, retry_after, self._file_limiter if limited else None)
        raise AssertionError("unreachable")
