"""识别级联与错误聚合。

按配置顺序依次调用识别引擎，聚合各引擎的结构化错误，
并在总 deadline 内终止（超时、取消均向下传播）。
"""

import asyncio
import time
from dataclasses import dataclass

from .engines import AcrcloudEngine, ShazamEngine, XfyunAcrEngine, XfyunQbhEngine
from .models import ErrorKind, RecognitionError, SongInfo
from .proxy import resolve_proxy


@dataclass(slots=True)
class RecognitionOutcome:
    """一次级联识别的完整结果。"""

    song: SongInfo | None
    errors: tuple[RecognitionError, ...] = ()
    timed_out: bool = False


class RecognitionCascade:
    """按引擎顺序执行识别，首个命中即停止；可重试错误按配置自动重试。"""

    def __init__(
        self,
        engines: list,
        timeout: float,
        max_retries: int = 2,
        retry_interval: float = 2.0,
    ):
        self.engines = engines
        self.timeout = max(0.1, float(timeout))
        self.max_retries = max(0, int(max_retries))
        self.retry_interval = max(0.0, float(retry_interval))

    async def identify(self, artifact, session) -> RecognitionOutcome:
        from . import log

        deadline = time.monotonic() + self.timeout
        errors: list[RecognitionError] = []
        timed_out = False
        try:
            for engine in self.engines:
                provider = getattr(engine, "provider", type(engine).__name__)
                mode = getattr(engine, "mode", "")
                if not engine.is_configured():
                    log.debug(f"引擎 {provider} 未配置，跳过")
                    continue
                if time.monotonic() >= deadline:
                    timed_out = True
                    log.warning("级联超时，停止尝试后续引擎")
                    break
                log.debug(f"正在使用引擎: {provider} ({mode})")
                # 可重试错误在同一引擎上自动重试（次数/间隔由配置控制），
                # 重试前检查剩余 deadline，避免超过总超时
                attempts = 0
                song = None
                while True:
                    attempts += 1
                    try:
                        remaining = max(0.001, deadline - time.monotonic())
                        song = await asyncio.wait_for(
                            engine.identify(artifact, session, deadline), timeout=remaining
                        )
                        break
                    except (asyncio.TimeoutError, TimeoutError):
                        timed_out = True
                        errors.append(
                            RecognitionError(ErrorKind.TIMEOUT, provider, mode, "识别超时")
                        )
                        log.warning(f"引擎 {provider} 超时")
                        break
                    except RecognitionError as error:
                        if (
                            error.retryable
                            and attempts <= self.max_retries
                            and time.monotonic() < deadline
                        ):
                            errors.append(error)
                            sleep_for = min(
                                self.retry_interval, max(0.001, deadline - time.monotonic())
                            )
                            log.warning(
                                f"引擎 {provider} 第 {attempts} 次失败，"
                                f"{sleep_for:.1f}s 后重试: {error.message} "
                                f"(kind={error.kind.value}, code={error.code})"
                            )
                            await asyncio.sleep(sleep_for)
                            continue
                        errors.append(error)
                        log.warning(
                            f"引擎 {provider} 返回错误: {error.message} "
                            f"(kind={error.kind.value}, code={error.code})"
                        )
                        break
                if song is not None:
                    log.debug(
                        f"引擎 {provider} 识别成功: {song.title} - "
                        f"{song.artist or '未知歌手'}"
                    )
                    return RecognitionOutcome(song=song, errors=tuple(errors))
                if timed_out:
                    break
                log.debug(f"引擎 {provider} 无结果，尝试下一引擎")
        except asyncio.CancelledError:
            raise
        return RecognitionOutcome(song=None, errors=tuple(errors), timed_out=timed_out)


# 配置下拉中文标签 → 引擎构造器
_ENGINE_LABELS = {
    "ACRCloud": lambda cfg, proxy: AcrcloudEngine(
        host=_cfg_str(cfg, "engines", "acrcloud", "host"),
        access_key=_cfg_str(cfg, "engines", "acrcloud", "access_key"),
        access_secret=_cfg_str(cfg, "engines", "acrcloud", "access_secret"),
        proxy=proxy,
    ),
    "Shazam": lambda cfg, proxy: ShazamEngine(proxy=proxy),
    "讯飞开放平台/ACRCloud": lambda cfg, proxy: XfyunAcrEngine(
        app_id=_cfg_str(cfg, "engines", "xfyun", "app_id"),
        api_key=_cfg_str(cfg, "engines", "xfyun", "api_key"),
        api_secret=_cfg_str(cfg, "engines", "xfyun", "api_secret"),
        mode="music",
        proxy=proxy,
    ),
    "讯飞开放平台/ACRCloud·哼唱": lambda cfg, proxy: XfyunAcrEngine(
        app_id=_cfg_str(cfg, "engines", "xfyun", "app_id"),
        api_key=_cfg_str(cfg, "engines", "xfyun", "api_key"),
        api_secret=_cfg_str(cfg, "engines", "xfyun", "api_secret"),
        mode="humming",
        proxy=proxy,
    ),
    "讯飞开放平台/自研": lambda cfg, proxy: XfyunQbhEngine(
        app_id=_cfg_str(cfg, "engines", "xfyun", "app_id"),
        api_key=_cfg_str(cfg, "engines", "xfyun", "api_key"),
        proxy=proxy,
    ),
}


def _cfg_str(config: dict, *keys) -> str:
    node = config
    for key in keys:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
        if node is None:
            return ""
    return str(node or "").strip()


def build_engines(config: dict) -> RecognitionCascade:
    """从配置的首选/次选/备选三档构造级联识别器。

    Args:
        config: 插件配置 dict（engines.select.{primary,secondary,fallback} 为标签）。

    Returns:
        RecognitionCascade：按配置顺序排列的级联识别器。
    """
    timeout = _cfg_float(config, "advanced", "identify_timeout", 60)
    max_retries = int(_cfg_float(config, "advanced", "retry_times", 2))
    retry_interval = _cfg_float(config, "advanced", "retry_interval", 2)
    proxy = resolve_proxy(config)
    engines: list = []
    added: set[str] = set()
    for slot in ("primary", "secondary", "fallback"):
        label = _cfg_str(config, "engines", "select", slot)
        if label == "留空" or label not in _ENGINE_LABELS or label in added:
            continue
        engines.append(_ENGINE_LABELS[label](config, proxy))
        added.add(label)
    return RecognitionCascade(
        engines=engines, timeout=timeout, max_retries=max_retries, retry_interval=retry_interval
    )


def _cfg_float(config: dict, *keys, default: float = 60.0) -> float:
    """从嵌套配置读取浮点值，缺失或非法时返回 default。

    Args:
        config: 插件配置 dict。
        *keys: 逐层键路径。
        default: 缺失或非法时的默认值。

    Returns:
        解析后的浮点值。
    """
    value = _cfg_str(config, *keys)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default
