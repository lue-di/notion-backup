# notion-backup

把 Notion 账号里**所有工作区的全部页面、块、数据库、评论和附件**，按 Notion 自身的记录模型（`block` / `collection` / `collection_view` / `space` / `team` / `notion_user` / `discussion` / `comment` …）备份到本地 SQLite。附件以 BLOB 存进同一个数据库。支持增量同步。

> 使用的是 Notion 网页端的内部 API（`/api/v3`，非公开、无文档），未来可能变动。本项目为非官方工具，与 Notion Labs, Inc. 无关；请只用于备份你自己有权访问的内容。

## 准备

1. 浏览器登录 notion.so → DevTools → Application → Cookies → `https://www.notion.so`
2. 复制 `token_v2` 和 `notion_user_id`（浏览器里登录了多个账号时必填）
3. 下载附件还需要 `file_token`：它在 **`https://file.notion.so`** 域名下（不在 www.notion.so 下）。在 Cookies 列表里选 `https://file.notion.so`；如果没有，先在 Notion 里打开一张图片，或在 Network 面板里点开任一 `file.notion.so` 请求看 Cookie。没有它的话，大部分附件会 403
4. 写入配置：

```bash
cp .env.example .env
chmod 600 .env
# 编辑 .env 填入 NOTION_TOKEN_V2 / NOTION_FILE_TOKEN / NOTION_USER_ID
```

`token_v2` 和 `file_token` 等同于你账号的登录权限：不要提交、不要分享。退出 Notion 登录会让它失效，届时重新获取即可。

## 使用

```bash
uv sync
uv run notion-backup sync              # 首次全量，之后增量
uv run notion-backup sync --full       # 强制重新加载所有页面
uv run notion-backup sync --no-files   # 跳过附件
uv run notion-backup stats             # 各表数量、附件、最近同步记录
uv run notion-backup ls -d 3           # 工作区页面树
uv run notion-backup show <页面id或链接>  # 从本地备份渲染成 Markdown
uv run notion-backup files download     # 只补下载附件（不重新爬页面），重试之前失败的
uv run notion-backup files extract <块id> ./out   # 导出附件
```

其他选项：`--space <id>`（只同步某个工作区）、`--external-files`（外链图片也下载）、`--max-file-mb 200`、`--concurrency 4`、`--rate 3`（API 每秒请求数）、`--file-rate 1`（附件每秒下载数；file.notion.so 的限流比 API 严得多，实测可持续约 0.3 个/秒，遇到 429 会自动降速）、`--db path.db`、`-v`。

## 数据库结构

见 [`notion_backup/schema.sql`](notion_backup/schema.sql)。要点：

- 每种 Notion 记录一张表；常用字段拆成列，`raw` 列保存完整的原始 JSON，任何字段都不会丢。其他记录类型进 `record` 表。
- 块树：`block.content` 是有序子块 id 列表（JSON），`parent_id` / `parent_table` 指向父级；`page_id` 是本工具计算的「该块所在页面」。
- 数据库行是 `parent_table = 'collection'` 的 `page` 块，属性值在 `properties`，列定义在 `collection.schema`。
- 删除是软删除：`alive = 0`。
- 附件：`file_ref`（引用，`url_key` 为去掉签名参数的 URL）→ `file_blob`（按 sha256 去重的内容）。
- `page_sync_state` 记录每页上次同步的版本；`sync_run` 记录每次同步的统计。

查询示例：

```sql
-- 某页面所有块
SELECT type, json_extract(properties, '$.title') FROM block WHERE page_id = ? AND alive = 1;
-- 各类型块数量
SELECT type, count(*) FROM block WHERE alive = 1 GROUP BY type ORDER BY 2 DESC;
-- 某数据库的全部行
SELECT id, properties FROM block WHERE parent_table = 'collection' AND parent_id = ? AND alive = 1;
```

## 增量同步如何工作

- 每次都会从 `getSpaces` / 侧边栏 / 团队空间 / 搜索出发重新发现页面，对每个页面先取它当前的 `version` 和 `last_edited_time`，与上次比较：
  - 未变：不重新加载，沿用上次记录的子页面和内嵌数据库继续往下走；
  - 变了：`loadPageChunk` 重新加载整页，不在新块树里的旧块标记为 `alive = 0`。
- 数据库每次都会重新查询行列表（行增减不会改变父页面的版本），每一行再单独做上面的版本判断。
- 本次没走到的已备份页面会单独核对：Notion 中已删除/无权限 → 软删除；仍存在（如在回收站页面之下）→ 标记 `unreachable`，保留备份。
- 每个页面一个事务，同步中断后重跑即可继续；失败的附件下次会重试（最多 3 次）。

## 开发

```bash
uv run pytest
```

## 许可证

[Apache-2.0](LICENSE)
