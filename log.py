"""插件统一日志工具。

所有插件日志统一前缀 `[听歌识曲]`；debug 级别受 `advanced.debug_log`
开关控制，由插件 __init__ 调用 set_debug() 设置。其余级别（info/warning/
error）始终输出。
"""

from astrbot.api import logger

PLUGIN_NAME = "听歌识曲"

_DEBUG_LOG = False


def set_debug(enabled: bool) -> None:
    """设置 debug 日志开关。

    Args:
        enabled: 是否输出详细日志。
    """
    global _DEBUG_LOG
    _DEBUG_LOG = bool(enabled)


def debug(msg: str, *args) -> None:
    """输出详细分步日志；仅 debug_log 开启时生效。

    stacklevel=3 让 AstrBot 日志来源指向业务调用者（main/media/engines）
    而非本中转模块，便于按日志定位到具体业务代码行。

    Args:
        msg: 日志内容，支持 %s 占位符。
        *args: 格式化参数。
    """
    if _DEBUG_LOG:
        logger.info(f"[{PLUGIN_NAME}] {msg}", *args, stacklevel=3)


def info(msg: str, *args) -> None:
    """输出关键流程日志。

    Args:
        msg: 日志内容，支持 %s 占位符。
        *args: 格式化参数。
    """
    logger.info(f"[{PLUGIN_NAME}] {msg}", *args, stacklevel=3)


def warning(msg: str, *args) -> None:
    """输出可恢复的异常/降级日志。

    Args:
        msg: 日志内容，支持 %s 占位符。
        *args: 格式化参数。
    """
    logger.warning(f"[{PLUGIN_NAME}] {msg}", *args, stacklevel=3)


def error(msg: str, exc: BaseException | None = None) -> None:
    """输出最终失败日志，附带异常堆栈。

    Args:
        msg: 日志内容。
        exc: 关联的异常（可选）。
    """
    if exc is not None:
        logger.exception(f"[{PLUGIN_NAME}] {msg}: {exc}")
    else:
        logger.error(f"[{PLUGIN_NAME}] {msg}")
