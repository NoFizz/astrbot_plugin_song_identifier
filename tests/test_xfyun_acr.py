import base64
import json
import pathlib
import tempfile

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from astrbot_plugin_song_identifier.main import (
    XfyunAcrEngine,
    build_xfyun_authorization,
)


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


def test_build_authorization_deterministic():
    # 官方文档示例输入（歌曲识别 ACRCloud API 鉴权说明）
    api_key = "apikeyXXXXXXXXXXXXXXXXXXXXXXXXXX"
    api_secret = "apisecretXXXXXXXXXXXXXXXXXXXXXXX"
    host = "cn-east-1.api.xf-yun.com"
    path = "/v1/private/s9884ba49"
    date = "Thu, 31 Mar 2022 02:42:08 GMT"
    auth1 = build_xfyun_authorization(api_key, api_secret, host, path, date)
    auth2 = build_xfyun_authorization(api_key, api_secret, host, path, date)
    assert auth1 == auth2
    # 与官方文档示例一致（文档给出的 authorization 示例）
    assert (
        auth1
        == "YXBpX2tleT0iYXBpa2V5WFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFgiLCBhbGdvcml0aG09ImhtYWMtc2hhMjU2IiwgaGVhZGVycz0iaG9zdCBkYXRlIHJlcXVlc3QtbGluZSIsIHNpZ25hdHVyZT0iMkdUVmN2Y0NQdDcyMWxnUUxseHhCNzVZS1lzb3RaMHM3TWh3WHJaTUNQdz0i"
    )


def _make_engine(server, monkeypatch):
    fake_mp3_path = pathlib.Path(tempfile.gettempdir()) / "xfyun_fake_out.mp3"

    async def fake_to_mp3(wav_path):
        fake_mp3_path.write_bytes(b"FAKEMP3")
        return str(fake_mp3_path)

    engine = XfyunAcrEngine(app_id="APP", api_key="AK", api_secret="SK")
    engine.host = f"http://127.0.0.1:{server.port}"
    monkeypatch.setattr(engine, "_to_mp3", fake_to_mp3)
    tmp = pathlib.Path(tempfile.gettempdir()) / "fake_in.wav"
    tmp.write_bytes(b"FAKEWAV")
    return engine, tmp, fake_mp3_path


@pytest.mark.asyncio
async def test_identify_posts_json_with_mp3(monkeypatch):
    """原声识别：请求走 acr_music + music 端点，成功返回结果并清理 mp3。"""
    received = {}

    async def handler(request):
        body = await request.json()
        received["app_id"] = body["header"]["app_id"]
        received["mode"] = body["parameter"]["acr_music"]["mode"]
        received["encoding"] = body["payload"]["data"]["encoding"]
        audio = body["payload"]["data"]["audio"]
        received["audio_decoded"] = base64.b64decode(audio).decode()
        return web.json_response(
            _xfyun_payload(
                {
                    "status": {"code": 0, "msg": "Success"},
                    "metadata": {"music": [{"title": "T", "artists": [{"name": "A"}]}]},
                }
            )
        )

    app = web.Application()
    app.router.add_post("/v1/private/s29ebee0d", handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        engine, tmp, fake_mp3_path = _make_engine(server, monkeypatch)
        async with aiohttp.ClientSession() as session:
            info = await engine.identify(str(tmp), session)
        assert info is not None and info.title == "T"
        assert info.source == "xfyun"
        assert received["app_id"] == "APP"
        assert received["mode"] == "music"
        assert received["encoding"] == "lame"
        assert received["audio_decoded"] == "FAKEMP3"
        assert not fake_mp3_path.exists()  # mp3 临时文件上传后必须清理
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_identify_falls_back_to_humming(monkeypatch):
    """原声识别无结果 → 自动请求哼唱端点（s9884ba49, acr_humming）并返回哼唱结果。"""
    calls = []

    async def music_handler(request):
        calls.append("music")
        body = await request.json()
        assert body["parameter"]["acr_music"]["mode"] == "music"
        # 原声无结果（metadata.music 为空）
        return web.json_response(
            _xfyun_payload(
                {"status": {"code": 0, "msg": "Success"}, "metadata": {"music": []}}
            )
        )

    async def humming_handler(request):
        calls.append("humming")
        body = await request.json()
        assert body["parameter"]["acr_humming"]["mode"] == "humming"
        return web.json_response(
            _xfyun_payload(
                {
                    "status": {"code": 0, "msg": "Success"},
                    "metadata": {
                        "humming": [
                            {
                                "title": "最长的电影",
                                "artists": [{"name": "周杰伦"}],
                                "album": {"name": "我很忙"},
                                "score": 0.96,
                            }
                        ]
                    },
                }
            )
        )

    app = web.Application()
    app.router.add_post("/v1/private/s29ebee0d", music_handler)
    app.router.add_post("/v1/private/s9884ba49", humming_handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        engine, tmp, _ = _make_engine(server, monkeypatch)
        async with aiohttp.ClientSession() as session:
            info = await engine.identify(str(tmp), session)
        assert calls == ["music", "humming"]
        assert info is not None
        assert info.title == "最长的电影"
        assert info.artist == "周杰伦"
        assert info.album == "我很忙"
        assert info.source == "xfyun"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_identify_both_modes_no_result(monkeypatch):
    """原声与哼唱均无结果 → 返回 None（交给下一引擎）。"""
    async def handler(request):
        return web.json_response(
            _xfyun_payload(
                {"status": {"code": 0, "msg": "Success"}, "metadata": {}}
            )
        )

    app = web.Application()
    app.router.add_post("/v1/private/s29ebee0d", handler)
    app.router.add_post("/v1/private/s9884ba49", handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        engine, tmp, _ = _make_engine(server, monkeypatch)
        async with aiohttp.ClientSession() as session:
            info = await engine.identify(str(tmp), session)
        assert info is None
    finally:
        await client.close()
