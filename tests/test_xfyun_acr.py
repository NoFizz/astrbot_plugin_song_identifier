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
    parse_xfyun_acr_response,
)


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


def test_parse_success():
    inner = {
        "status": {"code": 0, "msg": "Success", "version": "1.0"},
        "metadata": {
            "music": [
                {
                    "title": "光的方向",
                    "artists": [{"name": "张碧晨"}],
                    "album": {"name": "光的方向"},
                }
            ]
        },
    }
    payload = {
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
    info = parse_xfyun_acr_response(payload)
    assert info.title == "光的方向"
    assert info.artist == "张碧晨"
    assert info.source == "xfyun"


def test_parse_header_error():
    payload = {"header": {"code": 10107, "message": "error", "sid": "s1"}}
    assert parse_xfyun_acr_response(payload) is None


def test_parse_empty_text():
    payload = {"header": {"code": 0}, "payload": {"output_text": {"text": ""}}}
    assert parse_xfyun_acr_response(payload) is None


@pytest.mark.asyncio
async def test_identify_posts_json_with_mp3(monkeypatch):
    # 模拟 ffmpeg 转换：直接生成一个假 mp3 文件
    fake_mp3_path = pathlib.Path(tempfile.gettempdir()) / "xfyun_fake_out.mp3"

    async def fake_to_mp3(wav_path):
        fake_mp3_path.write_bytes(b"FAKEMP3")
        return str(fake_mp3_path)

    received = {}

    async def handler(request):
        body = await request.json()
        received["app_id"] = body["header"]["app_id"]
        received["mode"] = body["parameter"]["acr_music"]["mode"]
        received["encoding"] = body["payload"]["data"]["encoding"]
        audio = body["payload"]["data"]["audio"]
        received["audio_decoded"] = base64.b64decode(audio).decode()
        return web.json_response(
            {
                "header": {"code": 0, "message": "success", "sid": "s1"},
                "payload": {
                    "output_text": {
                        "compress": "raw",
                        "encoding": "utf8",
                        "format": "json",
                        "seq": "0",
                        "status": "3",
                        "text": base64.b64encode(
                            json.dumps(
                                {
                                    "status": {"code": 0, "msg": "Success"},
                                    "metadata": {
                                        "music": [
                                            {"title": "T", "artists": [{"name": "A"}]}
                                        ]
                                    },
                                }
                            ).encode()
                        ).decode(),
                    }
                },
            }
        )

    app = web.Application()
    app.router.add_post("/v1/private/s29ebee0d", handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        engine = XfyunAcrEngine(app_id="APP", api_key="AK", api_secret="SK")
        engine.host = f"http://127.0.0.1:{server.port}"
        monkeypatch.setattr(engine, "_to_mp3", fake_to_mp3)
        tmp = pathlib.Path(tempfile.gettempdir()) / "fake_in.wav"
        tmp.write_bytes(b"FAKEWAV")
        async with aiohttp.ClientSession() as session:
            info = await engine.identify(str(tmp), session)
        assert info is not None and info.title == "T"
        assert received["app_id"] == "APP"
        assert received["mode"] == "music"
        assert received["encoding"] == "lame"
        assert received["audio_decoded"] == "FAKEMP3"
        assert not fake_mp3_path.exists()  # mp3 临时文件上传后必须清理
    finally:
        await client.close()
