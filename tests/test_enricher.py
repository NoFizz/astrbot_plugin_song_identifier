import httpx
import pytest

from astrbot_plugin_song_identifier.main import SongEnricher, SongInfo


@pytest.mark.asyncio
async def test_enrich_fills_fields(monkeypatch):
    class FakeResponse:
        def json(self):
            return {
                "data": [
                    {
                        "songid": "001",
                        "title": "晴天",
                        "author": "周杰伦",
                        "url": "http://audio/1.mp3",
                        "pic": "http://cover/1.jpg",
                    }
                ]
            }

        def raise_for_status(self):
            pass

    captured = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def post(self, url, data=None, headers=None):
            captured["url"] = url
            captured["data"] = data
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    enricher = SongEnricher(platform="qq")
    original = SongInfo(title="晴天", artist="周杰伦", source="acrcloud")
    enriched = await enricher.enrich(original)

    assert enriched.cover_url == "http://cover/1.jpg"
    assert enriched.audio_url == "http://audio/1.mp3"
    assert enriched.song_id == "001"
    assert original.cover_url is None
    assert captured["url"] == "https://music.txqq.pro/"
    assert captured["data"]["input"] == "晴天 周杰伦"
    assert captured["data"]["type"] == "qq"


@pytest.mark.asyncio
async def test_enrich_no_result_returns_original(monkeypatch):
    class FakeResponse:
        def json(self):
            return {"data": []}

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def post(self, url, data=None, headers=None):
            return FakeResponse()

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

        async def post(self, url, data=None, headers=None):
            raise httpx.HTTPError("boom")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    original = SongInfo(title="晴天", source="acrcloud")
    enriched = await SongEnricher().enrich(original)
    assert enriched is original
