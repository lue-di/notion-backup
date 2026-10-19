from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from rich.tree import Tree

from notion_backup.client import NotionAuthError, NotionClient, NotionError
from notion_backup.config import ConfigError, load_config, load_db_path
from notion_backup.crawler import Crawler, SyncOptions
from notion_backup.files import download_pending
from notion_backup.db import PAGE_TYPES, Store
from notion_backup.render import Renderer, format_time

app = typer.Typer(help="把 Notion 账号按块级记录备份到本地 SQLite。", no_args_is_help=True)
files_app = typer.Typer(help="附件操作。", no_args_is_help=True)
app.add_typer(files_app, name="files")
console = Console()

DbOption = Annotated[Optional[str], typer.Option("--db", help="SQLite 文件路径（默认取 NOTION_DB_PATH）")]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _open_store(db: str | None, must_exist: bool = True) -> Store:
    path = load_db_path(db)
    if must_exist and not path.exists():
        console.print(f"[red]数据库 {path} 不存在，请先运行 notion-backup sync[/red]")
        raise typer.Exit(1)
    return Store(path)


DEFERRED_HINT = (
    "[yellow]Notion 对附件下载按账号限额（实测约 30 分钟 600 个后会被持续限流），"
    "剩余 {n} 个已保留为待下载。过一段时间（比如 1 小时）再运行 notion-backup files download 即可继续。[/yellow]"
)


def _file_token_hint(has_file_token: bool) -> str:
    why = "NOTION_FILE_TOKEN 可能已过期" if has_file_token else "没有配置 NOTION_FILE_TOKEN"
    return (
        f"[yellow]有附件返回 403（{why}）。下载附件需要 file.notion.so 的 file_token cookie：\n"
        "DevTools → Application → Cookies → https://file.notion.so（或 Network 里任一 file.notion.so 请求的 Cookie）\n"
        "复制 file_token 写入 .env 的 NOTION_FILE_TOKEN=，然后运行 notion-backup files download[/yellow]"
    )


def _load_config_or_exit(db: str | None):
    try:
        return load_config(db)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


def _run_with_client(cfg, coro_fn, concurrency: int = 4, rate: float = 3.0, file_rate: float = 1.0):
    """Run `coro_fn(client)` and turn API failures into clean exits."""

    async def main():
        async with NotionClient(
            cfg.token_v2, cfg.user_id, cfg.file_token, concurrency=concurrency, rate=rate, file_rate=file_rate
        ) as client:
            return await coro_fn(client), client.has_file_token

    try:
        return asyncio.run(main())
    except NotionAuthError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2)
    except NotionError as e:
        console.print(f"[red]失败: {e}[/red]")
        raise typer.Exit(1)


def _normalize_id(raw: str) -> str:
    """Accept a page URL or a dashed/undashed id."""
    tail = raw.rstrip("/").rsplit("/", 1)[-1].split("?")[0].split("#")[0]
    hexpart = tail.replace("-", "")[-32:]
    if len(hexpart) == 32 and all(c in "0123456789abcdef" for c in hexpart.lower()):
        h = hexpart.lower()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    return raw


