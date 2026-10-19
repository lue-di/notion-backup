-- Mirrors Notion's own record model: one table per record table.
-- Frequently queried fields are columns; `raw` always holds the complete record JSON.
-- JSON-typed columns store JSON text (query them with json_extract / json_each).
-- Times are Notion's epoch milliseconds; synced_at is epoch seconds.

CREATE TABLE IF NOT EXISTS block (
    id                TEXT PRIMARY KEY,
    space_id          TEXT,
    version           INTEGER,
    type              TEXT,
    parent_id         TEXT,
    parent_table      TEXT,
    page_id           TEXT,      -- page whose content tree contains this block (computed locally)
    properties        TEXT,      -- JSON
    content           TEXT,      -- JSON: ordered child block ids
    format            TEXT,      -- JSON
    view_ids          TEXT,      -- JSON
    collection_id     TEXT,
    discussions       TEXT,      -- JSON
    file_ids          TEXT,      -- JSON
    created_time      INTEGER,
    last_edited_time  INTEGER,
    created_by_id     TEXT,
    last_edited_by_id TEXT,
    alive             INTEGER NOT NULL DEFAULT 1,
    role              TEXT,
    raw               TEXT NOT NULL,
    synced_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS block_parent ON block(parent_id);
CREATE INDEX IF NOT EXISTS block_page ON block(page_id);
CREATE INDEX IF NOT EXISTS block_type ON block(type);
CREATE INDEX IF NOT EXISTS block_space ON block(space_id);

CREATE TABLE IF NOT EXISTS collection (
    id             TEXT PRIMARY KEY,
    space_id       TEXT,
    version        INTEGER,
    parent_id      TEXT,
    parent_table   TEXT,
    name           TEXT,      -- JSON rich text
    schema         TEXT,      -- JSON: property id -> {name, type, ...}
    format         TEXT,      -- JSON
    template_pages TEXT,      -- JSON
    alive          INTEGER NOT NULL DEFAULT 1,
    role           TEXT,
    raw            TEXT NOT NULL,
    synced_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS collection_parent ON collection(parent_id);

CREATE TABLE IF NOT EXISTS collection_view (
    id           TEXT PRIMARY KEY,
    space_id     TEXT,
    version      INTEGER,
    type         TEXT,
    name         TEXT,
    parent_id    TEXT,
    parent_table TEXT,
    query2       TEXT,        -- JSON
    format       TEXT,        -- JSON
    alive        INTEGER NOT NULL DEFAULT 1,
    role         TEXT,
    raw          TEXT NOT NULL,
    synced_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS collection_view_parent ON collection_view(parent_id);

CREATE TABLE IF NOT EXISTS space (
    id        TEXT PRIMARY KEY,
    space_id  TEXT,
    version   INTEGER,
    name      TEXT,
    icon      TEXT,
    pages     TEXT,           -- JSON
    alive     INTEGER NOT NULL DEFAULT 1,
    role      TEXT,
    raw       TEXT NOT NULL,
    synced_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS space_view (
    id               TEXT PRIMARY KEY,
    space_id         TEXT,
    version          INTEGER,
    parent_id        TEXT,
    bookmarked_pages TEXT,    -- JSON
    private_pages    TEXT,    -- JSON
    joined_teams     TEXT,    -- JSON
    alive            INTEGER NOT NULL DEFAULT 1,
    role             TEXT,
    raw              TEXT NOT NULL,
    synced_at        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS team (
    id        TEXT PRIMARY KEY,
    space_id  TEXT,
    version   INTEGER,
    name      TEXT,
    pages     TEXT,           -- JSON
    alive     INTEGER NOT NULL DEFAULT 1,
    role      TEXT,
    raw       TEXT NOT NULL,
    synced_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notion_user (
    id            TEXT PRIMARY KEY,
    space_id      TEXT,
    version       INTEGER,
    name          TEXT,
    email         TEXT,
    profile_photo TEXT,
    alive         INTEGER NOT NULL DEFAULT 1,
    role          TEXT,
    raw           TEXT NOT NULL,
    synced_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS discussion (
    id           TEXT PRIMARY KEY,
    space_id     TEXT,
    version      INTEGER,
    parent_id    TEXT,
    parent_table TEXT,
    resolved     INTEGER,
    context      TEXT,        -- JSON: the highlighted text
    comments     TEXT,        -- JSON: comment ids
    alive        INTEGER NOT NULL DEFAULT 1,
    role         TEXT,
    raw          TEXT NOT NULL,
    synced_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS discussion_parent ON discussion(parent_id);

CREATE TABLE IF NOT EXISTS comment (
    id               TEXT PRIMARY KEY,
    space_id         TEXT,
    version          INTEGER,
    parent_id        TEXT,
    parent_table     TEXT,
    text             TEXT,    -- JSON rich text
    created_time     INTEGER,
    last_edited_time INTEGER,
    created_by_id    TEXT,
    alive            INTEGER NOT NULL DEFAULT 1,
    role             TEXT,
    raw              TEXT NOT NULL,
    synced_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS comment_parent ON comment(parent_id);

-- Any other record table Notion returns (user_root, space_user, bot, ...).
CREATE TABLE IF NOT EXISTS record (
    table_name TEXT NOT NULL,
    id         TEXT NOT NULL,
    space_id   TEXT,
    version    INTEGER,
    alive      INTEGER NOT NULL DEFAULT 1,
    role       TEXT,
    raw        TEXT NOT NULL,
    synced_at  INTEGER NOT NULL,
    PRIMARY KEY (table_name, id)
);

-- Attachments. Content is deduplicated by sha256.
CREATE TABLE IF NOT EXISTS file_blob (
    sha256    TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    mime_type TEXT,
    data      BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS file_ref (
    url_key       TEXT PRIMARY KEY,  -- URL without signature query, or attachment:<id>:<name>
    source_url    TEXT NOT NULL,
    block_id      TEXT,
    space_id      TEXT,
    filename      TEXT,
    hosted        INTEGER NOT NULL,  -- 1 = stored by Notion, 0 = external link
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | skipped_too_large | failed | external
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    sha256        TEXT REFERENCES file_blob(sha256),
    downloaded_at INTEGER
);
CREATE INDEX IF NOT EXISTS file_ref_block ON file_ref(block_id);
CREATE INDEX IF NOT EXISTS file_ref_status ON file_ref(status);

CREATE TABLE IF NOT EXISTS page_sync_state (
    page_id          TEXT PRIMARY KEY,
    space_id         TEXT,
    version          INTEGER,
    last_edited_time INTEGER,
    synced_at        INTEGER NOT NULL,
    last_seen_run    INTEGER,
    unreachable      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    mode        TEXT NOT NULL,
    stats       TEXT,        -- JSON
    error       TEXT
);
