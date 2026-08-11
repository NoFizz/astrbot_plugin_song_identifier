"""recognition 级联与错误聚合测试。"""

import pytest
from astrbot_plugin_song_identifier.models import ErrorKind, RecognitionError, SongInfo
from astrbot_plugin_song_identifier.recognition import (
    RecognitionCascade,
    RecognitionOutcome,
    build_engines,
)


class _FakeEngine:
    """按队列返回预置结果的假引擎。"""

    provider = "fake"
    mode = "music"

    def __init__(self, outcomes, configured=True):
        self._outcomes = list(outcomes)
        self._configured = configured
        self.calls = 0

    def is_configured(self):
        return self._configured

    async def identify(self, artifact, session, deadline):
        self.calls += 1
        item = self._outcomes.pop(0)
        if isinstance(item, RecognitionError):
            raise item
        return item


def _song(provider="fake"):
    return SongInfo(title="晴天", provider=provider, mode="music")


@pytest.mark.asyncio
async def test_cascade_returns_first_hit_and_stops():
    e1 = _FakeEngine([_song("a")])
    e2 = _FakeEngine([_song("b")])
    outcome = await RecognitionCascade([e1, e2], timeout=5).identify(None, None)

    assert outcome.song is not None and outcome.song.provider == "a"
    assert e1.calls == 1
    assert e2.calls == 0
    assert outcome.timed_out is False
    assert outcome.errors == ()


@pytest.mark.asyncio
async def test_cascade_no_match_continues():
    e1 = _FakeEngine([None])
    e2 = _FakeEngine([_song("b")])
    outcome = await RecognitionCascade([e1, e2], timeout=5).identify(None, None)

    assert outcome.song is not None and outcome.song.provider == "b"
    assert e1.calls == 1 and e2.calls == 1


@pytest.mark.asyncio
async def test_cascade_skips_unconfigured_engine():
    e1 = _FakeEngine([], configured=False)
    e2 = _FakeEngine([_song("b")])
    outcome = await RecognitionCascade([e1, e2], timeout=5).identify(None, None)

    assert outcome.song is not None and outcome.song.provider == "b"
    assert e1.calls == 0 and e2.calls == 1


@pytest.mark.asyncio
async def test_cascade_retryable_error_continues_and_is_recorded():
    e1 = _FakeEngine(
        [
            RecognitionError(
                ErrorKind.TEMPORARY_NETWORK, "a", "music", "net", retryable=True
            )
        ]
    )
    e2 = _FakeEngine([_song("b")])
    # max_retries=0：本测试只验证"错误被记录且级联继续"，重试行为由
    # test_cascade_retries_* 覆盖；默认 max_retries=2 会让单结果队列空弹
    outcome = await RecognitionCascade([e1, e2], timeout=5, max_retries=0).identify(None, None)

    assert outcome.song is not None and outcome.song.provider == "b"
    assert len(outcome.errors) == 1
    assert outcome.errors[0].kind is ErrorKind.TEMPORARY_NETWORK


@pytest.mark.asyncio
async def test_cascade_auth_error_does_not_retry_same_engine_but_continues_others():
    e1 = _FakeEngine([RecognitionError(ErrorKind.AUTH_FAILED, "a", "music", "bad key")])
    e2 = _FakeEngine([_song("b")])
    outcome = await RecognitionCascade([e1, e2], timeout=5).identify(None, None)

    # 鉴权错误不重试同一引擎；级联记录后尝试下一引擎
    assert e1.calls == 1
    assert outcome.song is not None and outcome.song.provider == "b"
    assert outcome.errors[0].kind is ErrorKind.AUTH_FAILED


@pytest.mark.asyncio
async def test_cascade_all_fail_returns_none_with_errors():
    e1 = _FakeEngine([RecognitionError(ErrorKind.AUTH_FAILED, "a", "music", "k")])
    e2 = _FakeEngine([RecognitionError(ErrorKind.RATE_LIMITED, "b", "music", "q")])
    outcome = await RecognitionCascade([e1, e2], timeout=5).identify(None, None)

    assert outcome.song is None
    assert len(outcome.errors) == 2
    assert outcome.timed_out is False


@pytest.mark.asyncio
async def test_cascade_respects_timeout():
    class _SlowEngine(_FakeEngine):
        async def identify(self, artifact, session, deadline):
            self.calls += 1
            await __import__("asyncio").sleep(0.2)
            return None

    e1 = _SlowEngine([])
    outcome = await RecognitionCascade([e1], timeout=0.05).identify(None, None)

    assert outcome.song is None
    assert outcome.timed_out is True
    assert any(err.kind is ErrorKind.TIMEOUT for err in outcome.errors)


