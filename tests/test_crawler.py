import json

from notion_backup.crawler import Crawler, SyncOptions, walk_page
from notion_backup.render import Renderer

from fake_notion import SPACE, sample_workspace


async def sync(fake, store, **opts):
    return await Crawler(fake, store, SyncOptions(**opts)).run()


def alive(store, id):
    row = store.conn.execute("SELECT alive FROM block WHERE id = ?", (id,)).fetchone()
    return None if row is None else bool(row["alive"])


def test_walk_page_stops_at_subpages():
    blocks = {
        "p": {"id": "p", "type": "page", "content": ["a", "sub", "gone"]},
        "a": {"id": "a", "type": "toggle", "content": ["a1"]},
        "a1": {"id": "a1", "type": "text"},
        "sub": {"id": "sub", "type": "page", "content": ["deep"]},
    }
    tree = walk_page("p", blocks)
    assert tree.block_ids == ["a", "a1", "sub"]
    assert [c["id"] for c in tree.containers] == ["sub"]
    assert tree.missing == {"gone"}


async def test_first_sync_backs_up_everything(store):
    fake = sample_workspace()
    stats = await sync(fake, store)

    assert set(fake.loaded) == {"home", "child", "row1", "row2"}
    assert stats["pages_loaded"] == 4
    assert stats["collections"] == 1
    for id in ("home", "intro", "toggle", "nested", "img", "child", "child_text", "db", "row1", "row2", "row2_text"):
        assert alive(store, id), id

    page_of = dict(store.conn.execute("SELECT id, page_id FROM block").fetchall())
    assert page_of["nested"] == "home"
    assert page_of["child"] == "home"  # a sub-page belongs to its parent's tree
    assert page_of["child_text"] == "child"
    assert page_of["row2_text"] == "row2"

    assert store.get_value("collection", "coll")["name"] == [["Tasks"]]
    assert store.get_value("comment", "c1")["text"] == [["nice"]]
    assert store.conn.execute("SELECT name, email FROM notion_user").fetchone()[:] == ("Me", "me@example.com")
    assert store.conn.execute("SELECT name FROM space").fetchone()[0] == "My Space"

    ref = store.conn.execute("SELECT * FROM file_ref").fetchone()
    assert ref["url_key"] == "attachment:abc-123:photo.png"
    assert ref["status"] == "ok" and ref["filename"] == "photo.png" and ref["block_id"] == "img"
    blob = store.conn.execute("SELECT data, mime_type FROM file_blob").fetchone()
    assert blob["data"].startswith(b"data:") and blob["mime_type"] == "image/png"


async def test_unchanged_sync_reloads_nothing(store):
    fake = sample_workspace()
    await sync(fake, store)
    fake.loaded.clear()
    fake.downloads.clear()

    stats = await sync(fake, store)
    assert fake.loaded == []
    assert fake.downloads == []
    assert stats["pages_unchanged"] == 4
    assert fake.queried_collections == ["coll", "coll"]  # databases are always re-listed


async def test_edit_reloads_only_that_page_and_soft_deletes(store):
    fake = sample_workspace()
    await sync(fake, store)
    fake.loaded.clear()

    # Remove the toggle (and with it the nested block) from Home, and edit Child's text.
    fake.edit("home", content=["intro", "img", "child", "db"])
    fake.edit("child_text", properties={"title": [["edited"]]})
    fake.touch_page("child")
    stats = await sync(fake, store)

    assert sorted(fake.loaded) == ["child", "home"]
    assert alive(store, "toggle") is False
    assert alive(store, "nested") is False
    assert alive(store, "intro") is True
    assert store.get_value("block", "child_text")["properties"]["title"] == [["edited"]]
    assert stats["blocks_deleted"] == 2


