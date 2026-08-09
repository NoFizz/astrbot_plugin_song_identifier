import pathlib
import tempfile

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from astrbot_plugin_song_identifier.main import (
    XfyunHummingEngine,
    build_qbh_headers,
    parse_qbh_response,
)


def test_build_headers_shape():
    headers = build_qbh_headers("APP", "KEY")
    assert set(headers) == {"X-Appid", "X-CurTime", "X-Param", "X-CheckSum"}
    assert headers["X-Appid"] == "APP"
    assert len(headers["X-CheckSum"]) == 32  # MD5 hex


def test_parse_success():
    payload = {
        "code": "0",
        "data": [
            {
                "song": "千里之外",
                "song_id": "6433782",
                "singer": "周杰伦",
                "singer_id": "313264",
                "start_time": 245,
                "end_time": 33340,
            },
            {
                "song": "千里之外",
                "song_id": "5233627",
                "singer": "刘芳",
                "singer_id": "347675",
                "start_time": 1200,
                "end_time": 16440,
            },
        ],
        "desc": "success",
        "sid": "wbh00000eff@ch676e0e61c4562a0100",
    }
    info = parse_qbh_response(payload)
    assert info.title == "千里之外"
    assert info.artist == "周杰伦"
    assert info.song_id == "6433782"
    assert info.source == "xfyun_humming"


def test_parse_error():
    payload = {"code": "10107", "data": [], "desc": "illegal parameter", "sid": "x"}
    assert parse_qbh_response(payload) is None


def test_parse_empty_data():
    payload = {"code": "0", "data": [], "desc": "success", "sid": "x"}
    assert parse_qbh_response(payload) is None


@pytest.mark.asyncio
async def test_identify_posts_audio_body(monkeypatch):
    # 模拟 ffmpeg 重采样：直接生成一个假 16k wav 文件
    fake_16k_path = pathlib.Path(tempfile.gettempdir()) / "qbh_fake_out.wav"

    async def fake_to_16k(wav_path):
        fake_16k_path.write_bytes(b"FAKE16K")
        return str(fake_16k_path)

    received = {}

    async def handler(request):
        received["body"] = await request.read()
        received["headers"] = dict(request.headers.items())
        return web.json_response(
            {
                "code": "0",
                "data": [{"song": "一次就好", "song_id": "1", "singer": "杨宗纬"}],
                "desc": "success",
                "sid": "s1",
            }
        )

    app = web.Application()
    app.router.add_post("/v1/service/v1/qbh", handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        engine = XfyunHummingEngine(app_id="APP", api_key="KEY")
        engine.url = f"http://127.0.0.1:{server.port}/v1/service/v1/qbh"
        monkeypatch.setattr(engine, "_to_16k_wav", fake_to_16k)
        tmp = pathlib.Path(tempfile.gettempdir()) / "fake_hum.wav"
        tmp.write_bytes(b"FAKEWAV")
        async with aiohttp.ClientSession() as session:
            info = await engine.identify(str(tmp), session)
        assert info is not None and info.title == "一次就好"
        assert received["body"] == b"FAKE16K"
        assert received["headers"]["X-Appid"] == "APP"
        assert "X-CheckSum" in received["headers"]
        assert "X-CurTime" in received["headers"]
        assert "X-Param" in received["headers"]
        assert not fake_16k_path.exists()  # 16k 临时文件上传后必须清理
    finally:
        await client.close()
