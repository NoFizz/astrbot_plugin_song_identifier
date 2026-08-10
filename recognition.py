"""识别级联与错误聚合。

按配置顺序依次调用识别引擎，聚合各引擎的结构化错误，
并在总 deadline 内终止（超时、取消均向下传播）。
"""

import asyncio
import time
from dataclasses import dataclass

from .engines import AcrcloudEngine, ShazamEngine, XfyunAcrEngine, XfyunQbhEngine
from .models import ErrorKind, RecognitionError, SongInfo


@dataclass(slots=True)
class RecognitionOutcome:
    """一次级联识别的完整结果。"""

    song: SongInfo | None
    errors: tuple[RecognitionError, ...] = ()
    timed_out: bool = False


class RecognitionCascade:
    """按引擎顺序执行识别，首个命中即停止。"""

    def __init__(self, engines: list, timeout: float):
        self.engines = engines
        self.timeout = max(0.1, float(timeout))

    async def identify(self, artifact, session) -> RecognitionOutcome:
        deadline = time.monotonic() + self.timeout
        errors: list[RecognitionError] = []
        timed_out = False
        try:
            for engine in self.engines:
                if not engine.is_configured():
                    continue
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                try:
                    remaining = max(0.001, deadline - time.monotonic())
                    song = await asyncio.wait_for(
                        engine.identify(artifact, session, deadline), timeout=remaining
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    timed_out = True
                    engine_name = type(engine).__name__
                    errors.append(
                        RecognitionError(
                            ErrorKind.TIMEOUT, engine_name, engine.mode, "识别超时"
                        )
                    )
                    break
                except RecognitionError as error:
                    errors.append(error)
                    continue
                if song is not None:
                    return RecognitionOutcome(song=song, errors=tuple(errors))
        except asyncio.CancelledError:
            raise
        return RecognitionOutcome(song=None, errors=tuple(errors), timed_out=timed_out)


# 配置下拉中文标签 → 引擎构造器
_ENGINE_LABELS = {
    "ACRCloud": lambda cfg: AcrcloudEngine(
        host=_cfg_str(cfg, "engines", "acrcloud", "host"),
        access_key=_cfg_str(cfg, "engines", "acrcloud", "access_key"),
        access_secret=_cfg_str(cfg, "engines", "acrcloud", "access_secret"),
    ),
    "Shazam": lambda cfg: ShazamEngine(),
    "讯飞开放平台/ACRCloud": lambda cfg: XfyunAcrEngine(
        app_id=_cfg_str(cfg, "engines", "xfyun", "app_id"),
        api_key=_cfg_str(cfg, "engines", "xfyun", "api_key"),
        api_secret=_cfg_str(cfg, "engines", "xfyun", "api_secret"),
        mode="music",
    ),
    "讯飞开放平台/ACRCloud·哼唱": lambda cfg: XfyunAcrEngine(
        app_id=_cfg_str(cfg, "engines", "xfyun", "app_id"),
        api_key=_cfg_str(cfg, "engines", "xfyun", "api_key"),
        api_secret=_cfg_str(cfg, "engines", "xfyun", "api_secret"),
        mode="humming",
    ),
    "讯飞开放平台/自研": lambda cfg: XfyunQbhEngine(
        app_id=_cfg_str(cfg, "engines", "xfyun", "app_id"),
        api_key=_cfg_str(cfg, "engines", "xfyun", "api_key"),
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
    engines: list = []
    added: set[str] = set()
    for slot in ("primary", "secondary", "fallback"):
        label = _cfg_str(config, "engines", "select", slot)
        if label == "留空" or label not in _ENGINE_LABELS or label in added:
            continue
        engines.append(_ENGINE_LABELS[label](config))
        added.add(label)
    return RecognitionCascade(engines=engines, timeout=timeout)


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
