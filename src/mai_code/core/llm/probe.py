from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import anthropic
import httpx

logger = logging.getLogger(__name__)


# 探测端点支持的模型列表。两种策略：
# 1) Anthropic SDK 的 /v1/models（官方端点与部分网关）
# 2) OpenAI 风格 GET {origin}/models、{origin}/v1/models（DeepSeek 等兼容网关）
# 全部失败返回空列表，调用方回退默认模型。base_url/api_key 为空时回退读环境变量。
async def detect_endpoint_models(base_url: str = "", api_key: str = "") -> list[str]:
    base = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    key = (
        api_key
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or ""
    )
    models: list[str] = []
    # 策略 1：Anthropic 风格
    try:
        kwargs: dict[str, Any] = {"api_key": key or "empty"}
        if base:
            kwargs["base_url"] = base
        client = anthropic.AsyncAnthropic(**kwargs)
        page = await asyncio.wait_for(client.models.list(), timeout=5.0)
        models = sorted({m.id for m in page.data})
    except Exception as e:
        logger.info("anthropic /v1/models probe failed: %s", e)
    # 策略 2：OpenAI 风格（两种响应都是 data[].id，解析一致）
    if not models and base:
        for url in _candidate_urls(base):
            try:
                async with httpx.AsyncClient(timeout=5.0) as hc:
                    resp = await hc.get(
                        url,
                        headers={"Authorization": f"Bearer {key}", "x-api-key": key},
                    )
                    resp.raise_for_status()
                    ids = [str(m["id"]) for m in resp.json().get("data", []) if m.get("id")]
                if ids:
                    models = sorted(set(ids))
                    break
            except Exception as e:
                logger.info("model probe %s failed: %s", url, e)
    logger.info("detected %d models from endpoint", len(models))
    return models


# 返回 OpenAI 风格 /models 的候选探测地址
def _candidate_urls(base: str) -> list[str]:
    origin = re.sub(r"/anthropic$", "", base)
    return [f"{origin}/models", f"{origin}/v1/models", f"{base}/v1/models"]


# 网关在 /models 响应里标注上下文窗口的常见字段名（vLLM / OpenRouter / 各类中转）
_CONTEXT_WINDOW_KEYS = ("context_length", "context_window", "max_model_len", "max_context_length")
_MAX_PLAUSIBLE_WINDOW = 100_000_000


# 从 /models 响应中提取上下文窗口：取模型条目与顶层字段中的最大合理值，没有则返回 0
def _extract_context_window(payload: Any) -> int:
    entries: list[Any] = []
    if isinstance(payload, dict):
        entries.append(payload)
        data = payload.get("data")
        if isinstance(data, list):
            entries.extend(x for x in data if isinstance(x, dict))
    elif isinstance(payload, list):
        entries.extend(x for x in payload if isinstance(x, dict))
    best = 0
    for entry in entries:
        for key_name in _CONTEXT_WINDOW_KEYS:
            val = entry.get(key_name)
            # 排除布尔值（bool 是 int 子类）与明显不合理的超大值
            if isinstance(val, int) and not isinstance(val, bool) \
                    and 0 < val <= _MAX_PLAUSIBLE_WINDOW:
                best = max(best, val)
    return best


# 探测端点的上下文窗口 token 数：扫描 /models 响应中的 window 字段，探测不到返回 0
async def detect_endpoint_context_window(base_url: str = "", api_key: str = "") -> int:
    base = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    key = (
        api_key
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or ""
    )
    if not base:
        return 0
    best = 0
    for url in _candidate_urls(base):
        try:
            async with httpx.AsyncClient(timeout=5.0) as hc:
                resp = await hc.get(
                    url,
                    headers={"Authorization": f"Bearer {key}", "x-api-key": key},
                )
                resp.raise_for_status()
                best = max(best, _extract_context_window(resp.json()))
        except Exception as e:
            logger.info("context window probe %s failed: %s", url, e)
    if best:
        logger.info("detected context window %d from endpoint", best)
    return best


_MODELS_DEV_URL = "https://models.dev/api.json"
_registry_cache: dict[str, int] | None = None


# 拉取并解析公共模型注册表（models.dev），返回 模型id -> context window；进程内缓存
async def _load_registry() -> dict[str, int]:
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as hc:
        resp = await hc.get(_MODELS_DEV_URL)
        resp.raise_for_status()
        data = resp.json()
    table: dict[str, int] = {}
    for provider_entry in data.values():
        models = provider_entry.get("models") if isinstance(provider_entry, dict) else None
        if not isinstance(models, dict):
            continue
        for mid, info in models.items():
            if not isinstance(info, dict):
                continue
            limit = info.get("limit")
            cw = (limit or {}).get("context") if isinstance(limit, dict) else None
            cw = cw or info.get("context_window")
            if isinstance(cw, int) and not isinstance(cw, bool) and 0 < cw <= _MAX_PLAUSIBLE_WINDOW:
                if cw > table.get(mid, 0):
                    table[mid] = cw
    _registry_cache = table
    logger.info("model registry loaded: %d entries", len(table))
    return table


# 按模型名从公共注册表查询上下文窗口：精确匹配 → 去中转 "provider/" 前缀 → 逐级截尾缀，查不到返回 0
async def detect_registry_context_window(model: str) -> int:
    model = model.strip()
    if not model:
        return 0
    table = await _load_registry()
    if model in table:
        return table[model]
    # 中转常给模型加 "provider/" 前缀，如 "azure/gpt-5.6-sol"
    matches = [cw for mid, cw in table.items() if mid.endswith("/" + model)]
    if matches:
        return max(matches)
    # 最后逐级截掉自定义尾缀重试（如 "gpt-5.6-sol" → "gpt-5.6"），代价是可能命中同名系其他型号
    base = model
    while "-" in base:
        base = base.rsplit("-", 1)[0]
        if base in table:
            logger.info("registry fallback matched trimmed model %r for %r", base, model)
            return table[base]
    return 0
