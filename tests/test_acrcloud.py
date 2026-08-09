import pathlib
import tempfile

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from astrbot_plugin_song_identifier.main import (
    AcrcloudEngine,
    build_acrcloud_signature,
    parse_acrcloud_response,
)


def test_build_signature_deterministic():
    sig1 = build_acrcloud_signature("AK", "SK", "1700000000")
    sig2 = build_acrcloud_signature("AK", "SK", "1700000000")
    assert sig1 == sig2
    assert len(sig1) > 10
    sig3 = build_acrcloud_signature("AK", "SK2", "1700000000")
    assert sig1 != sig3


def test_parse_success():
    payload = {
        "status": {"code": 0, "msg": "Success"},
        "metadata": {
            "music": [
                {
                    "title": "晴天",
                    "artists": [{"name": "周杰伦"}],
                    "album": {"name": "叶惠美"},
                }
            ]
        },
    }
    info = parse_acrcloud_response(payload)
    assert info.title == "晴天"
    assert info.artist == "周杰伦"
    assert info.album == "叶惠美"
    assert info.source == "acrcloud"


def test_parse_failure_code():
    payload = {"status": {"code": 1001, "msg": "No result"}}
    assert parse_acrcloud_response(payload) is None


def test_parse_empty_music():
    payload = {"status": {"code": 0, "msg": "Success"}, "metadata": {"music": []}}
    assert parse_acrcloud_response(payload) is None


@pytest.mark.asyncio
async def test_identify_posts_multipart():
    received = {}

    async def handler(request):
        form = await request.post()
        received["access_key"] = form.get("access_key")
        received["signature_version"] = form.get("signature_version")
        received["data_type"] = form.get("data_type")
        sample = form.get("sample")
        received["sample_bytes"] = form.get("sample_bytes")
        if sample is not None:
            received["sample_name"] = sample.filename
            received["sample_content"] = sample.file.read().decode()
        return web.json_response({
            "status": {"code": 0, "msg": "Success"},
            "metadata": {"music": [{"title": "T", "artists": [{"name": "A"}]}]},
        })

    app = web.Application()
    app.router.add_post("/v1/identify", handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        host = f"http://127.0.0.1:{server.port}"
        engine = AcrcloudEngine(host=host, access_key="AK", access_secret="SK")
        tmp = pathlib.Path(tempfile.gettempdir()) / "fake_audio.wav"
        tmp.write_bytes(b"FAKEAUDIO")
        async with aiohttp.ClientSession() as session:
            info = await engine.identify(str(tmp), session)
        assert info is not None and info.title == "T"
        assert received["access_key"] == "AK"
        assert received["signature_version"] == "1"
        assert received["data_type"] == "audio"
        assert received["sample_bytes"] == str(len(b"FAKEAUDIO"))
        assert received["sample_name"].endswith(".wav")
    finally:
        await client.close()