@app.command()
def sync(
    full: Annotated[bool, typer.Option("--full", help="忽略增量状态，重新加载每个页面")] = False,
    space: Annotated[Optional[list[str]], typer.Option("--space", help="只同步指定工作区 id（可重复）")] = None,
    no_files: Annotated[bool, typer.Option("--no-files", help="不下载附件")] = False,
    external_files: Annotated[bool, typer.Option("--external-files", help="也下载外链文件/图片")] = False,
    max_file_mb: Annotated[int, typer.Option("--max-file-mb", help="单个附件大小上限 (MB)")] = 200,
    concurrency: Annotated[int, typer.Option("--concurrency", help="并发请求数")] = 4,
    rate: Annotated[float, typer.Option("--rate", help="API 每秒最多请求数（遇到 429 会自动降速）")] = 3.0,
    file_rate: Annotated[float, typer.Option("--file-rate", help="附件每秒最多下载数（遇到 429 会自动降速）")] = 1.0,
    db: DbOption = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """备份（首次全量，之后增量）。"""
    _setup_logging(verbose)
    cfg = _load_config_or_exit(db)
    store = Store(cfg.db_path)
    opts = SyncOptions(
        full=full,
        space_ids=set(space or []),
        download_files=not no_files,
        external_files=external_files,
        max_file_bytes=max_file_mb * 1024 * 1024,
    )
    started = time.monotonic()

    async def crawl(client):
        with console.status("开始同步…") as status:

            def progress(stats, msg):
                status.update(
                    f"已加载 {stats['pages_loaded']} 页 · 未变化 {stats['pages_unchanged']} · "
                    f"数据库 {stats['collections']} · 附件 {stats['files_ok']} · {msg}"
                )

            return await Crawler(client, store, opts, progress).run()

    try:
        stats, has_file_token = _run_with_client(cfg, crawl, concurrency, rate, file_rate)
    finally:
        store.close()

    table = Table(title=f"同步完成（{time.monotonic() - started:.0f}s）→ {cfg.db_path}")
    table.add_column("项目")
    table.add_column("数量", justify="right")
    for key in (
        "spaces", "roots", "pages_loaded", "pages_unchanged", "collections", "blocks", "records_written",
        "blocks_deleted", "pages_deleted", "pages_unreachable", "files_registered", "files_ok",
        "files_failed", "files_forbidden", "files_too_large", "files_deferred", "errors",
    ):  # fmt: skip
        if stats.get(key):
            table.add_row(key, str(stats[key]))
    console.print(table)
    if stats.get("errors"):
        console.print("[yellow]有页面重试后仍失败，下次同步会自动重新加载（加 -v 查看详情）[/yellow]")
    if stats.get("files_forbidden"):
        console.print(_file_token_hint(has_file_token))
    if stats.get("files_deferred"):
        console.print(DEFERRED_HINT.format(n=stats["files_deferred"]))


@app.command()
def stats(db: DbOption = None) -> None:
    """数据库统计。"""
    store = _open_store(db)
    q = lambda sql, *a: store.conn.execute(sql, a).fetchone()[0]  # noqa: E731
    table = Table(title=str(store.path))
    table.add_column("表")
    table.add_column("存活", justify="right")
    table.add_column("已删除", justify="right")
    for t in ("space", "team", "notion_user", "block", "collection", "collection_view", "discussion", "comment"):
        table.add_row(
            t, str(q(f"SELECT count(*) FROM {t} WHERE alive = 1")), str(q(f"SELECT count(*) FROM {t} WHERE alive = 0"))
        )
    table.add_row("record (其他)", str(q("SELECT count(*) FROM record")), "")
    console.print(table)

    pages = q(f"SELECT count(*) FROM block WHERE alive = 1 AND type IN {PAGE_TYPES}")
    unreachable = q("SELECT count(*) FROM page_sync_state WHERE unreachable = 1")
    console.print(f"页面: {pages}（不可达但保留: {unreachable}）")

    ft = Table(title="附件")
    ft.add_column("状态")
    ft.add_column("数量", justify="right")
    for status, n in store.conn.execute("SELECT status, count(*) FROM file_ref GROUP BY status"):
        ft.add_row(status, str(n))
    blobs, size = store.conn.execute("SELECT count(*), COALESCE(sum(size), 0) FROM file_blob").fetchone()
    ft.add_row("去重后文件", f"{blobs}（{size / 1024 / 1024:.1f} MB）")
    console.print(ft)

    rt = Table(title="最近同步")
    for col in ("id", "开始", "模式", "耗时", "结果"):
        rt.add_column(col)
    for r in store.conn.execute("SELECT * FROM sync_run ORDER BY id DESC LIMIT 5"):
        s = json.loads(r["stats"] or "{}")
        took = f"{r['finished_at'] - r['started_at']}s" if r["finished_at"] else "进行中/中断"
        result = r["error"] or f"加载 {s.get('pages_loaded', 0)} 页, 未变 {s.get('pages_unchanged', 0)}"
        rt.add_row(str(r["id"]), format_time(r["started_at"] * 1000), r["mode"], took, result)
    console.print(rt)
    store.close()


@app.command()
def ls(
    depth: Annotated[int, typer.Option("--depth", "-d", help="展开层数")] = 2,
    db: DbOption = None,
) -> None:
    """列出工作区与页面树。"""
    store = _open_store(db)
    r = Renderer(store)
    child_sql = (
        f"SELECT id FROM block WHERE page_id = ? AND alive = 1 AND type IN {PAGE_TYPES} ORDER BY created_time"
    )

    def add(node: Tree, page_id: str, level: int) -> None:
        sub = node.add(f"{r.title_of(page_id)} [dim]{page_id}[/dim]")
        if level < depth:
            for (cid,) in store.conn.execute(child_sql, (page_id,)).fetchall():
                add(sub, cid, level + 1)

    for space in store.conn.execute("SELECT id, raw FROM space WHERE alive = 1").fetchall():
        sv = json.loads(space["raw"])
        tree = Tree(f"[bold]{sv.get('name') or space['id']}[/bold] [dim]{space['id']}[/dim]")
        roots: list[str] = list(sv.get("pages") or [])
        for team in store.conn.execute("SELECT raw FROM team WHERE space_id = ? AND alive = 1", (space["id"],)):
            roots += json.loads(team["raw"]).get("pages") or []
        for view in store.conn.execute("SELECT raw FROM space_view WHERE space_id = ?", (space["id"],)):
            roots += json.loads(view["raw"]).get("private_pages") or []
        for pid in dict.fromkeys(roots):
            if store.conn.execute("SELECT 1 FROM block WHERE id = ? AND alive = 1", (pid,)).fetchone():
                add(tree, pid, 1)
        console.print(tree)
    store.close()


@app.command()
def show(
    page: Annotated[str, typer.Argument(help="页面 id 或 Notion 链接")],
    db: DbOption = None,
) -> None:
    """把本地备份中的某个页面渲染成 Markdown。"""
    store = _open_store(db)
    try:
        md = Renderer(store).render_page(_normalize_id(page))
    except KeyError:
        console.print(f"[red]本地备份中没有页面 {page}[/red]")
        raise typer.Exit(1)
    finally:
        store.close()
    typer.echo(md)


@files_app.command("download")
def files_download(
    external_files: Annotated[bool, typer.Option("--external-files", help="也下载外链文件/图片")] = False,
    max_file_mb: Annotated[int, typer.Option("--max-file-mb", help="单个附件大小上限 (MB)")] = 200,
    rate: Annotated[float, typer.Option("--rate", help="API 每秒最多请求数")] = 3.0,
    file_rate: Annotated[float, typer.Option("--file-rate", help="附件每秒最多下载数（遇到 429 会自动降速）")] = 1.0,
    db: DbOption = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """只下载附件（不重新爬取页面），之前失败的会重试。"""
    _setup_logging(verbose)
    cfg = _load_config_or_exit(db)
    store = _open_store(db)
    reset = store.reset_failed_files()

    async def download(client):
        with console.status("下载附件…") as status:
            done = 0

            def progress(name):
                nonlocal done
                done += 1
                status.update(f"已处理 {done} 个附件 · {name}")

            return await download_pending(
                client,
                store,
                include_external=external_files,
                max_bytes=max_file_mb * 1024 * 1024,
                on_progress=progress,
            )

    try:
        counts, has_file_token = _run_with_client(cfg, download, rate=rate, file_rate=file_rate)
    finally:
        store.close()
    console.print(
        f"成功 {counts['files_ok']} · 失败 {counts['files_failed']}（其中 403: {counts['files_forbidden']}）"
        f" · 超过大小上限 {counts['files_too_large']} · 重置了 {reset} 个失败记录"
    )
    if counts["files_forbidden"]:
        console.print(_file_token_hint(has_file_token))
    if counts["files_deferred"]:
        console.print(DEFERRED_HINT.format(n=counts["files_deferred"]))


@files_app.command("extract")
def files_extract(
    key: Annotated[str, typer.Argument(help="块 id 或 file_ref.url_key")],
    out_dir: Annotated[Path, typer.Argument(help="导出目录")] = Path("."),
    db: DbOption = None,
) -> None:
    """把附件从数据库导出到目录。"""
    store = _open_store(db)
    rows = store.find_files(_normalize_id(key)) or store.find_files(key)
    store.close()
    if not rows:
        console.print("[red]没有找到已下载的附件[/red]")
        raise typer.Exit(1)
    out_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        name = Path(row["filename"] or row["url_key"].rsplit("/", 1)[-1] or "file").name
        target = out_dir / name
        target.write_bytes(row["data"])
        console.print(f"{target} ({len(row['data'])} bytes, {row['mime_type'] or '?'})")
