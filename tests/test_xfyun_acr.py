"""讯飞 ACRCloud 引擎测试：官方 golden vector 与请求/响应契约。"""

import base64
import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from astrbot_plugin_song_identifier.engines.xfyun_acr import (
    XfyunAcrEngine,
    build_xfyun_authorization,
    build_xfyun_request_body,
    decode_xfyun_response,
)
from astrbot_plugin_song_identifier.media import MediaArtifact


def _xfyun_payload(inner: dict) -> dict:
    """构造讯飞 ACRCloud 外层响应（text 为 base64 内层 JSON）。"""
    return {
        "header": {"code": 0, "message": "success", "sid": "s1"},
        "payload": {
            "output_text": {
                "compress": "raw",
                "encoding": "utf8",
                "format": "json",
                "seq": "0",
                "status": "3",
                "text": base64.b64encode(json.dumps(inner).encode()).decode(),
            }
        },
    }


def test_build_authorization_matches_official_doc():
    """与官方文档给出的 authorization 示例一致（歌曲识别 ACRCloud API 鉴权说明）。"""
    api_key = "apikeyXXXXXXXXXXXXXXXXXXXXXXXXXX"
    api_secret = "apisecretXXXXXXXXXXXXXXXXXXXXXXX"
    host = "cn-east-1.api.xf-yun.com"
    path = "/v1/private/s9884ba49"
    date = "Thu, 31 Mar 2022 02:42:08 GMT"
    auth = build_xfyun_authorization(api_key, api_secret, host, path, date)
    assert (
        auth
        == "YXBpX2tleT0iYXBpa2V5WFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFgiLCBhbGdvcml0aG09ImhtYWMtc2hhMjU2IiwgaGVhZGVycz0iaG9zdCBkYXRlIHJlcXVlc3QtbGluZSIsIHNpZ25hdHVyZT0iMkdUVmN2Y0NQdDcyMWxnUUxseHhCNzVZS1lzb3RaMHM3TWh3WHJaTUNQdz0i"
    )


def test_build_request_body_music_mode():
    body = build_xfyun_request_body("APP", "music", b"FAKEMP3")
    assert body["parameter"]["acr_music"]["mode"] == "music"
    assert body["payload"]["data"]["encoding"] == "lame"
    assert base64.b64decode(body["payload"]["data"]["audio"]) == b"FAKEMP3"


def test_build_request_body_rejects_oversized_audio():
    from astrbot_plugin_song_identifier.models import ErrorKind, RecognitionError

    with pytest.raises(RecognitionError) as raised:
        build_xfyun_request_body("APP", "music", b"x" * 786433)  # > 1MiB Base64
    assert raised.value.kind is ErrorKind.INPUT_INVALID


def test_decode_response_music():
    song = decode_xfyun_response(
        _xfyun_payload(
            {"status": {"code": 0}, "metadata": {"music": [{"title": "T", "artists": [{"name": "A"}]}]}}
        ),
        mode="music",
    )
    assert song is not None
    assert song.title == "T"
    assert song.provider == "xfyun_acr"
    assert song.mode == "music"


def test_decode_response_humming():
    song = decode_xfyun_response(
        _xfyun_payload(
            {
                "status": {"code": 0},
                "metadata": {
                    "humming": [{"title": "晴天", "artists": [{"name": "周杰伦"}], "score": 0.96}]
                },
            }
        ),
        mode="humming",
    )
    assert song is not None
    assert song.title == "晴天"
    assert song.mode == "humming"
    assert song.score == 0.96


def _make_engine(server):
    engine = XfyunAcrEngine(app_id="APP", api_key="AK", api_secret="SK", mode="music")
    engine.base_url = f"http://127.0.0.1:{server.port}"
    return engine


@pytest.mark.asyncio
async def test_identify_posts_music_request(monkeypatch):
    received = {}

    async def handler(request):
        body = await request.json()
        received["mode"] = body["parameter"]["acr_music"]["mode"]
        received["app_id"] = body["header"]["app_id"]
        return web.json_response(
            _xfyun_payload(
                {"status": {"code": 0}, "metadata": {"music": [{"title": "T", "artists": [{"name": "A"}]}]}}
            )
        )

    app = web.Application()
    app.router.add_post("/v1/private/s29ebee0d", handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        engine = _make_engine(server)

        async def fake_to_mp3(source):
            path = Path(__file__).parent / "fake.mp3"
            path.write_bytes(b"FAKEMP3")
            return path

        monkeypatch.setattr(engine, "_to_mp3", fake_to_mp3)
        artifact = MediaArtifact(path=Path("fake.wav"), created_paths=())
        async with aiohttp.ClientSession() as session:
            song = await engine.identify(artifact, session, deadline=9999999999)
        assert song is not None and song.title == "T"
        assert received["mode"] == "music"
        assert received["app_id"] == "APP"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_to_mp3_writes_into_artifact_temp_dir(tmp_path, monkeypatch):
    """MP3 转码必须写入媒体工件所在目录（AstrBot temp），而非系统临时目录。"""
    from pathlib import Path

    from astrbot_plugin_song_identifier.engines.xfyun_acr import XfyunAcrEngine

    engine = XfyunAcrEngine(app_id="APP", api_key="AK", api_secret="SK", mode="music")
    calls = []

    class FakeProcess:
        returncode = 0

        async def wait(self):
            # 模拟真实 ffmpeg：写出输出文件
            output = Path(calls[-1][-1])
            output.write_bytes(b"MP3")
            return self.returncode

    async def fake_create(*args, **kwargs):
        calls.append(args)
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create)
    source = tmp_path / "normalized.wav"
    source.write_bytes(b"wav")

    out = await engine._to_mp3(source)

    assert out is not None
    assert out.parent == tmp_path
    assert out.suffix == ".mp3"
