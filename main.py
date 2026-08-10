"""听歌识曲插件（OneBot v11 / aiocqhttp）。

引用语音/视频/音频文件消息 → 识别歌曲 → 网易云增强 → 文本/图片/卡片输出。
编排层：只负责 AstrBot 事件与 LLM tool 的流程控制，
具体逻辑分布在 media / engines / recognition / enrichment / output。
"""

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.event_message_type import EventMessageType

from . import log
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
        log.set_debug(bool(config.get("advanced", {}).get("debug_log", False)))
        self.detector = TriggerDetector(
            str(config.get("trigger", {}).get("keyword", "识曲") or "识曲")
        )
        self.materializer = MediaMaterializer(
            max_seconds=int(config.get("advanced", {}).get("audio_max_seconds", 12))
        )
        self.identifier = build_engines(config)
        if not self.identifier.engines:
            log.warning("未配置任何识别引擎，请到插件配置中设置 首选/次选/备选 引擎。")
        self.enricher = SongEnricher()
        self.formatter = ResultFormatter(config)
        self._last_enriched = None

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理所有消息事件：触发检测、识别流程与结果发送。"""
        if not self.detector.check(event):
            return
        log.info("开始识曲")
        log.debug(
            f"触发检测命中: 群聊={not event.is_private_chat()}, "
            f"发送者={event.get_sender_id()}, 关键词={self.detector.keyword!r}"
        )

        media = MediaExtractor.extract_media(event)
        if media is None:
            log.debug("引用消息中无媒体段，提示用户")
            await event.send(event.plain_result("请引用包含语音或视频的消息后再试。"))
            event.stop_event()
            return
        log.debug(f"媒体段类型: {type(media).__name__}")

        try:
            outcome, materialize_ok = await self._identify(media)
            if not materialize_ok:
                log.warning("媒体落地失败，提示用户")
                await event.send(event.plain_result("媒体文件获取失败，请重试。"))
            elif outcome.timed_out:
                log.warning("识别超时，提示用户")
                await event.send(event.plain_result("识别超时，请稍后重试。"))
            elif outcome.song is None:
                log.warning("识别无结果，提示用户")
                await event.send(
                    event.plain_result("未能识别出歌曲，请确认音频清晰且时长足够。")
                )
            else:
                await self._send_result(event, outcome)
                log.info(
                    "识曲完成: %s - %s (provider=%s)",
                    outcome.song.title,
                    outcome.song.artist or "未知歌手",
                    outcome.song.provider,
                )
        except Exception as error:
            log.error("识曲主流程异常", exc=error)
            await event.send(event.plain_result(f"识曲出错：{error}"))
        finally:
            event.stop_event()

    async def _identify(self, media):
        """落地媒体 → 级联识别 → 网易云增强。

        Returns:
            (outcome, materialize_ok) 元组。
        """
        import aiohttp

        log.debug(f"开始处理媒体（截取前 {self.materializer.max_seconds} 秒）")
        artifact = await self.materializer.materialize(media)
        if artifact is None:
            log.warning("媒体落地失败（无本地音频文件）")
            return RecognitionOutcome(song=None), False
        try:
            try:
                size = artifact.path.stat().st_size
            except OSError:
                size = 0
            log.debug(f"媒体归一化完成: {artifact.path.name} ({size} bytes)")
            timeout = aiohttp.ClientTimeout(
                total=float(self.config.get("advanced", {}).get("identify_timeout", 60))
            )
            log.debug(f"开始级联识别（超时 {timeout.total}s）")
            async with aiohttp.ClientSession(timeout=timeout) as session:
                outcome = await self.identifier.identify(artifact, session)
            log.debug(
                f"级联识别结束: song={'有' if outcome.song else '无'}, "
                f"timed_out={outcome.timed_out}, errors={len(outcome.errors)}"
            )
            # 识别失败/回退时输出各引擎失败原因（脱敏：不含密钥/响应正文）
            if outcome.song is None:
                for error in outcome.errors:
                    log.warning(
                        f"引擎 {error.provider}/{error.mode} 识别失败: "
                        f"{error.message} (kind={error.kind.value}, code={error.code})"
                    )
                if not outcome.errors and not outcome.timed_out:
                    log.warning("所有引擎均未识别出歌曲（无结果）")
            if outcome.song is not None:
                log.debug(
                    f"增强查询(网易云): {outcome.song.title} {outcome.song.artist}"
                )
                enriched = await self.enricher.enrich(outcome.song)
                outcome.song = enriched.song
                self._last_enriched = enriched
                log.debug(
                    f"增强结果: netease_id={enriched.netease_id or '无'}, "
                    f"cover={bool(enriched.cover_url)}"
                )
            return outcome, True
        finally:
            await artifact.cleanup()

    @filter.llm_tool(name="identify_song")
    async def identify_song(self, event: AstrMessageEvent, target: str):
        """识别语音/视频/音频文件中的歌曲。

        当用户引用了（回复了）一条包含语音、视频或音频文件的消息，并询问
        这是什么歌、歌名是什么、BGM 是什么时，调用此工具进行歌曲识别。
        媒体文件自动从用户引用的消息中获取，target 参数仅用于触发工具调用，
        可传任意非空字符串（如"识别"）。

        Args:
            target(string): 要识别的媒体引用消息。传任意非空字符串即可，媒体自动从引用消息获取。
        """
        log.debug(f"LLM 工具被调用: target={target!r}")
        media = MediaExtractor.extract_media(event)
        if media is None:
            log.debug("LLM 工具: 无媒体段")
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
        log.debug(f"LLM 工具返回文本: {text[:80]}")
        yield event.plain_result(text)

    async def _send_result(self, event: AstrMessageEvent, outcome: RecognitionOutcome):
        """按配置输出识别结果：card / image / text。"""
        fmt = str(self.config.get("output", {}).get("format", "文本") or "文本").strip()
        log.debug(f"输出格式: {fmt}")
        if fmt == "卡片":
            if await self._try_send_card(event):
                log.debug("QQ 音乐卡片发送成功")
                await self._send_link(event)
                return
            log.warning("卡片发送失败，降级为文本")
        if fmt == "图片":
            image = await self.formatter.build_image(self._last_enriched)
            if image:
                from astrbot.api import message_components as Comp

                await event.send(event.chain_result([Comp.Image.fromBytes(image)]))
                log.debug(f"图片卡片发送完成 ({len(image)} bytes)")
                await self._send_link(event)
                return
            log.warning("图片生成失败，降级为文本")
        text = self.formatter.format_text(self._last_enriched)
        await event.send(event.plain_result(text))
        log.debug(f"文本结果已发送: {text[:80]}")
        await self._send_link(event)

    async def _send_link(self, event: AstrMessageEvent):
        """按 output.link 开关分条发送试听链接。"""
        if not self.config.get("output", {}).get("link", True):
            return
        link = self.formatter.format_link(self._last_enriched)
        if link:
            await event.send(event.plain_result(link))
            log.debug(f"试听链接已发送: {link[:60]}")

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
                log.warning(f"卡片发送失败({label}): {error}")
        return False
