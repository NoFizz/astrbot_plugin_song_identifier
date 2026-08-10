"""统一日志模块测试：前缀、debug 开关与级别。"""

import logging

import astrbot_plugin_song_identifier.log as log_module


class _FakeLogger:
    def __init__(self):
        self.info_msgs = []
        self.warning_msgs = []
        self.error_msgs = []
        self.exception_msgs = []
        self.info_stacklevels = []

    def info(self, msg, *args, **kwargs):
        self.info_msgs.append(msg)
        self.info_stacklevels.append(kwargs.get("stacklevel"))

    def warning(self, msg, *args, **kwargs):
        self.warning_msgs.append(msg)

    def error(self, msg, *args, **kwargs):
        self.error_msgs.append(msg)

    def exception(self, msg, *args, **kwargs):
        self.exception_msgs.append(msg)


def test_all_messages_have_plugin_prefix(monkeypatch):
    fake = _FakeLogger()
    monkeypatch.setattr(log_module, "logger", fake)
    log_module.set_debug(True)

    log_module.info("开始识曲")
    log_module.warning("引擎失败")
    log_module.error("最终失败")
    log_module.debug("详细步骤")

    for msg in (
        fake.info_msgs + fake.warning_msgs + fake.error_msgs + fake.exception_msgs
    ):
        assert msg.startswith("[听歌识曲] "), msg


def test_debug_only_when_enabled(monkeypatch):
    fake = _FakeLogger()
    monkeypatch.setattr(log_module, "logger", fake)

    log_module.set_debug(False)
    log_module.debug("不应输出")
    assert fake.info_msgs == []

    log_module.set_debug(True)
    log_module.debug("应输出")
    assert fake.info_msgs == ["[听歌识曲] 应输出"]

    log_module.set_debug(False)  # 还原


def test_info_warning_always_output(monkeypatch):
    fake = _FakeLogger()
    monkeypatch.setattr(log_module, "logger", fake)
    log_module.set_debug(False)

    log_module.info("关键流程")
    log_module.warning("降级")

    assert fake.info_msgs == ["[听歌识曲] 关键流程"]
    assert fake.warning_msgs == ["[听歌识曲] 降级"]


def test_error_with_exception_logs_stack(monkeypatch):
    fake = _FakeLogger()
    monkeypatch.setattr(log_module, "logger", fake)

    log_module.error("出错了", exc=ValueError("boom"))

    assert len(fake.exception_msgs) == 1
    assert fake.exception_msgs[0] == "[听歌识曲] 出错了: boom"


def test_log_calls_pass_stacklevel_to_point_to_business_code(monkeypatch):
    """log 中转必须带 stacklevel，让 AstrBot 显示真实业务文件而非 log.py。

    AstrBot 的 source_file 取"目录名.文件名"，来自调用 logger 的帧。
    若 log.py 中转不带 stacklevel，来源恒为 astrbot_plugin_song_identifier.log，
    无法定位到实际业务代码。
    """
    fake = _FakeLogger()
    monkeypatch.setattr(log_module, "logger", fake)
    log_module.set_debug(True)

    log_module.debug("业务日志")
    log_module.info("业务日志")

    for sl in fake.info_stacklevels:
        assert sl is not None and sl >= 2, "log 调用必须带 stacklevel"
