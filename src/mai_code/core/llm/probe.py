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
        origin = re.sub(r"/anthropic$", "", base)
        for url in (f"{origin}/models", f"{origin}/v1/models", f"{base}/v1/models"):
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
