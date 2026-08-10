from astrbot_plugin_song_identifier.models import (
    ErrorKind,
    RecognitionError,
    SongInfo,
)


def test_song_info_keeps_provider_and_platform_ids_separate():
    song = SongInfo(
        title="T",
        provider="acrcloud",
        mode="music",
        netease_id="163",
        qq_songmid="qq",
    )

    assert song.provider == "acrcloud"
    assert song.netease_id == "163"
    assert song.qq_songmid == "qq"


def test_song_info_requires_non_empty_title():
    assert SongInfo(title="  ", provider="shazam", mode="music").is_valid() is False
    assert SongInfo(title="花の塔", provider="shazam", mode="music").is_valid() is True


def test_recognition_error_exposes_retry_policy():
    error = RecognitionError(
        ErrorKind.RATE_LIMITED,
        "acrcloud",
        "music",
        "quota",
        3015,
        True,
    )

    assert error.kind is ErrorKind.RATE_LIMITED
    assert error.retryable is True
    assert error.code == 3015
    assert str(error) == "acrcloud/music: quota (code=3015)"


def test_error_kind_values_match_contract():
    assert {kind.value for kind in ErrorKind} == {
        "no_match",
        "input_invalid",
        "not_configured",
        "auth_failed",
        "rate_limited",
        "temporary_network",
        "protocol_error",
        "timeout",
        "cancelled",
    }
