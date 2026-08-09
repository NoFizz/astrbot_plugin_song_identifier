import httpx
import pytest

from astrbot_plugin_song_identifier.main import SongEnricher, SongInfo

SEARCH_HIT = {
    "result": {
        "songs": [
            {
                "name": "言って。",
                "id": 487527980,
                "artists": [{"name": "ヨルシカ"}],
            }
        ],
        "songCount": 1,
    },
    "code": 200,
}


class FakeSearchResponse:
    status_code = 200
    content = b'{"result":{}}'

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass

    @property
    def text(self):
        return ""


@pytest.mark.asyncio
async def test_enrich_fills_fields(monkeypatch):
    """增强应填网易云 song_id/source=netease，封面来自详情接口，原对象不被修改。"""
    captured = {}

    async def fake_fetch_cover(self, client, song_id):
        captured["cover_song_id"] = song_id
        return "http://cover/1.jpg"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def get(self, url, params=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            return FakeSearchResponse(SEARCH_HIT)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(SongEnricher, "_fetch_cover", fake_fetch_cover)

    enricher = SongEnricher()
    original = SongInfo(title="言って。", artist="ヨルシカ", source="acrcloud")
    enriched = await enricher.enrich(original)

    assert enriched.song_id == "487527980"
    assert enriched.source == "netease"
    assert enriched.cover_url == "http://cover/1.jpg"
    assert original.song_id is None  # 不修改原对象
    assert captured["url"] == "https://music.163.com/api/search/get/web"
    assert captured["params"]["s"] == "言って。 ヨルシカ"
    assert captured["params"]["type"] == 1
    assert captured["cover_song_id"] == "487527980"


@pytest.mark.asyncio
async def test_enrich_no_result_returns_original(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def get(self, url, params=None, headers=None):
            return FakeSearchResponse({"result": {"songs": []}, "code": 200})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    original = SongInfo(title="晴天", source="acrcloud")
    enriched = await SongEnricher().enrich(original)
    assert enriched is original


@pytest.mark.asyncio
async def test_enrich_exception_returns_original(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def get(self, url, params=None, headers=None):
            raise httpx.HTTPError("boom")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    original = SongInfo(title="晴天", source="acrcloud")
    enriched = await SongEnricher().enrich(original)
    assert enriched is original


@pytest.mark.asyncio
async def test_enrich_cover_failure_keeps_song_id(monkeypatch):
    """封面获取失败不应阻塞增强：song_id/source 仍生效，封面留空。"""

    async def fake_fetch_cover(self, client, song_id):
        return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def get(self, url, params=None, headers=None):
            return FakeSearchResponse(SEARCH_HIT)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(SongEnricher, "_fetch_cover", fake_fetch_cover)

    original = SongInfo(title="言って。", artist="ヨルシカ", source="acrcloud")
    enriched = await SongEnricher().enrich(original)
    assert enriched.song_id == "487527980"
    assert enriched.source == "netease"
    assert enriched.cover_url is None


@pytest.mark.asyncio
async def test_fetch_cover_parses_pic_url(monkeypatch):
    """详情接口返回的 album.picUrl 应被提取为封面 URL。"""
    captured = {}

    class FakeDetailResponse:
        status_code = 200
        content = b""

        def json(self):
            return {
                "songs": [
                    {
                        "album": {
                            "picUrl": "https://p1.music.126.net/xxx.jpg",
                            "name": "夏草が邪魔をする",
                        }
                    }
                ],
                "code": 200,
            }

        def raise_for_status(self):
            pass

        @property
        def text(self):
            return ""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def get(self, url, params=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            return FakeDetailResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    enricher = SongEnricher()
    cover = await enricher._fetch_cover(FakeClient(), "487527980")
    assert cover == "https://p1.music.126.net/xxx.jpg"
    assert captured["url"] == "https://music.163.com/api/song/detail/"
    assert captured["params"]["ids"] == "[487527980]"
