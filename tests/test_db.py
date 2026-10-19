import json

from notion_backup.client import Record
from notion_backup.files import block_file_urls, filename_of, unwrap, url_key


def block(id, version, **extra):
    return Record("block", id, {"id": id, "version": version, "type": "text", **extra}, "editor", "s")


def test_upsert_maps_columns_and_keeps_raw(store):
    store.upsert_record(block("a", 3, properties={"title": [["hi"]]}, content=["b"], format={"collection_pointer": {"id": "c"}}))
    row = store.conn.execute("SELECT * FROM block WHERE id = 'a'").fetchone()
    assert row["version"] == 3 and row["space_id"] == "s" and row["role"] == "editor"
    assert json.loads(row["properties"]) == {"title": [["hi"]]}
    assert json.loads(row["content"]) == ["b"]
    assert row["collection_id"] == "c"
    assert json.loads(row["raw"])["format"] == {"collection_pointer": {"id": "c"}}


def test_upsert_never_goes_back_in_version(store):
    store.upsert_record(block("a", 5, properties={"title": [["new"]]}))
    store.upsert_record(block("a", 4, properties={"title": [["old"]]}))
    assert store.get_value("block", "a")["properties"] == {"title": [["new"]]}
    store.upsert_record(block("a", 5, alive=False))
    assert store.conn.execute("SELECT alive FROM block").fetchone()[0] == 0


def test_unknown_tables_go_to_record(store):
    store.upsert_record(Record("user_root", "u", {"id": "u", "version": 1}, None, None))
    assert store.conn.execute("SELECT table_name, id FROM record").fetchone()[:] == ("user_root", "u")


def test_file_blobs_are_deduplicated(store):
    for key in ("k1", "k2"):
        store.add_file_ref(key, key, "b", "s", None, True)
        store.save_file(key, b"same bytes", "image/png")
    assert store.conn.execute("SELECT count(*) FROM file_blob").fetchone()[0] == 1
    assert store.conn.execute("SELECT count(DISTINCT sha256) FROM file_ref").fetchone()[0] == 1


def test_external_files_skipped_unless_requested(store):
    store.add_file_ref("https://ext/a.png", "https://ext/a.png", "b", "s", "a.png", False)
    assert store.files_to_download(include_external=False) == []
    assert len(store.files_to_download(include_external=True)) == 1


def test_url_helpers():
    signed = "https://prod-files-secure.s3.us-west-2.amazonaws.com/ws/file/My%20Doc.pdf?X-Amz-Signature=abc"
    assert url_key(signed) == "https://prod-files-secure.s3.us-west-2.amazonaws.com/ws/file/My%20Doc.pdf"
    assert filename_of(signed) == "My Doc.pdf"
    assert filename_of("attachment:1234:report.pdf") == "report.pdf"


def test_block_file_urls():
    hosted = "https://prod-files-secure.s3.us-west-2.amazonaws.com/x/y.pdf"
    assert block_file_urls({"type": "image", "properties": {"source": [["https://ext.example/cat.jpg"]]}}) == [
        "https://ext.example/cat.jpg"
    ]
    # Database "Files & media" property on a row page, plus a hosted cover; built-in covers are ignored.
    row = {
        "type": "page",
        "properties": {"f": [["y.pdf", [["a", hosted]]]]},
        "format": {"page_cover": "/images/page-cover/gradients_8.png", "page_icon": "🙂"},
    }
    assert block_file_urls(row) == [hosted]


def test_reset_failed_files(store):
    store.add_file_ref("k", "attachment:k:x", "b", "s", "x", True)
    for _ in range(3):
        store.file_failed("k", "failed", "403")
    assert store.files_to_download(include_external=False) == []
    assert store.reset_failed_files() == 1
    assert len(store.files_to_download(include_external=False)) == 1


def test_unwrap_notion_image_proxy_urls():
    inner = "https://s3-us-west-2.amazonaws.com/secure.notion-static.com/10eb/pexels-photo.webp"
    wrapped = "https://www.notion.so/image/https%3A%2F%2Fs3-us-west-2.amazonaws.com%2Fsecure.notion-static.com%2F10eb%2Fpexels-photo.webp"
    assert unwrap(wrapped) == inner
    assert unwrap(inner) == inner
    assert filename_of(wrapped) == "pexels-photo.webp"
