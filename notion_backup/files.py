"""Find file references in records and download them into SQLite BLOBs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from notion_backup.client import FileTooLarge, NotionAuthError, NotionClient, NotionError, RateLimitExhausted
from notion_backup.db import Store

log = logging.getLogger(__name__)

MEDIA_TYPES = ("image", "file", "pdf", "video", "audio")
HOSTED_MARKERS = ("prod-files-secure", "secure.notion-static.com", "file.notion.so/f/")
DOWNLOAD_BATCH = 50


def is_url(s: Any) -> bool:
    return isinstance(s, str) and s.startswith(("http://", "https://", "attachment:"))


def is_hosted(url: str) -> bool:
    return url.startswith("attachment:") or any(m in url for m in HOSTED_MARKERS)


def unwrap(url: str) -> str:
    """Old pages store files as www.notion.so/image/<url-encoded original URL>."""
    parts = urlsplit(url)
    if parts.netloc.endswith("notion.so") and parts.path.startswith("/image/"):
        inner = unquote(parts.path[len("/image/") :])
        if is_url(inner):
            return inner
    return url


def url_key(url: str) -> str:
    """Stable identity of a file: signature query strings change on every request."""
    if url.startswith("attachment:"):
        return url
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def filename_of(url: str) -> str | None:
    if url.startswith("attachment:"):
        # attachment:<uuid>:<filename>
        _, _, rest = url.partition(":")
        _, _, name = rest.partition(":")
        return name or None
    name = unquote(urlsplit(unwrap(url)).path.rsplit("/", 1)[-1])
    return name or None


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def block_file_urls(value: dict[str, Any]) -> list[str]:
    """File URLs referenced by a block: media sources, page icon/cover, and any
    Notion-hosted URL inside properties/format (covers database "Files & media" columns)."""
    props = value.get("properties") or {}
    fmt = value.get("format") or {}
    urls: list[str] = []
    if value.get("type") in MEDIA_TYPES:
        source = props.get("source")
        if isinstance(source, list) and source and isinstance(source[0], list) and source[0]:
            urls.append(source[0][0])
        urls.append(fmt.get("display_source"))
    urls += [fmt.get("page_icon"), fmt.get("page_cover")]
    urls += [s for s in _strings([props, fmt]) if is_url(s) and is_hosted(s)]
    return list(dict.fromkeys(u for u in urls if is_url(u)))


def comment_file_urls(value: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(s for s in _strings(value) if is_url(s) and is_hosted(s)))


def register_files(store: Store, urls: list[str], block_id: str, space_id: str | None) -> int:
    for url in urls:
        store.add_file_ref(url_key(url), url, block_id, space_id, filename_of(url), is_hosted(url))
    return len(urls)


async def download_pending(
    client: NotionClient,
    store: Store,
    *,
    include_external: bool,
    max_bytes: int,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, int]:
    counts = {"files_ok": 0, "files_failed": 0, "files_too_large": 0, "files_forbidden": 0, "files_deferred": 0}
    rows = store.files_to_download(include_external)
    stopped = False
    for start in range(0, len(rows), DOWNLOAD_BATCH):
        batch = rows[start : start + DOWNLOAD_BATCH]
        hosted = [r for r in batch if r["hosted"]]
        signed = await client.get_signed_file_urls([(unwrap(r["source_url"]), r["block_id"], r["space_id"]) for r in hosted])
        targets: dict[str, str] = {}
        for r, s in zip(hosted, signed):
            targets[r["url_key"]] = s or client.signed_proxy_url(unwrap(r["source_url"]), r["block_id"], r["space_id"])
        for r in batch:
            targets.setdefault(r["url_key"], r["source_url"])

        async def one(r) -> None:
            nonlocal stopped
            key = r["url_key"]
            if stopped:
                return
            try:
                data, mime = await client.download(targets[key], max_bytes)
            except RateLimitExhausted:
                # Leave the file pending for the next run instead of counting a failed attempt.
                stopped = True
                return
            except FileTooLarge as e:
                store.file_failed(key, "skipped_too_large", f"size {e.args[0]} > {max_bytes}")
                counts["files_too_large"] += 1
            except NotionAuthError:
                raise
            except (httpx.HTTPError, NotionError) as e:
                status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else getattr(e, "status", None)
                if status == 403:
                    counts["files_forbidden"] += 1
                log.warning("download failed for %s: %s", r["filename"] or key, str(e).split("\n")[0][:200])
                store.file_failed(key, "failed", str(e))
                counts["files_failed"] += 1
            else:
                store.save_file(key, data, mime)
                counts["files_ok"] += 1
            if on_progress:
                on_progress(r["filename"] or key)

        await asyncio.gather(*(one(r) for r in batch))
        if stopped:
            counts["files_deferred"] = len(store.files_to_download(include_external))
            log.warning("附件下载被 Notion 持续限流，已停止；剩余 %d 个稍后再下载", counts["files_deferred"])
            break
    return counts