@pytest.mark.asyncio
async def test_cascade_propagates_cancellation():
    class _SlowEngine(_FakeEngine):
        async def identify(self, artifact, session, deadline):
            await __import__("asyncio").sleep(10)
            return None

    import asyncio

    task = asyncio.create_task(
        RecognitionCascade([_SlowEngine([])], timeout=30).identify(None, None)
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_recognition_outcome_has_expected_fields():
    outcome = RecognitionOutcome(song=None, errors=(), timed_out=True)
    assert outcome.song is None
    assert outcome.errors == ()
    assert outcome.timed_out is True


@pytest.mark.asyncio
async def test_cascade_retries_retryable_error_then_succeeds(monkeypatch):
    """可重试错误：同一引擎重试，次数内成功后返回。"""
    import asyncio

    real_sleep = asyncio.sleep
    sleeps = []
    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.recognition.asyncio.sleep",
        lambda s: sleeps.append(s) or real_sleep(0),
    )
    e1 = _FakeEngine(
        [
            RecognitionError(ErrorKind.TEMPORARY_NETWORK, "a", "music", "net", retryable=True),
            RecognitionError(ErrorKind.TEMPORARY_NETWORK, "a", "music", "net", retryable=True),
            _song("a"),
        ]
    )
    outcome = await RecognitionCascade([e1], timeout=5, max_retries=2, retry_interval=2).identify(None, None)

    assert outcome.song is not None and outcome.song.provider == "a"
    assert e1.calls == 3
    assert sleeps == [2.0, 2.0]
    assert len(outcome.errors) == 2  # 两次失败均记录


@pytest.mark.asyncio
async def test_cascade_retries_exhausted_returns_errors(monkeypatch):
    """重试耗尽仍失败：累积全部错误并返回无结果。"""
    import asyncio

    real_sleep = asyncio.sleep
    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.recognition.asyncio.sleep",
        lambda s: real_sleep(0),
    )
    e1 = _FakeEngine(
        [
            RecognitionError(ErrorKind.TEMPORARY_NETWORK, "a", "music", "net", retryable=True),
            RecognitionError(ErrorKind.TEMPORARY_NETWORK, "a", "music", "net", retryable=True),
            RecognitionError(ErrorKind.TEMPORARY_NETWORK, "a", "music", "net", retryable=True),
        ]
    )
    outcome = await RecognitionCascade([e1], timeout=5, max_retries=2, retry_interval=2).identify(None, None)

    assert outcome.song is None
    assert e1.calls == 3  # 1 次 + 重试 2 次
    assert len(outcome.errors) == 3


@pytest.mark.asyncio
async def test_cascade_does_not_retry_non_retryable_error(monkeypatch):
    """非可重试错误（认证等）不重试，直接换下一引擎。"""
    import asyncio

    real_sleep = asyncio.sleep
    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.recognition.asyncio.sleep",
        lambda s: real_sleep(0),
    )
    e1 = _FakeEngine([RecognitionError(ErrorKind.AUTH_FAILED, "a", "music", "bad key")])
    e2 = _FakeEngine([_song("b")])
    outcome = await RecognitionCascade([e1, e2], timeout=5, max_retries=2, retry_interval=2).identify(None, None)

    assert outcome.song is not None and outcome.song.provider == "b"
    assert e1.calls == 1  # 不重试
    assert e2.calls == 1


@pytest.mark.asyncio
async def test_cascade_zero_retries_no_retry(monkeypatch):
    """retry_times=0：不重试，与旧行为一致。"""
    import asyncio

    real_sleep = asyncio.sleep
    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.recognition.asyncio.sleep",
        lambda s: real_sleep(0),
    )
    e1 = _FakeEngine(
        [RecognitionError(ErrorKind.TEMPORARY_NETWORK, "a", "music", "net", retryable=True)]
    )
    e2 = _FakeEngine([_song("b")])
    outcome = await RecognitionCascade([e1, e2], timeout=5, max_retries=0, retry_interval=2).identify(None, None)

    assert outcome.song is not None and outcome.song.provider == "b"
    assert e1.calls == 1


def test_build_engines_reads_zero_retry_config():
    """retry_times=0 / retry_interval=0 必须原样生效（0 = 关闭重试）。"""
    config = {
        "engines": {"select": {"primary": "留空", "secondary": "留空", "fallback": "留空"}},
        "advanced": {"retry_times": 0, "retry_interval": 0},
    }
    cascade = build_engines(config)
    assert cascade.max_retries == 0
    assert cascade.retry_interval == 0.0
