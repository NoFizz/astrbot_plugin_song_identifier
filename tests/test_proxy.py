"""统一代理解析测试。"""

import pytest

from astrbot_plugin_song_identifier.proxy import resolve_proxy


def test_resolve_proxy_empty_config_returns_none():
    assert resolve_proxy({}) is None


def test_resolve_proxy_blank_value_returns_none():
    assert resolve_proxy({"advanced": {"proxy": "  "}}) is None


def test_resolve_proxy_missing_advanced_returns_none():
    assert resolve_proxy({"output": {}}) is None


def test_resolve_proxy_returns_configured_proxy():
    assert resolve_proxy({"advanced": {"proxy": "http://127.0.0.1:7890"}}) == (
        "http://127.0.0.1:7890"
    )
