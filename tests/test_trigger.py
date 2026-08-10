"""触发规则测试：关键词 + 引用；群聊需 @bot，私聊无需。"""

from astrbot.api.message_components import At, Plain, Reply

from astrbot_plugin_song_identifier.media import TriggerDetector


def make_at(qq):
    return At(qq=qq)


def make_reply():
    return Reply(id="123", chain=[])


def make_plain(text):
    return Plain(text=text)


def test_group_trigger_requires_at_reply_keyword(mock_event):
    det = TriggerDetector("识曲")
    # 只 @bot，无引用
    ev = mock_event(messages=[make_at("bot-1"), make_plain("识曲")], self_id="bot-1")
    assert det.check(ev) is False
    # @bot + 关键词 + 引用 → 触发
    ev = mock_event(
        messages=[make_at("bot-1"), make_plain("识曲"), make_reply()],
        self_id="bot-1",
    )
    assert det.check(ev) is True
    # @了别人，不是 bot
    ev = mock_event(
        messages=[make_at("user-2"), make_plain("识曲"), make_reply()],
        self_id="bot-1",
    )
    assert det.check(ev) is False


def test_private_trigger_no_at_required(mock_event):
    det = TriggerDetector("识曲")
    ev = mock_event(
        messages=[make_plain("识曲"), make_reply()],
        group_id="",  # 私聊
    )
    assert det.check(ev) is True
    # 私聊无关键词
    ev = mock_event(messages=[make_reply()], group_id="")
    assert det.check(ev) is False


def test_keyword_missing_never_triggers(mock_event):
    det = TriggerDetector("识曲")
    ev = mock_event(
        messages=[make_at("bot-1"), make_reply()],
        self_id="bot-1",
    )
    assert det.check(ev) is False
