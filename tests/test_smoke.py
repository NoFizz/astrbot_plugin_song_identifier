def test_plugin_importable():
    from astrbot_plugin_song_identifier.main import SongIdentifierPlugin

    assert SongIdentifierPlugin is not None


def test_log_debug_gated_by_flag(monkeypatch):
    """详细日志仅在 debug_log 开启时输出。"""
    import astrbot_plugin_song_identifier.main as m

    calls = []
    monkeypatch.setattr(m.logger, "info", lambda msg: calls.append(msg))
    m._DEBUG_LOG = False
    m._log_debug("detail message")
    assert calls == []
    m._DEBUG_LOG = True
    m._log_debug("detail message 2")
    assert calls == ["detail message 2"]
    m._DEBUG_LOG = False  # 还原全局状态
