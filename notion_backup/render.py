"""Render a backed-up page from the local database as Markdown (for checking backups)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from notion_backup.db import Store, collection_id_of

INDENT = "  "


def plain_text(rich: Any) -> str:
    """Rich text -> plain string, keeping mention placeholders readable."""
    if not isinstance(rich, list):
        return ""
    out = []
    for seg in rich:
        if not isinstance(seg, list) or not seg:
            continue
        text = seg[0] if isinstance(seg[0], str) else ""
        for ann in seg[1] if len(seg) > 1 and isinstance(seg[1], list) else []:
            if ann and ann[0] == "d" and len(ann) > 1 and isinstance(ann[1], dict):
                d = ann[1]
                text = d.get("start_date", "") + (f" → {d['end_date']}" if d.get("end_date") else "")
        out.append(text)
    return "".join(out)


class Renderer:
    def __init__(self, store: Store):
        self.store = store
        self._blocks: dict[str, dict[str, Any]] = {}

    # ---- lookups ------------------------------------------------------------

    def _load_page_blocks(self, page_id: str) -> None:
        rows = self.store.conn.execute(
            "SELECT raw FROM block WHERE (page_id = ? OR id = ?) AND alive = 1", (page_id, page_id)
        ).fetchall()
        for row in rows:
            v = json.loads(row["raw"])
            self._blocks[v["id"]] = v

    def _block(self, bid: str) -> dict[str, Any] | None:
        if bid not in self._blocks:
            v = self.store.get_value("block", bid)
            if v is None:
                return None
            self._blocks[bid] = v
        return self._blocks[bid]

    def title_of(self, bid: str) -> str:
        b = self._block(bid)
        if not b:
            return bid
        if b.get("type") == "collection_view_page":
            c = self.store.get_value("collection", collection_id_of(b) or "")
            if c:
                return plain_text(c.get("name")) or "Untitled"
        return plain_text((b.get("properties") or {}).get("title")) or "Untitled"

    def user_name(self, uid: str) -> str:
        u = self.store.get_value("notion_user", uid)
        return (u or {}).get("name") or uid

    # ---- rich text ----------------------------------------------------------

    def rich(self, rich: Any) -> str:
        if not isinstance(rich, list):
            return ""
        out = []
        for seg in rich:
            if not isinstance(seg, list) or not seg:
                continue
            text = seg[0] if isinstance(seg[0], str) else ""
            anns = seg[1] if len(seg) > 1 and isinstance(seg[1], list) else []
            link = None
            for ann in anns:
                if not ann:
                    continue
                kind = ann[0]
                if kind == "p" and len(ann) > 1:
                    text = f"[{self.title_of(ann[1])}](notion://{ann[1]})"
                elif kind == "u" and len(ann) > 1:
                    text = "@" + self.user_name(ann[1])
                elif kind == "d" and len(ann) > 1 and isinstance(ann[1], dict):
                    text = plain_text([seg])
                elif kind == "e" and len(ann) > 1:
                    text = f"${ann[1]}$"
                elif kind == "a" and len(ann) > 1:
                    link = ann[1]
            for kind, wrap in (("c", "`"), ("b", "**"), ("i", "*"), ("s", "~~")):
                if any(a and a[0] == kind for a in anns) and text.strip():
                    text = f"{wrap}{text}{wrap}"
            out.append(f"[{text}]({link})" if link else text)
        return "".join(out)

    # ---- blocks -------------------------------------------------------------

    def render_page(self, page_id: str) -> str:
        self._load_page_blocks(page_id)
        page = self._block(page_id)
        if page is None:
            raise KeyError(page_id)
        lines = [f"# {self.title_of(page_id)}", ""]
        if page.get("type") == "collection_view_page":
            lines += self._database(page, 0)
        else:
            row_props = self._row_properties(page)
            if row_props:
                lines += row_props + [""]
            lines += self._children(page, 0)
        return "\n".join(lines).rstrip() + "\n"

    def _children(self, block: dict[str, Any], depth: int) -> list[str]:
        lines: list[str] = []
        number = 0
        for cid in block.get("content") or []:
            child = self._block(cid)
            if not child or child.get("alive") is False:
                continue
            number = number + 1 if child.get("type") == "numbered_list" else 0
            lines += self._render(child, depth, number)
        return lines

    def _render(self, b: dict[str, Any], depth: int, number: int) -> list[str]:
        t = b.get("type")
        props = b.get("properties") or {}
        fmt = b.get("format") or {}
        pad = INDENT * depth
        title = self.rich(props.get("title"))
        nested = lambda d=depth + 1: self._children(b, d)  # noqa: E731

        if t == "text":
            return [pad + title, ""] + nested()
        if t in ("header", "sub_header", "sub_sub_header"):
            level = {"header": 1, "sub_header": 2, "sub_sub_header": 3}[t] + 1
            return [pad + "#" * level + " " + title, ""] + nested()
        if t == "bulleted_list":
            return [f"{pad}- {title}"] + nested()
        if t == "numbered_list":
            return [f"{pad}{number}. {title}"] + nested()
        if t == "to_do":
            checked = plain_text(props.get("checked")) == "Yes"
            return [f"{pad}- [{'x' if checked else ' '}] {title}"] + nested()
        if t == "toggle":
            return [f"{pad}- ▸ {title}"] + nested()
        if t == "quote":
            return [f"{pad}> {title}", ""] + nested()
        if t == "callout":
            return [f"{pad}> {fmt.get('page_icon') or '💡'} {title}", ""] + nested()
        if t == "code":
            lang = plain_text(props.get("language")).lower()
            body = plain_text(props.get("title"))
            return [f"{pad}```{lang}", *(pad + line for line in body.split("\n")), f"{pad}```", ""]
        if t == "equation":
            return [f"{pad}$$ {plain_text(props.get('title'))} $$", ""]
        if t == "divider":
            return [pad + "---", ""]
        if t in ("page", "collection_view_page"):
            return [f"{pad}📄 [{self.title_of(b['id'])}](notion://{b['id']})", ""]
        if t == "alias":
            target = (fmt.get("alias_pointer") or {}).get("id")
            return [f"{pad}↗ [{self.title_of(target) if target else '?'}](notion://{target})", ""]
        if t in ("image", "file", "pdf", "video", "audio"):
            src = plain_text(props.get("source")) or fmt.get("display_source") or ""
            caption = self.rich(props.get("caption")) or plain_text(props.get("title")) or t
            prefix = "!" if t == "image" else ""
            return [f"{pad}{prefix}[{caption}]({src})", ""]
        if t in ("bookmark", "embed", "link_preview"):
            link = plain_text(props.get("link")) or plain_text(props.get("source"))
            return [f"{pad}[{title or link}]({link})", ""]
        if t == "table":
            return self._table(b, pad) + [""]
        if t == "collection_view":
            return self._database(b, depth) + [""]
        if t in ("column_list", "column", "transclusion_container"):
            return self._children(b, depth)
        if t == "transclusion_reference":
            ref = (fmt.get("transclusion_reference_pointer") or {}).get("id")
            return [f"{pad}[synced block → {ref}]", ""]
        return [f"{pad}[{t}] {title}".rstrip(), ""] + nested()

    def _table(self, b: dict[str, Any], pad: str) -> list[str]:
        fmt = b.get("format") or {}
        cols = fmt.get("table_block_column_order") or []
        rows = [self._block(r) for r in b.get("content") or []]
        rows = [r for r in rows if r and r.get("alive") is not False]
        if not cols or not rows:
            return []
        cell = lambda r, c: self.rich((r.get("properties") or {}).get(c)).replace("|", "\\|")  # noqa: E731
        lines = [pad + "| " + " | ".join(cell(r, c) for c in cols) + " |" for r in rows]
        sep = pad + "|" + "---|" * len(cols)
        if fmt.get("table_block_column_header"):
            lines.insert(1, sep)
        else:
            lines.insert(0, pad + "|" + "   |" * len(cols))
            lines.insert(1, sep)
        return lines

    # ---- databases ----------------------------------------------------------

    def _schema(self, collection: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        schema = collection.get("schema") or {}
        return sorted(schema.items(), key=lambda kv: (kv[0] != "title", kv[1].get("name", "")))

    def _row_properties(self, page: dict[str, Any]) -> list[str]:
        if page.get("parent_table") != "collection":
            return []
        collection = self.store.get_value("collection", page.get("parent_id") or "")
        if not collection:
            return []
        props = page.get("properties") or {}
        return [
            f"- **{spec.get('name', pid)}**: {self._prop_value(props.get(pid), spec)}"
            for pid, spec in self._schema(collection)
            if pid != "title" and props.get(pid)
        ]

    def _prop_value(self, value: Any, spec: dict[str, Any]) -> str:
        if spec.get("type") == "checkbox":
            return "☑" if plain_text(value) == "Yes" else "☐"
        return self.rich(value).replace("\n", " ").replace("|", "\\|")

    def _database(self, b: dict[str, Any], depth: int) -> list[str]:
        pad = INDENT * depth
        cid = collection_id_of(b)
        collection = self.store.get_value("collection", cid or "") or {}
        name = plain_text(collection.get("name")) or "Untitled database"
        rows = self.store.conn.execute(
            "SELECT raw FROM block WHERE parent_id = ? AND parent_table = 'collection' AND alive = 1 "
            "ORDER BY created_time",
            (cid,),
        ).fetchall()
        lines = [f"{pad}🗃 **{name}** ({len(rows)} rows)", ""]
        schema = self._schema(collection)
        if not rows or not schema:
            return lines
        lines.append(pad + "| " + " | ".join(spec.get("name", pid) for pid, spec in schema) + " |")
        lines.append(pad + "|" + "---|" * len(schema))
        for row in rows:
            v = json.loads(row["raw"])
            props = v.get("properties") or {}
            cells = [
                f"[{self._prop_value(props.get(pid), spec) or 'Untitled'}](notion://{v['id']})"
                if pid == "title"
                else self._prop_value(props.get(pid), spec)
                for pid, spec in schema
            ]
            lines.append(pad + "| " + " | ".join(cells) + " |")
        return lines


def format_time(ms: int | None) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
