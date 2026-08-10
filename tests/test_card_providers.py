import httpx
import pytest

from astrbot_plugin_song_identifier.main import (
    NeteaseCardProvider,
    QQMusicCardProvider,
    SongInfo,
)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass

    @property
    def content(self):
        return b"{}"

    @property
    def text(self):
        return ""


def make_fake_client(payload):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def get(self, url, params=None, headers=None):
            return FakeResponse(payload)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    return FakeClient


@pytest.mark.asyncio
async def test_netease_provider_with_song_id():
    """有网易云 song_id 时直接构造 163 卡片，无需网络请求。"""
    provider = NeteaseCardProvider()
    song = SongInfo(title="晴天", artist="周杰伦", song_id="487527980", source="netease")
    segment = await provider.build_music_segment(song)
    assert segment == {"type": "music", "data": {"type": "163", "id": "487527980"}}


@pytest.mark.asyncio
async def test_netease_provider_searches_when_no_id(monkeypatch):
    """无 song_id 时通过网易云搜索补 id。"""
    payload = {
        "result": {
            "songs": [{"name": "晴天", "id": 487527980, "artists": [{"name": "周杰伦"}]}]
        },
        "code": 200,
    }
    monkeypatch.setattr(httpx, "AsyncClient", make_fake_client(payload))
    provider = NeteaseCardProvider()
    song = SongInfo(title="晴天", artist="周杰伦", source="acrcloud")
    segment = await provider.build_music_segment(song)
    assert segment == {"type": "music", "data": {"type": "163", "id": "487527980"}}


@pytest.mark.asyncio
async def test_netease_provider_no_result(monkeypatch):
    monkeypatch.setattr(
        httpx, "AsyncClient", make_fake_client({"result": {"songs": []}, "code": 200})
    )
    provider = NeteaseCardProvider()
    song = SongInfo(title="晴天", artist="周杰伦", source="acrcloud")
    assert await provider.build_music_segment(song) is None


@pytest.mark.asyncio
async def test_qq_provider_builds_qq_card(monkeypatch):
    payload = {
        "code": 0,
        "data": {
            "song": {
                "list": [{"songmid": "0013qqfr11Bx68", "songname": "言って。"}]
            }
        },
    }
    monkeypatch.setattr(httpx, "AsyncClient", make_fake_client(payload))
    provider = QQMusicCardProvider()
    song = SongInfo(title="言って。", artist="ヨルシカ", source="acrcloud")
    segment = await provider.build_music_segment(song)
    assert segment == {"type": "music", "data": {"type": "qq", "id": "0013qqfr11Bx68"}}



