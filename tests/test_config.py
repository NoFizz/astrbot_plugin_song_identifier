"""配置契约测试：_conf_schema.json 与 build_engines 标签映射一致。"""

import json
from pathlib import Path

from astrbot_plugin_song_identifier.recognition import _ENGINE_LABELS


def _schema():
    return json.loads(Path(__file__).parents[1].joinpath("_conf_schema.json").read_text(encoding="utf-8-sig"))


def test_schema_json_is_valid():
    schema = _schema()
    assert schema["engines"]["items"]["select"]["items"]["primary"]["type"] == "string"


def test_engine_options_match_build_engines_labels():
    schema = _schema()
    options = schema["engines"]["items"]["select"]["items"]["primary"]["options"]
    # 每个下拉选项要么是 "留空"，要么能被 build_engines 识别
    for option in options:
        assert option == "留空" or option in _ENGINE_LABELS, f"未识别的引擎标签: {option}"


def test_default_audio_max_seconds_is_12():
    schema = _schema()
    assert schema["advanced"]["items"]["audio_max_seconds"]["default"] == 12


def test_no_secrets_in_defaults():
    """凭据默认值必须为空，绝不携带密钥。"""
    schema = _schema()
    acrcloud = schema["engines"]["items"]["acrcloud"]["items"]
    xfyun = schema["engines"]["items"]["xfyun"]["items"]
    for field in ("host", "access_key", "access_secret"):
        assert acrcloud[field]["default"] == ""
    for field in ("app_id", "api_key", "api_secret"):
        assert xfyun[field]["default"] == ""
