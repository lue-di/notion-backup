import json

import httpx
import pytest
import respx

import asyncio

from notion_backup.client import API_BASE, NotionAuthError, NotionClient, RateLimiter, iter_records, merge_record_maps


@pytest.fixture
async def client(monkeypatch):
    c = NotionClient("secret-token", "user-1", rate=1000, file_rate=1000)

    async def no_sleep(*a):
        pass

    monkeypatch.setattr(c, "_backoff", no_sleep)
    yield c
    await c.aclose()


def test_iter_records_accepts_both_shapes():
    rm = {
        "block": {
            "old": {"role": "editor", "value": {"id": "old", "type": "text", "space_id": "s1"}},
            "new": {"spaceId": "s2", "value": {"role": "reader", "value": {"id": "new", "type": "page"}}},
            "noaccess": {"role": "none"},
        },
        "__version__": 3,
    }
    recs = {r.id: r for r in iter_records(rm)}
    assert set(recs) == {"old", "new"}
    assert (recs["old"].role, recs["old"].space_id) == ("editor", "s1")
    assert (recs["new"].role, recs["new"].space_id, recs["new"].value["type"]) == ("reader", "s2", "page")


def test_merge_record_maps():
    dst = {"block": {"a": 1}}
    merge_record_maps(dst, {"block": {"b": 2}, "collection": {"c": 3}})
    assert dst == {"block": {"a": 1, "b": 2}, "collection": {"c": 3}}


@respx.mock
async def test_auth_headers_and_cookies(client):
    route = respx.post(API_BASE + "getSpaces").respond(json={"user-1": {"space": {"s": {"value": {"id": "s"}}}}})
    rm = await client.get_spaces()
    assert rm == {"space": {"s": {"value": {"id": "s"}}}}
    req = route.calls.last.request
    assert "token_v2=secret-token" in req.headers["cookie"]
    assert req.headers["x-notion-active-user-header"] == "user-1"


@respx.mock
async def test_load_page_chunk_follows_cursor(client):
    route = respx.post(API_BASE + "loadPageChunk").mock(
        side_effect=[
            httpx.Response(200, json={"recordMap": {"block": {"a": {}}}, "cursor": {"stack": [[{"id": "x"}]]}}),
            httpx.Response(200, json={"recordMap": {"block": {"b": {}}}, "cursor": {"stack": []}}),
        ]
    )
    rm = await client.load_page_chunk("page", "space")
    assert set(rm["block"]) == {"a", "b"}
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert [b["chunkNumber"] for b in bodies] == [0, 1]
    assert bodies[1]["cursor"] == {"stack": [[{"id": "x"}]]}


@respx.mock
async def test_load_page_chunk_falls_back_to_v2(client):
    respx.post(API_BASE + "loadPageChunk").respond(400, json={"errorId": "x"})
    v2 = respx.post(API_BASE + "loadCachedPageChunkV2").respond(json={"recordMap": {"block": {"a": {}}}, "cursor": {"stack": []}})
    rm = await client.load_page_chunk("page", "space")
    assert set(rm["block"]) == {"a"}
    assert json.loads(v2.calls.last.request.content)["page"] == {"table": "block", "id": "page", "spaceId": "space"}


@respx.mock
async def test_retries_on_429_then_succeeds(client):
    route = respx.post(API_BASE + "syncRecordValues").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(502), httpx.Response(200, json={"recordMap": {}})]
    )
    assert await client.sync_record_values([{"table": "block", "id": "a"}]) == {}
    assert route.call_count == 3


@respx.mock
async def test_401_is_auth_error(client):
    respx.post(API_BASE + "syncRecordValues").respond(401)
    with pytest.raises(NotionAuthError):
        await client.sync_record_values([{"table": "block", "id": "a"}])


@respx.mock
async def test_sync_record_values_batches(client):
    route = respx.post(API_BASE + "syncRecordValues").respond(json={"recordMap": {"block": {}}})
    await client.sync_record_values([{"table": "block", "id": str(i)} for i in range(250)])
    sizes = sorted(len(json.loads(c.request.content)["requests"]) for c in route.calls)
    assert sizes == [50, 100, 100]


@respx.mock
async def test_query_collection_overrides_view_filter(client):
    route = respx.post(API_BASE + "queryCollection").respond(
        json={
            "result": {"reducerResults": {"collection_group_results": {"blockIds": ["r1", "r2"], "hasMore": False}}},
            "recordMap": {"block": {"r1": {}}},
        }
    )
    ids, rm = await client.query_collection("coll", "view", "space")
    assert ids == ["r1", "r2"]
    body = json.loads(route.calls.last.request.content)
    assert body["source"] == {"type": "collection", "id": "coll", "spaceId": "space"}
    assert body["loader"]["filter"] == {"operator": "and", "filters": []}


