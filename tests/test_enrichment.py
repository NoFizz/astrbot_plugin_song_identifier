"""增强层隔离测试：网易云/QQ 搜索不得污染识别核心结果。"""

import pytest
from astrbot_plugin_song_identifier.enrichment import EnrichedSong, SongEnricher
from astrbot_plugin_song_identifier.models import SongInfo


class _FakeClient:
    """httpx.AsyncClient 的响应替身，支持按 URL 路由不同响应。"""

    def __init__(self, status_code=200, payload=None, urls=None):
        self._status_code = status_code
        self._payload = payload
        self._urls = urls or {}

    async def get(self, url, params=None, headers=None):
        if url in self._urls:
            return _FakeResponse(self._status_code, self._urls[url])
        return _FakeResponse(self._status_code, self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _song():
    return SongInfo(
        title="花の塔",
        artist="さユり",
        album="花の塔",
        provider="acrcloud",
        mode="music",
    )


def _netease_hit():
    return {
        "result": {
            "songs": [{"id": 123456, "name": "花の塔", "artists": [{"name": "さユり"}]}]
        }
    }


@pytest.mark.asyncio
async def test_enrich_preserves_provider_fields(monkeypatch):
    song = _song()
    enricher = SongEnricher()
    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.enrichment.httpx.AsyncClient",
        lambda **kw: _FakeClient(payload=_netease_hit()),
    )

    enriched = await enricher.enrich(song)

    # 识别核心字段必须原样保留
    assert enriched.song is song
    assert enriched.song.provider == "acrcloud"
    assert enriched.song.mode == "music"
    assert enriched.song.title == "花の塔"
    assert enriched.song.artist == "さユり"
    # 增强 ID 只出现在独立字段
    assert enriched.netease_id == "123456"
    assert enriched.song.netease_id is None


@pytest.mark.asyncio
async def test_enrich_network_failure_returns_empty_enrichment(monkeypatch):
    song = _song()
    enricher = SongEnricher()

    async def _boom(self, url, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.enrichment.httpx.AsyncClient", _boom
    )

    enriched = await enricher.enrich(song)

    assert enriched.song is song
    assert enriched.netease_id is None
    assert enriched.qq_songmid is None
    assert enriched.cover_url is None


@pytest.mark.asyncio
async def test_enrich_no_hit_keeps_song_unchanged(monkeypatch):
    song = _song()
    enricher = SongEnricher()
    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.enrichment.httpx.AsyncClient",
        lambda **kw: _FakeClient(payload={"result": {"songs": []}}),
    )

    enriched = await enricher.enrich(song)

    assert enriched.song is song
    assert enriched.netease_id is None


def test_enriched_song_builds_platform_links():
    enriched = EnrichedSong(song=_song(), netease_id="123", qq_songmid="qq123")

    assert enriched.netease_url == "https://music.163.com/song/123"
    assert enriched.qq_url == "https://y.qq.com/n/ryqq/songDetail/qq123"


def test_enriched_song_without_ids_has_no_links():
    enriched = EnrichedSong(song=_song())

    assert enriched.netease_url is None
    assert enriched.qq_url is None


def _qq_hit(songname="花の塔", songmid="003abc", singers=None):
    hit = {"songname": songname, "songmid": songmid}
    if singers is not None:
        hit["singer"] = [{"name": s} for s in singers]
    return {"data": {"song": {"list": [hit]}}}


@pytest.mark.asyncio
async def test_enrich_fills_qq_songmid_when_title_matches(monkeypatch):
    from astrbot_plugin_song_identifier.enrichment import _QQ_SEARCH_URL

    song = _song()
    enricher = SongEnricher()
    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.enrichment.httpx.AsyncClient",
        lambda **kw: _FakeClient(
            payload=_netease_hit(),
            urls={_QQ_SEARCH_URL: _qq_hit()},
        ),
    )

    enriched = await enricher.enrich(song)

    assert enriched.qq_songmid == "003abc"
    assert enriched.netease_id == "123456"


@pytest.mark.asyncio
async def test_enrich_skips_qq_when_title_mismatch(monkeypatch):
    from astrbot_plugin_song_identifier.enrichment import _QQ_SEARCH_URL

    song = _song()
    enricher = SongEnricher()
    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.enrichment.httpx.AsyncClient",
        lambda **kw: _FakeClient(
            payload=_netease_hit(),
            urls={_QQ_SEARCH_URL: _qq_hit(songname="完全不同的歌")},
        ),
    )

    enriched = await enricher.enrich(song)

    # 标题不匹配：QQ songmid 为空，但网易云增强不受影响
    assert enriched.qq_songmid is None
    assert enriched.netease_id == "123456"


@pytest.mark.asyncio
async def test_enrich_skips_qq_when_artist_mismatch(monkeypatch):
    from astrbot_plugin_song_identifier.enrichment import _QQ_SEARCH_URL

    song = _song()
    enricher = SongEnricher()
    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.enrichment.httpx.AsyncClient",
        lambda **kw: _FakeClient(
            payload=_netease_hit(),
            urls={_QQ_SEARCH_URL: _qq_hit(singers=["别人"])},
        ),
    )

    enriched = await enricher.enrich(song)

    # 歌手不匹配：QQ songmid 为空，但网易云增强不受影响
    assert enriched.qq_songmid is None
    assert enriched.netease_id == "123456"


@pytest.mark.asyncio
async def test_enrich_accepts_qq_when_artist_matches(monkeypatch):
    from astrbot_plugin_song_identifier.enrichment import _QQ_SEARCH_URL

    song = _song()
    enricher = SongEnricher()
    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.enrichment.httpx.AsyncClient",
        lambda **kw: _FakeClient(
            payload=_netease_hit(),
            urls={_QQ_SEARCH_URL: _qq_hit(singers=["さユり"])},
        ),
    )

    enriched = await enricher.enrich(song)

    assert enriched.qq_songmid == "003abc"
