"""ACRCloud 引擎测试：签名、multipart 请求字段与响应解析。"""

from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from astrbot_plugin_song_identifier.engines.acrcloud import (
    AcrcloudEngine,
    build_acrcloud_signature,
    parse_acrcloud_response,
)
from astrbot_plugin_song_identifier.media import MediaArtifact
from astrbot_plugin_song_identifier.models import ErrorKind, RecognitionError


def test_build_signature_matches_documented_algorithm():
    """签名必须与官方文档 V1 算法一致（docs.acrcloud.cn/api/identification-api.html）。"""
    import base64
    import hashlib
    import hmac

    sig = build_acrcloud_signature("AK", "SK", "1700000000")
    origin = "POST\n/v1/identify\nAK\naudio\n1\n1700000000"
    expected = base64.b64encode(
        hmac.new(b"SK", origin.encode(), hashlib.sha1).digest()
    ).decode()
    assert sig == expected


def test_parse_success_preserves_provider_metadata():
    song = parse_acrcloud_response(
        {
            "status": {"code": 0, "msg": "Success"},
            "metadata": {
                "music": [
                    {
                        "acrid": "a1",
                        "title": "晴天",
                        "artists": [{"name": "周杰伦"}],
                        "album": {"name": "叶惠美"},
                        "score": 100,
                    }
                ]
            },
        }
    )
    assert song is not None
    assert song.title == "晴天"
    assert song.artist == "周杰伦"
    assert song.album == "叶惠美"
    assert song.provider == "acrcloud"
    assert song.acrid == "a1"


def test_parse_no_result_returns_none():
    assert parse_acrcloud_response({"status": {"code": 1001}}) is None


def test_parse_classifies_auth_error():
    with pytest.raises(RecognitionError) as raised:
        parse_acrcloud_response({"status": {"code": 3014, "msg": "Invalid signature"}})
    assert raised.value.kind is ErrorKind.AUTH_FAILED


@pytest.mark.asyncio
async def test_identify_posts_correct_multipart_fields(tmp_path):
    received = {}
    audio_path = tmp_path / "fake_audio.wav"
    audio_path.write_bytes(b"FAKEAUDIO")

    async def handler(request):
        form = await request.post()
        received["access_key"] = form.get("access_key")
        received["signature_version"] = form.get("signature_version")
        received["data_type"] = form.get("data_type")
        received["sample_bytes"] = form.get("sample_bytes")
        sample = form.get("sample")
        received["sample_name"] = sample.filename if sample else None
        return web.json_response(
            {
                "status": {"code": 0, "msg": "Success"},
                "metadata": {"music": [{"title": "T", "artists": [{"name": "A"}]}]},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/identify", handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        engine = AcrcloudEngine(
            host=f"http://127.0.0.1:{server.port}", access_key="AK", access_secret="SK"
        )
        artifact = MediaArtifact(path=audio_path, created_paths=())
        async with aiohttp.ClientSession() as session:
            song = await engine.identify(artifact, session, deadline=9999999999)
        assert song is not None and song.title == "T"
        assert received["access_key"] == "AK"
        assert received["signature_version"] == "1"
        assert received["data_type"] == "audio"
        assert received["sample_bytes"] == str(len(b"FAKEAUDIO"))
        assert received["sample_name"].endswith(".wav")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_identify_passes_proxy_to_request(tmp_path, monkeypatch):
    """配置代理时 ACRCloud 请求必须携带 proxy 参数。"""
    import aiohttp
    import pytest
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from astrbot_plugin_song_identifier.media import MediaArtifact

    captured = {}

    async def handler(request):
        return web.json_response(
            {
                "status": {"code": 0, "msg": "Success"},
                "metadata": {"music": [{"title": "T", "artists": [{"name": "A"}]}]},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/identify", handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        engine = AcrcloudEngine(
            host=f"http://127.0.0.1:{server.port}",
            access_key="AK",
            access_secret="SK",
            proxy="http://127.0.0.1:7890",
        )
        artifact = MediaArtifact(path=tmp_path / "a.wav", created_paths=())
        (tmp_path / "a.wav").write_bytes(b"FAKE")

        class _RealPost:
            """用真实 aiohttp 发请求，仅拦截 proxy 参数（引擎用 async with session.post）。"""

            def __init__(self, url, kwargs):
                self._url = url
                self._kwargs = kwargs

            async def __aenter__(self):
                self._session = aiohttp.ClientSession()
                self._response = await self._session.post(self._url, **self._kwargs)
                return self._response

            async def __aexit__(self, *args):
                await self._response.release()
                await self._session.close()

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def post(self, url, **kwargs):
                captured["proxy"] = kwargs.get("proxy")
                return _RealPost(
                    url, {k: v for k, v in kwargs.items() if k != "proxy"}
                )

        async with aiohttp.ClientSession() as session:
            song = await engine.identify(artifact, _Session(), deadline=9999999999)
        assert song is not None
        assert captured["proxy"] == "http://127.0.0.1:7890"
    finally:
        await client.close()