@respx.mock
async def test_download_respects_size_limit(client):
    from notion_backup.client import FileTooLarge

    respx.get("https://files.example/big").respond(200, content=b"x" * 2000)
    respx.get("https://files.example/ok").respond(200, content=b"hello", headers={"Content-Type": "text/plain; charset=utf-8"})
    with pytest.raises(FileTooLarge):
        await client.download("https://files.example/big", 1000)
    assert await client.download("https://files.example/ok", 1000) == (b"hello", "text/plain")


@respx.mock
async def test_token_not_sent_to_third_party_hosts(client):
    route = respx.get("https://prod-files-secure.s3.us-west-2.amazonaws.com/f").respond(200, content=b"x")
    await client.download("https://prod-files-secure.s3.us-west-2.amazonaws.com/f", 100)
    assert "token_v2" not in route.calls.last.request.headers.get("cookie", "")


async def test_rate_limiter_spaces_requests():
    limiter = RateLimiter(rate=20)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.gather(*(limiter.wait() for _ in range(5)))
    assert loop.time() - start >= 4 / 20 - 0.01


async def test_429_pauses_every_request_and_slows_down():
    limiter = RateLimiter(rate=4)
    loop = asyncio.get_running_loop()
    limiter.throttled()
    assert limiter.rate == 2
    limiter.throttled()  # in-flight failures from the same burst count once
    assert limiter.rate == 2
    start = loop.time()
    await limiter.wait()
    assert loop.time() - start >= 0.99  # first pause: 1s
    limiter._last_cut -= 10
    limiter.throttled()  # a later 429: halve again, pause doubles
    assert limiter.rate == 1 and limiter._paused_until - loop.time() > 1.9
    for _ in range(1000):
        limiter.succeeded()
    assert limiter.rate == 4 and limiter._strikes == 0


@respx.mock
async def test_429_slows_down_the_shared_limiter(client, monkeypatch):
    respx.post(API_BASE + "syncRecordValues").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"recordMap": {}})]
    )
    pauses = []

    async def no_sleep(_):
        pass

    monkeypatch.setattr(client, "_backoff", NotionClient._backoff.__get__(client))  # undo the fixture's stub
    monkeypatch.setattr(client._api_limiter, "throttled", pauses.append)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    await client.sync_record_values([{"table": "block", "id": "a"}])
    assert len(pauses) == 1


@respx.mock
async def test_file_token_is_sent_to_file_notion_so():
    c = NotionClient("tok", "user-1", "ftok", rate=1000)
    route = respx.get("https://file.notion.so/f/f/x/y.png").respond(200, content=b"img")
    await c.download("https://file.notion.so/f/f/x/y.png", 100)
    assert "file_token=ftok" in route.calls.last.request.headers["cookie"]
    assert c.has_file_token
    await c.aclose()


@respx.mock
async def test_sign_isolates_invalid_url(client):
    def sign(request):
        urls = [u["url"] for u in json.loads(request.content)["urls"]]
        if "bad" in urls:
            return httpx.Response(400, json={"debugMessage": "Invalid secure file URL."})
        return httpx.Response(200, json={"signedUrls": [f"https://file.notion.so/f/{u}" for u in urls]})

    respx.post(API_BASE + "getSignedFileUrls").mock(side_effect=sign)
    signed = await client.get_signed_file_urls([(u, "b", "s") for u in ("a", "bad", "c", "d")])
    assert signed == ["https://file.notion.so/f/a", None, "https://file.notion.so/f/c", "https://file.notion.so/f/d"]


def test_signed_proxy_url_has_user_and_cache():
    c = NotionClient("tok", "user-1")
    url = c.signed_proxy_url("attachment:abc:pic.png", "blk", "sp")
    assert url == "https://www.notion.so/signed/attachment%3Aabc%3Apic.png?table=block&id=blk&spaceId=sp&userId=user-1&cache=v2"


@respx.mock
async def test_successful_downloads_restore_the_rate(monkeypatch):
    c = NotionClient("tok", file_rate=4)
    respx.get("https://file.notion.so/f/x").respond(200, content=b"x")

    async def no_wait():
        pass

    monkeypatch.setattr(c._file_limiter, "wait", no_wait)
    c._file_limiter.rate = c._file_limiter.min_rate
    for _ in range(60):
        await c.download("https://file.notion.so/f/x", 10)
    assert c._file_limiter.rate == 4
    await c.aclose()


@respx.mock
async def test_download_gives_up_when_blocked_too_long(monkeypatch):
    from notion_backup.client import RateLimitExhausted

    c = NotionClient("tok", file_rate=1000, file_block_timeout=0)

    async def no_wait():
        pass

    monkeypatch.setattr(c._file_limiter, "wait", no_wait)
    route = respx.get("https://file.notion.so/f/x").respond(429)
    with pytest.raises(RateLimitExhausted):
        await c.download("https://file.notion.so/f/x", 10)
    assert route.call_count == 2  # first 429 starts the clock, the next one gives up
    await c.aclose()
