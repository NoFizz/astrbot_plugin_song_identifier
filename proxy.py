"""统一代理解析（知识库 09 章：配置优先 → 环境变量回退 → 直连）。

- httpx（网易云/QQ 搜索、封面 API）：默认 trust_env=True，自动读环境变量代理；
  插件配置非空时显式传 proxy。
- aiohttp（封面下载、识别引擎）：ClientSession(trust_env=True) 读环境变量代理；
  插件配置非空时 per-request 传 proxy（仅支持 http/https 代理 URL，socks 需额外依赖不引入）。
"""

_PROXY_KEYS = ("advanced", "proxy")


def resolve_proxy(config: dict) -> str | None:
    """返回插件配置的代理 URL；未配置或空白时返回 None（回退环境变量）。

    Args:
        config: 插件配置 dict。

    Returns:
        代理 URL（如 http://127.0.0.1:7890），未配置时 None。
    """
    node = config
    for key in _PROXY_KEYS:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    value = str(node or "").strip()
    return value or None
