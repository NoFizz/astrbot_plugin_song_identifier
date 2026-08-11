import asyncio
import base64
import gzip
import hashlib
import json
import time
from pathlib import Path

import pytest
from astrbot_plugin_song_identifier.engines.acrcloud import parse_acrcloud_response
from astrbot_plugin_song_identifier.engines.shazam import (
    ShazamEngine,
    parse_shazam_response,
)
from astrbot_plugin_song_identifier.engines.xfyun_acr import (
    build_xfyun_request_body,
    decode_xfyun_response,
)
from astrbot_plugin_song_identifier.engines.xfyun_qbh import (
    build_qbh_headers,
    parse_qbh_response,
)
from astrbot_plugin_song_identifier.media import MediaArtifact
from astrbot_plugin_song_identifier.models import ErrorKind, RecognitionError


def test_acrcloud_parser_preserves_provider_metadata():
    song = parse_acrcloud_response(
        {
            "status": {"code": 0, "msg": "Success"},
            "metadata": {
                "music": [
                    {
                        "acrid": "acr-1",
                        "title": "花の塔",
                        "artists": [{"name": "さユり"}],
                        "album": {"name": "花の塔"},
                        "score": 100,
                    }
                ]
            },
        }
    )

    assert song is not None
    assert song.provider == "acrcloud"
    assert song.mode == "music"
    assert song.acrid == "acr-1"
    assert song.score == 100


def test_acrcloud_parser_returns_none_for_no_match():
    assert parse_acrcloud_response({"status": {"code": 1001}}) is None


def test_acrcloud_parser_classifies_auth_failure():
    with pytest.raises(RecognitionError) as raised:
        parse_acrcloud_response({"status": {"code": 3014, "msg": "Invalid signature"}})

    assert raised.value.kind is ErrorKind.AUTH_FAILED
    assert raised.value.code == 3014
    assert "Invalid signature" in str(raised.value)


def test_acrcloud_parser_rejects_non_mapping_payload():
    with pytest.raises(RecognitionError) as raised:
        parse_acrcloud_response(json.loads("[]"))

    assert raised.value.kind is ErrorKind.PROTOCOL_ERROR


def _xfyun_payload(inner: dict, *, compress: str = "raw") -> dict:
    data = json.dumps(inner).encode()
    if compress == "gzip":
        data = gzip.compress(data)
    return {
        "header": {"code": 0, "message": "success", "sid": "sid-1"},
        "payload": {
            "output_text": {
                "compress": compress,
                "encoding": "utf8",
                "format": "json",
                "text": base64.b64encode(data).decode(),
            }
        },
    }


def test_xfyun_decoder_supports_gzip_humming_response():
    song = decode_xfyun_response(
        _xfyun_payload(
            {
                "status": {"code": 0, "msg": "Success"},
                "metadata": {
                    "humming": [
                        {
                            "title": "晴天",
                            "artists": [{"name": "周杰伦"}],
                            "score": 0.96,
                        }
                    ]
                },
            },
            compress="gzip",
        ),
        mode="humming",
    )

    assert song is not None
    assert song.provider == "xfyun_acr"
    assert song.mode == "humming"
    assert song.score == 0.96
    assert song.provider_sid == "sid-1"


def test_xfyun_decoder_classifies_outer_auth_error():
    with pytest.raises(RecognitionError) as raised:
        decode_xfyun_response(
            {"header": {"code": 10005, "message": "invalid app_id"}}, mode="music"
        )

    assert raised.value.kind is ErrorKind.AUTH_FAILED


def test_xfyun_decoder_classifies_invalid_base64():
    payload = _xfyun_payload({"status": {"code": 0}})
    payload["payload"]["output_text"]["text"] = "%%%"

    with pytest.raises(RecognitionError) as raised:
        decode_xfyun_response(payload, mode="music")

    assert raised.value.kind is ErrorKind.PROTOCOL_ERROR


def test_xfyun_request_body_keeps_mode_service_and_encoding_consistent():
    body = build_xfyun_request_body("APP", "humming", b"mp3")

    assert body["header"] == {"app_id": "APP", "status": 3}
    assert body["parameter"]["acr_humming"]["mode"] == "humming"
    assert body["payload"]["data"]["encoding"] == "lame"
    assert base64.b64decode(body["payload"]["data"]["audio"]) == b"mp3"


def test_xfyun_request_body_rejects_base64_over_conservative_limit():
    with pytest.raises(RecognitionError) as raised:
        build_xfyun_request_body("APP", "music", b"x" * 786433)

    assert raised.value.kind is ErrorKind.INPUT_INVALID


