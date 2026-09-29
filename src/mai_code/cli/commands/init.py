from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

from mai_code.core.config import MaiConfig, get_config
from mai_code.core.llm.probe import detect_endpoint_models

_CONFIG_PATH = Path("~/.mai/config.toml").expanduser()


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


# 把 [llm] 段写入 ~/.mai/config.toml：已有文件则只替换 [llm] 块，其余段原样保留
def _write_llm_toml(entries: list[tuple[str, str]]) -> None:
    def toml_value(v: str) -> str:
        if v.startswith("[") and v.endswith("]"):
            return v  # 已是 TOML 数组字面量
        return json.dumps(v)  # json 字符串转义与 TOML 基本字符串兼容

    new_block = ["[llm]"] + [f"{k} = {toml_value(v)}" for k, v in entries]

    lines = _CONFIG_PATH.read_text(encoding="utf-8").splitlines() if _CONFIG_PATH.exists() else []
    out: list[str] = []
    i, replaced = 0, False
    while i < len(lines):
        if lines[i].strip() == "[llm]":
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("["):
                i += 1
            out.extend(new_block)
            out.append("")
            replaced = True
            continue
        out.append(lines[i])
        i += 1
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.extend(new_block)
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


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

    # 3) 探测模型列表并选默认模型
    print("\n正在探测端点可用模型……")
    try:
        models = asyncio.run(detect_endpoint_models(base_url, api_key))
    except Exception as e:  # 探测本身不该让向导崩溃
        print(f"  探测出错：{e}")
        models = []
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
        print("  未探测到模型列表（端点可能不支持 /models），手动输入模型名")
        default_model = input(f"   default_model (回车 = {config.llm.default_model}): ").strip() or config.llm.default_model

    # 4) 写配置
    entries: list[tuple[str, str]] = [("default_model", default_model)]
    if base_url:
        entries.append(("base_url", base_url))
    if api_key:
        entries.append(("api_key", api_key))
    _write_llm_toml(entries)

    print("\n" + "=" * 56)
    print(f"  配置已写入 {_CONFIG_PATH}")
    print(f"  默认模型: {default_model}")
    print(f"  端点:     {base_url or '(官方默认)'}")
    print("  现在可以直接运行 mai-tui 开始使用")
    print("=" * 56 + "\n")
