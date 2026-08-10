"""听歌识曲插件（OneBot v11 / aiocqhttp）。

引用语音/视频/音频文件消息 → 识别歌曲 → 网易云增强 → 文本/图片/卡片输出。
编排层：只负责 AstrBot 事件与 LLM tool 的流程控制，
具体逻辑分布在 media / engines / recognition / enrichment / output。
"""

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.event_message_type import EventMessageType

from .enrichment import SongEnricher
from .media import MediaExtractor, MediaMaterializer, TriggerDetector
from .output import PLATFORM_PROVIDERS, ResultFormatter
from .recognition import RecognitionOutcome, build_engines


class SongIdentifierPlugin(Star):
    """识别引用消息中的歌曲（OneBot v11）。"""

    def __init__(self, context: Context, config: dict):
        """构造插件，装配媒体落地、识别级联、增强与格式化器。

        Args:
            context: AstrBot Star 上下文。
            config: 插件配置 dict。
        """
        super().__init__(context)
        self.config = config
        self.detector = TriggerDetector(
            str(config.get("trigger", {}).get("keyword", "识曲") or "识曲")
        )
        self.materializer = MediaMaterializer(
            max_seconds=int(config.get("advanced", {}).get("audio_max_seconds", 12))
        )
        self.identifier = build_engines(config)
        if not self.identifier.engines:
            logger.warning(
                "未配置任何识别引擎，请到插件配置中设置 首选/次选/备选 引擎。"
            )
        self.enricher = SongEnricher()
        self.formatter = ResultFormatter(config)
        self._last_enriched = None

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理所有消息事件：触发检测、识别流程与结果发送。"""
        if not self.detector.check(event):
            return
        logger.info("开始识曲")

        media = MediaExtractor.extract_media(event)
        if media is None:
            await event.send(event.plain_result("请引用包含语音或视频的消息后再试。"))
            event.stop_event()
            return

        try:
            outcome, materialize_ok = await self._identify(media)
            if not materialize_ok:
                await event.send(event.plain_result("媒体文件获取失败，请重试。"))
            elif outcome.timed_out:
                await event.send(event.plain_result("识别超时，请稍后重试。"))
            elif outcome.song is None:
                await event.send(
                    event.plain_result("未能识别出歌曲，请确认音频清晰且时长足够。")
                )
            else:
                await self._send_result(event, outcome)
        except Exception as error:
            logger.exception("识曲主流程异常: %s", error)
            await event.send(event.plain_result(f"识曲出错：{error}"))
        finally:
            event.stop_event()

    async def _identify(self, media):
        """落地媒体 → 级联识别 → 网易云增强。

        Returns:
            (outcome, materialize_ok) 元组。
        """
        import aiohttp

        artifact = await self.materializer.materialize(media)
        if artifact is None:
            return RecognitionOutcome(song=None), False
        try:
            timeout = aiohttp.ClientTimeout(
                total=float(self.config.get("advanced", {}).get("identify_timeout", 60))
            )
            async with aiohttp.ClientSession(timeout=timeout) as session:
                outcome = await self.identifier.identify(artifact, session)
            if outcome.song is not None:
                enriched = await self.enricher.enrich(outcome.song)
                outcome.song = enriched.song
                self._last_enriched = enriched
                logger.info(
                    "识别成功: %s - %s (provider=%s)",
                    outcome.song.title,
                    outcome.song.artist or "未知歌手",
                    outcome.song.provider,
                )
            return outcome, True
        finally:
            await artifact.cleanup()

    @filter.llm_tool(name="identify_song")
    async def identify_song(self, event: AstrMessageEvent):
        """识别语音/视频/音频文件中的歌曲。

        当用户引用了（回复了）一条包含语音、视频或音频文件的消息，并询问
        这是什么歌、歌名是什么、BGM 是什么时，调用此工具进行歌曲识别。
        媒体文件自动从用户引用的消息中获取，无需额外参数。

        Args:
            无需参数。
        """
        media = MediaExtractor.extract_media(event)
        if media is None:
            yield event.plain_result(
                "用户消息中没有可识别的媒体：需要引用（回复）一条包含"
                "语音/视频/音频文件的消息。"
            )
            return
        outcome, materialize_ok = await self._identify(media)
        if not materialize_ok:
            yield event.plain_result("媒体文件处理失败，请重试。")
            return
        if outcome.timed_out:
            yield event.plain_result("识别超时，请稍后重试。")
            return
        if outcome.song is None:
            yield event.plain_result("未能识别出歌曲，请确认音频清晰且时长足够。")
            return
        text = self.formatter.format_text(self._last_enriched)
        link = self.formatter.format_link(self._last_enriched)
        if link:
            text = f"{text}\n{link}"
        yield event.plain_result(text)

    async def _send_result(self, event: AstrMessageEvent, outcome: RecognitionOutcome):
        """按配置输出识别结果：card / image / text。"""
        fmt = str(self.config.get("output", {}).get("format", "文本") or "文本").strip()
        if fmt == "卡片":
            if await self._try_send_card(event):
                await self._send_link(event)
                return
            logger.warning("卡片发送失败，降级为文本")
        if fmt == "图片":
            image = await self.formatter.build_image(self._last_enriched)
            if image:
                from astrbot.api import message_components as Comp

                await event.send(event.chain_result([Comp.Image.fromBytes(image)]))
                await self._send_link(event)
                return
            logger.warning("图片生成失败，降级为文本")
        await event.send(
            event.plain_result(self.formatter.format_text(self._last_enriched))
        )
        await self._send_link(event)

    async def _send_link(self, event: AstrMessageEvent):
        """按 output.link 开关分条发送试听链接。"""
        if not self.config.get("output", {}).get("link", True):
            return
        link = self.formatter.format_link(self._last_enriched)
        if link:
            await event.send(event.plain_result(link))

    async def _try_send_card(self, event: AstrMessageEvent) -> bool:
        """按配置卡片平台顺序尝试发送，全部失败返回 False。"""
        bot = getattr(event, "bot", None)
        if bot is None:
            return False
        action = "send_private_msg" if event.is_private_chat() else "send_group_msg"
        target_key = "user_id" if event.is_private_chat() else "group_id"
        target = (
            event.get_sender_id() if event.is_private_chat() else event.get_group_id()
        )
        for slot in ("primary", "secondary"):
            label = (
                str(
                    self.config.get("output", {})
                    .get("card_platforms", {})
                    .get(slot, "")
                )
                or ""
            )
            provider = PLATFORM_PROVIDERS.get(label)
            if provider is None:
                continue
            try:
                segment = await provider.build_music_segment(self._last_enriched)
                if not segment:
                    continue
                await bot.api.call_action(
                    action, **{target_key: target, "message": [segment]}
                )
                return True
            except Exception as error:
                logger.warning("卡片发送失败(%s): %s", label, error)
        return False
