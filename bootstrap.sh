#!/usr/bin/env bash
# Mai-Code 一键引导：clone 后运行 `bash bootstrap.sh` 即可完成 安装 + 配置 + 启动。
# 之后日常使用仓库根目录的 ./mai-tui（TUI）或 ./mai（CLI）。
set -euo pipefail
cd "$(dirname "$0")"

NO_LAUNCH=0
[ "${1:-}" = "--no-launch" ] && NO_LAUNCH=1

# ---- 1) 找一个 >= 3.12 的 Python ----
PY=""
for cand in python3.13 python3.12 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      PY="$cand"
      break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "❌ 未找到 Python >= 3.12，请先安装（macOS: brew install python@3.12）"
  exit 1
fi
echo "✓ 使用 $PY ($("$PY" -V 2>&1))"

# ---- 2) 建虚拟环境并安装 ----
if [ ! -x .venv/bin/python ]; then
  echo "→ 创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
fi
# uv 建的 venv 可能没有 pip，先补；实在不行用 uv 兜底安装
if [ ! -x .venv/bin/pip ]; then
  .venv/bin/python -m ensurepip -q 2>/dev/null || true
fi
UV_BIN="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV_BIN" ]; then
  for u in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    [ -x "$u" ] && UV_BIN="$u" && break
  done
fi
echo "→ 安装 MaiCode ..."
if [ -x .venv/bin/pip ]; then
  .venv/bin/pip install --upgrade pip -q
  .venv/bin/pip install . -q
elif [ -n "$UV_BIN" ]; then
  "$UV_BIN" pip install --python .venv/bin/python . -q
else
  echo "❌ venv 里没有 pip 且未安装 uv，无法安装依赖"
  exit 1
fi
echo "✓ 安装完成"

# ---- 3) 启动（未配置 LLM 时 mai-tui 会自动进入配置向导）----
if [ "$NO_LAUNCH" -eq 1 ]; then
  echo "✓ 完成。日常使用：./mai-tui（TUI）或 ./mai（CLI）"
  exit 0
fi
exec .venv/bin/mai-tui
