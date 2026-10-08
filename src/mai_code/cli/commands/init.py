from __future__ import annotations

import asyncio
import getpass
import os
import re
import sys

from mai_code.core.config import GLOBAL_CONFIG_PATH, MaiConfig, get_config, update_llm_toml
from mai_code.core.llm.probe import detect_endpoint_models

_CONFIG_PATH = GLOBAL_CONFIG_PATH


# 是否已完成过 LLM 配置：config 里写了 key，或环境变量里有（.env / shell 均可）
def _is_configured(config: MaiConfig) -> bool:
    return bool(
        config.llm.api_key
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )


# 首次运行守护：未配置 LLM 时自动进入配置向导，返回重新加载的配置。
# stdin 不是终端（脚本/CI）时打印指引后退出，避免挂死。
def ensure_configured() -> MaiConfig:
    config = get_config()
    if _is_configured(config):
        return config
    print("=" * 56)
    print("  检测到 Mai-Code 还没有配置 LLM 端点")
    print("  即将进入首次配置向导（也可以之后运行 Mai init 重新配置）")
    print("=" * 56)
    if not sys.stdin.isatty():
        print(
            "\n非交互环境无法运行向导。请手动创建 "
            f"{_CONFIG_PATH} ：\n\n  [llm]\n"
            '  base_url = "https://api.example.com/anthropic"\n'
            '  api_key = "sk-..."\n'
            '  default_model = "模型名"\n'
        )
        raise SystemExit(1)
    cmd_init(config)
    return get_config()


# 把 [llm] 段写入 ~/.mai/config.toml（实现下沉到 core.config.update_llm_toml，合并式覆盖）


# LLM 配置向导：base_url → api_key（密文输入）→ 探测模型选默认 → 写 config.toml
def cmd_init(config: MaiConfig) -> None:
    if not sys.stdin.isatty():
        print("Mai init 需要交互式终端。")
        raise SystemExit(1)

    print("\nMai-Code LLM 配置向导")
    print("-" * 56)
    print("端点要求：Anthropic Messages API 兼容（官方 / DeepSeek / 各类中转均可）")

    # 1) base_url
    default_base = config.llm.base_url or os.environ.get("ANTHROPIC_BASE_URL") or ""
    hint = f" (直接回车 = {default_base})" if default_base else " (直接回车 = 官方默认端点)"
    base_url = input(f"\n1) API base_url{hint}: ").strip() or default_base

    # 2) api_key
    has_env_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if config.llm.api_key:
        hint = " (直接回车 = 保留已配置的 key)"
    elif has_env_key:
        hint = " (直接回车 = 使用环境变量中的 key)"
    else:
        hint = ""
    api_key = ""
    try:
        api_key = getpass.getpass(f"2) API key（输入不回显）{hint}: ").strip()
    except EOFError:
        # 无 /dev/tty 的环境（tmux/管道/部分 CI）退化为明文输入
        api_key = input("2) API key（明文输入）: ").strip()
    if not api_key and (config.llm.api_key or has_env_key):
        api_key = config.llm.api_key  # 保留旧值；纯环境变量时不写入 toml
    if not api_key:
        print("  ⚠️ 未提供 key，之后需设置 ANTHROPIC_API_KEY 环境变量才能调用")

    # 3) 探测模型列表并选默认模型；探测为空时允许重新输入 base_url，避免错把网页地址当 API 端点
    models: list[str] = []
    while True:
        print("\n正在探测端点可用模型……")
        try:
            models = asyncio.run(detect_endpoint_models(base_url, api_key))
        except Exception as e:  # 探测本身不该让向导崩溃
            print(f"  探测出错：{e}")
            models = []
        if models:
            break
        print("  未探测到模型列表。两种可能：端点不支持 /models，或 base_url 填错")
        print("  （例如 DeepSeek 的 API 端点是 https://api.deepseek.com/anthropic，")
        print("   platform.deepseek.com 是文档站，不是 API）")
        re_url = input("   回车 = 继续手动输入模型名 / 或输入新的 base_url 重新探测: ").strip()
        if not re_url:
            break
        base_url = re_url

    default_model = ""
    if models:
        print(f"  端点共 {len(models)} 个模型：")
        for idx, m in enumerate(models, 1):
            mark = "  ← 当前默认" if m == config.llm.default_model else ""
            print(f"   {idx}. {m}{mark}")
        raw = input(f"选择默认模型 [1-{len(models)}]: ").strip()
        try:
            default_model = models[max(0, int(raw) - 1)] if raw else (config.llm.default_model if config.llm.default_model in models else models[0])
        except ValueError:
            default_model = models[0]
    else:
        print("  未探测到模型列表，手动输入模型名")
        while True:
            default_model = (
                input(f"   default_model (回车 = {config.llm.default_model}): ").strip()
                or config.llm.default_model
            )
            # 模型名形如 deepseek-chat / claude-sonnet-4-6；纯数字或含空格基本是误输入
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9._:/-]*", default_model):
                break
            print('   ⚠️ 模型名不合法（如 "1"），应为 deepseek-chat 这类标识符，请重新输入')

    # 4) 写配置
    entries: list[tuple[str, str]] = [("default_model", default_model)]
    if base_url:
        entries.append(("base_url", base_url))
    if api_key:
        entries.append(("api_key", api_key))
    update_llm_toml(entries)

    print("\n" + "=" * 56)
    print(f"  配置已写入 {_CONFIG_PATH}")
    print(f"  默认模型: {default_model}")
    print(f"  端点:     {base_url or '(官方默认)'}")
    print("  现在可以直接运行 mai-tui 开始使用")
    print("=" * 56 + "\n")
