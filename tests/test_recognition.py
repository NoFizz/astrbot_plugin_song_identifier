"""recognition 级联与错误聚合测试。"""

import pytest
from astrbot_plugin_song_identifier.models import ErrorKind, RecognitionError, SongInfo
from astrbot_plugin_song_identifier.recognition import (
    RecognitionCascade,
    RecognitionOutcome,
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
    outcome = await RecognitionCascade([e1, e2], timeout=5).identify(None, None)

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
