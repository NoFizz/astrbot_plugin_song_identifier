import httpx
import pytest

from astrbot_plugin_song_identifier.main import (
    KuwoCardProvider,
    KugouCardProvider,
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


@pytest.mark.asyncio
async def test_kugou_provider_builds_custom_card(monkeypatch):
    payload = {
        "status": 1,
        "data": {
            "info": [
                {
                    "hash": "1be0405a2b95e2486f510fb369371527",
                    "songname": "言って。",
                    "singername": "ヨルシカ",
                    "trans_param": {
                        "union_cover": "http://imge.kugou.com/stdmusic/{size}/20190306/1.jpg"
                    },
                }
            ]
        },
    }
    monkeypatch.setattr(httpx, "AsyncClient", make_fake_client(payload))
    provider = KugouCardProvider()
    song = SongInfo(
        title="言って。", artist="ヨルシカ", song_id="487527980", source="netease"
    )
    segment = await provider.build_music_segment(song)
    data = segment["data"]
    assert segment["type"] == "music"
    assert data["type"] == "custom"
    assert (
        data["url"] == "https://www.kugou.com/song/#hash=1be0405a2b95e2486f510fb369371527"
    )
    assert data["title"] == "言って。"
    assert data["singer"] == "ヨルシカ"
    assert "400" in data["image"]
    assert data["audio"] == "https://music.163.com/song/media/outer/url?id=487527980.mp3"


@pytest.mark.asyncio
async def test_kuwo_provider_builds_custom_card(monkeypatch):
    payload = {
        "abslist": [
            {
                "MUSICRID": "MUSIC_389766634",
                "NAME": "言って。",
                "ARTIST": "ヨルシカ",
                "web_albumpic_short": "120/s4s32/63/493417087.jpg",
            }
        ]
    }
    monkeypatch.setattr(httpx, "AsyncClient", make_fake_client(payload))
    provider = KuwoCardProvider()
    song = SongInfo(
        title="言って。", artist="ヨルシカ", song_id="487527980", source="netease"
    )
    segment = await provider.build_music_segment(song)
    data = segment["data"]
    assert segment["type"] == "music"
    assert data["type"] == "custom"
    assert data["url"] == "https://www.kuwo.cn/play_detail/389766634"
    assert data["title"] == "言って。"
    assert data["singer"] == "ヨルシカ"
    assert "img1.kuwo.cn/star/albumcover/" in data["image"]
    assert data["audio"] == "https://music.163.com/song/media/outer/url?id=487527980.mp3"


@pytest.mark.asyncio
async def test_kugou_provider_no_result(monkeypatch):
    monkeypatch.setattr(
        httpx, "AsyncClient", make_fake_client({"status": 1, "data": {"info": []}})
    )
    provider = KugouCardProvider()
    song = SongInfo(title="晴天", artist="周杰伦", source="acrcloud")
    assert await provider.build_music_segment(song) is None