def test_qbh_headers_include_humming_engine_type():
    headers = build_qbh_headers("APP", "KEY", timestamp="1700000000")
    params = json.loads(base64.b64decode(headers["X-Param"]))

    assert params["engine_type"] == "afs"
    assert params["aue"] == "raw"
    assert headers["X-CurTime"] == "1700000000"


def test_qbh_parser_preserves_provider_and_song_id():
    song = parse_qbh_response(
        {
            "code": "0",
            "sid": "sid-1",
            "data": [{"song": "千里之外", "singer": "周杰伦", "song_id": "643"}],
        }
    )

    assert song is not None
    assert song.provider == "xfyun_qbh"
    assert song.mode == "humming"
    assert song.acrid == "643"


def test_qbh_parser_classifies_input_error():
    with pytest.raises(RecognitionError) as raised:
        parse_qbh_response({"code": "10107", "desc": "illegal parameter"})

    assert raised.value.kind is ErrorKind.INPUT_INVALID


def test_qbh_headers_param_base64_golden():
    """X-Param golden 值：standard 与 urlsafe Base64 对当前固定参数等价。

    官方文档文字写 MIME Base64，官方 Python demo 用 urlsafe_b64encode；
    当前参数集的 Base64 恰好不含 + / 字符，两种编码输出一致。若将来修改
    params（如新增字段），必须先验证两种编码是否一致，防止鉴权静默失败。
    """
    headers = build_qbh_headers("APP", "KEY", timestamp="1700000000")

    assert (
        headers["X-Param"]
        == "eyJlbmdpbmVfdHlwZSI6ImFmcyIsImF1ZSI6InJhdyIsInNhbXBsZV9yYXRlIjoiMTYwMDAifQ=="
    )
    assert headers["X-CheckSum"] == hashlib.md5(
        b"KEY1700000000"
        + b"eyJlbmdpbmVfdHlwZSI6ImFmcyIsImF1ZSI6InJhdyIsInNhbXBsZV9yYXRlIjoiMTYwMDAifQ=="
    ).hexdigest()


def test_shazam_parser_uses_matches_to_detect_no_result():
    assert parse_shazam_response({"matches": []}) is None


def test_shazam_parser_preserves_provider_metadata():
    song = parse_shazam_response(
        {
            "matches": [{"id": "1"}],
            "track": {"key": "123", "title": "花の塔", "subtitle": "さユり"},
        }
    )

    assert song is not None
    assert song.provider == "shazam"
    assert song.mode == "music"
    assert song.acrid == "123"


@pytest.mark.asyncio
async def test_shazam_engine_enforces_deadline(monkeypatch, tmp_path):
    class SlowShazam:
        async def recognize(self, path):
            await asyncio.sleep(1)

    monkeypatch.setattr("shazamio.Shazam", SlowShazam)
    artifact = MediaArtifact(Path(tmp_path) / "audio.wav", ())

    with pytest.raises(RecognitionError) as raised:
        await ShazamEngine().identify(artifact, deadline=time.monotonic() + 0.01)

    assert raised.value.kind is ErrorKind.TIMEOUT


@pytest.mark.asyncio
async def test_acrcloud_engine_rejects_expired_deadline_without_request(
    monkeypatch, tmp_path
):
    from astrbot_plugin_song_identifier.engines.acrcloud import AcrcloudEngine

    engine = AcrcloudEngine(
        host="http://localhost", access_key="AK", access_secret="SK"
    )
    artifact = MediaArtifact(Path(tmp_path) / "audio.wav", ())

    class BoomSession:
        def post(self, *args, **kwargs):
            raise AssertionError("provider must not send a request after deadline")

    with pytest.raises(RecognitionError) as raised:
        await engine.identify(artifact, BoomSession(), deadline=time.monotonic() - 1)

    assert raised.value.kind is ErrorKind.TIMEOUT


def test_acrcloud_rate_limit_retryable_matches_official_semantics():
    with pytest.raises(RecognitionError) as raised:
        parse_acrcloud_response({"status": {"code": 3015, "msg": "QPS exceeded"}})

    assert raised.value.kind is ErrorKind.RATE_LIMITED
    assert raised.value.retryable is True

    with pytest.raises(RecognitionError) as raised:
        parse_acrcloud_response({"status": {"code": 3003, "msg": "limit exceeded"}})

    assert raised.value.kind is ErrorKind.RATE_LIMITED
    assert raised.value.retryable is False