async def test_new_database_row_and_full_mode(store):
    fake = sample_workspace()
    await sync(fake, store)
    fake.loaded.clear()

    fake.block("row3", "page", "coll", "Task 3", parent_table="collection")
    await sync(fake, store)
    assert fake.loaded == ["row3"]

    fake.loaded.clear()
    await sync(fake, store, full=True)
    assert set(fake.loaded) == {"home", "child", "row1", "row2", "row3"}


async def test_deleted_subpage_is_detected(store):
    fake = sample_workspace()
    await sync(fake, store)

    fake.edit("child", alive=False)
    fake.edit("home", content=["intro", "toggle", "img", "db"])
    stats = await sync(fake, store)

    assert alive(store, "child") is False
    assert alive(store, "child_text") is False
    assert stats["pages_deleted"] == 1


async def test_subpage_moved_to_sidebar_root_stays_alive(store):
    fake = sample_workspace()
    await sync(fake, store)

    fake.edit("child", parent_id=SPACE, parent_table="space")
    fake.records["space"][SPACE]["pages"].append("child")
    fake.edit("home", content=["intro", "toggle", "img", "db"])
    await sync(fake, store)

    assert alive(store, "child") is True
    assert alive(store, "child_text") is True


async def test_missing_blocks_are_fetched_individually(store):
    fake = sample_workspace()
    fake.chunk_block_limit = 2  # loadPageChunk returns only the page and its first block
    await sync(fake, store)
    for id in ("toggle", "nested", "img", "db", "child_text", "row2_text"):
        assert alive(store, id), id


async def test_failed_downloads_are_retried(store):
    fake = sample_workspace()

    async def broken(url, max_bytes):
        import httpx

        raise httpx.ConnectError("boom")

    good = fake.download
    fake.download = broken
    stats = await sync(fake, store)
    assert stats["files_failed"] == 1
    assert store.conn.execute("SELECT status, attempts FROM file_ref").fetchone()[:] == ("failed", 1)

    fake.download = good
    stats = await sync(fake, store)
    assert stats["files_ok"] == 1
    assert store.conn.execute("SELECT status FROM file_ref").fetchone()[0] == "ok"


async def test_render_page(store):
    fake = sample_workspace()
    await sync(fake, store)
    md = Renderer(store).render_page("home")
    assert md.startswith("# Home\n")
    assert "- ▸ toggle" in md and "  nested" in md
    assert "📄 [Child](notion://child)" in md
    assert "🗃 **Tasks** (2 rows)" in md
    assert "| [Task 1](notion://row1) | Done |" in md
    assert "![image](attachment:abc-123:photo.png)" in md

    row = Renderer(store).render_page("row1")
    assert "- **Status**: Done" in row


async def test_sync_run_is_recorded(store):
    fake = sample_workspace()
    await sync(fake, store)
    run = store.conn.execute("SELECT * FROM sync_run").fetchone()
    assert run["finished_at"] and run["error"] is None
    assert json.loads(run["stats"])["pages_loaded"] == 4


async def test_failed_pages_are_retried_in_the_same_run(store):
    fake = sample_workspace()
    fake.fail_once = {"child", "row1"}
    stats = await sync(fake, store)
    assert set(fake.loaded) == {"home", "child", "row1", "row2"}
    assert stats["errors"] == 0
    assert alive(store, "child_text")


async def test_downloads_stop_and_stay_pending_when_quota_is_exhausted(store):
    from notion_backup.client import RateLimitExhausted

    fake = sample_workspace()
    fake.block("img2", "image", "home", properties={"source": [["attachment:def-456:two.png"]]})

    async def exhausted(url, max_bytes):
        raise RateLimitExhausted("quota", 429)

    good = fake.download
    fake.download = exhausted
    stats = await sync(fake, store)
    assert stats["files_deferred"] == 2
    assert [tuple(r) for r in store.conn.execute("SELECT DISTINCT status, attempts FROM file_ref")] == [("pending", 0)]

    fake.download = good
    stats = await sync(fake, store)
    assert stats["files_ok"] == 2
