def test_plugin_importable():
    from astrbot_plugin_song_identifier.main import SongIdentifierPlugin

    assert SongIdentifierPlugin is not None
