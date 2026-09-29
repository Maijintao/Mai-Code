from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, Static, TextArea

from mai_code.core.config import MaiConfig
from mai_code.core.skills.loader import SkillLoader
from mai_code.core.transport.socket_client import IpcError, SocketClient

log = logging.getLogger(__name__)


# 天秤座欢迎态 13×8：整格厚块造型（抗半块字符缝隙），左右完全对称
# 梁/托盘/底座全部 2 像素厚，仅在轮廓边缘用半格收圆角；S 为阴影色
_PIXEL_MASCOT_GRID = (
    "..LLLLLLLLL..",
    "..BBBBBBBBB..",
    "..D...D...D..",
    "..D...D...D..",
    "BBBBB.L.BBBBB",
    ".SSS..D..SSS.",
    "...DDDDDDD...",
    "..SSSSSSSSS..",
)
_PIXEL_MASCOT_COLORS = {
    "D": "#155e75",
    "S": "#0c4a6e",
    "B": "#38bdf8",
    "L": "#e0f2fe",
}


# 用半块字符填满终端单元，让天秤线条连续不出现断层
def _pixel_mascot() -> Text:
    mascot = Text()
    background = "#070b14"
    for row_index in range(0, len(_PIXEL_MASCOT_GRID), 2):
        if row_index:
            mascot.append("\n")
        top = _PIXEL_MASCOT_GRID[row_index]
        bottom = _PIXEL_MASCOT_GRID[row_index + 1] if row_index + 1 < len(_PIXEL_MASCOT_GRID) else "." * len(top)
        for upper, lower in zip(top, bottom, strict=True):
            if upper == lower == ".":
                mascot.append(" ")
                continue
            foreground = _PIXEL_MASCOT_COLORS.get(upper, background)
            backdrop = _PIXEL_MASCOT_COLORS.get(lower, background)
            mascot.append("▀", style=f"{foreground} on {backdrop}")
    return mascot


# Minecraft 风格像素标题：冷白正面 + 克制蓝色右下挤出层
_PIXEL_TITLE_FACE = "#eef3fb"
_PIXEL_TITLE_DEPTH = "#5f7fae"
_PIXEL_TITLE_GLYPHS = {
    "M": ["█...█", "██.██", "█.█.█", "█...█", "█...█"],
    "A": ["..█..", ".█.█.", "█...█", "█████", "█...█"],
    "I": ["███", ".█.", ".█.", ".█.", "███"],
    "-": ["....", "....", "████", "....", "...."],
    "C": [".███.", "█...█", "█....", "█...█", ".███."],
    "O": [".███.", "█...█", "█...█", "█...█", ".███."],
    "D": ["████.", "█...█", "█...█", "█...█", "████."],
    "E": ["█████", "█....", "████.", "█....", "█████"],
}


# 用方块字符逐格拼出 MAI-CODE 立体标题，先铺阴影层再叠正面
def _pixel_title(text: str = "MAI-CODE") -> Text:
    height = 5
    placements, cursor = [], 0
    for ch in text:
        glyph = _PIXEL_TITLE_GLYPHS[ch]
        placements.append((cursor, glyph))
        cursor += len(glyph[0]) + 1
    width = cursor - 1
    canvas: list[list[str | None]] = [[None] * (width + 1) for _ in range(height + 1)]
    for dx, dy in ((1, 1), (1, 0)):
        for gx, glyph in placements:
            for ry, row in enumerate(glyph):
                for rx, cell in enumerate(row):
                    if cell == "█" and canvas[ry + dy][gx + rx + dx] is None:
                        canvas[ry + dy][gx + rx + dx] = _PIXEL_TITLE_DEPTH
    for gx, glyph in placements:
        for ry, row in enumerate(glyph):
            for rx, cell in enumerate(row):
                if cell == "█":
                    canvas[ry][gx + rx] = _PIXEL_TITLE_FACE
    title = Text()
    for row_index, row in enumerate(canvas):
        if row_index:
            title.append("\n")
        for cell in row:
            title.append("█" if cell else " ", style=cell or "")
    return title


# 欢迎面板：对齐 Claude Code 欢迎页排版（左右分栏 + 中缝分割线，宠物暂缺）
class WelcomePanel(Static):
    DEFAULT_CSS = """
    WelcomePanel {
        width: 1fr;
        height: auto;
        padding: 0;
        align: center middle;
        color: #e6ebf3;
    }
    WelcomePanel #welcome-frame {
        width: 100%;
        max-width: 84;
        height: 16;
        border: round #4d83e8;
        background: #070b14;
        padding: 0 2 1 2;
    }
    WelcomePanel #pixel-title {
        width: 1fr;
        height: 6;
        margin-bottom: 0;
        content-align: center middle;
    }
    WelcomePanel #welcome-body {
        width: 1fr;
        height: 10;
    }
    WelcomePanel #intro {
        width: 38%;
        height: 1fr;
        padding: 0 2 0 1;
        border-right: solid #315da9;
        content-align: center top;
        align: center top;
    }
    WelcomePanel #welcome-heading {
        width: 100%;
        height: 1;
        color: #eef3fb;
        text-style: bold;
        text-align: center;
    }
    WelcomePanel #pet {
        width: 100%;
        height: 4;
        content-align: center top;
    }
    WelcomePanel #details {
        width: 1fr;
        height: auto;
        padding: 0 0 0 2;
    }
    WelcomePanel .tip {
        height: 1;
        color: #e6ebf3;
    }
    WelcomePanel .activity-rule {
        margin-top: 1;
        color: #566074;
    }
    WelcomePanel .activity-empty {
        color: #8f9bad;
    }
    WelcomePanel #welcome-meta {
        display: none;
    }
    WelcomePanel .meta {
        height: 1;
        color: #8993a7;
    }
    """

    # 组合欢迎卡片：像素标题居中，左栏标题区（带右分割线），右栏提示与最近活动，底部模型与目录
    def compose(self) -> ComposeResult:
        with Vertical(id="welcome-frame"):
            yield Static(_pixel_title(), id="pixel-title")
            with Horizontal(id="welcome-body"):
                with Vertical(id="intro"):
                    yield Label("Welcome back!", id="welcome-heading")
                    yield Static(_pixel_mascot(), id="pet")
                with Vertical(id="details"):
                    yield Static("›  Type a goal and press Enter", classes="tip")
                    yield Static("›  Type / for skills and commands", classes="tip")
                    yield Static("›  Press Ctrl+Q to exit", classes="tip")
                    yield Static("──────  Recent activity", classes="activity-rule")
                    yield Static("No recent activity", classes="activity-empty")
            with Vertical(id="welcome-meta"):
                yield Static("MAI-CODE · local agent runtime", classes="meta")
                yield Static(_display_cwd(), classes="meta")


# 当前工作目录缩略显示：home 内用 ~ 前缀，过长时只保留末两级目录
def _display_cwd() -> str:
    home = str(Path.home())
    cwd = str(Path.cwd())
    text = f"~{cwd[len(home):]}" if cwd.startswith(home) else cwd
    if len(text) <= 48:
        return text
    parts = [p for p in text.split("/") if p]
    return "…/" + "/".join(parts[-2:])


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s


# 按终端显示宽度截断字符串（CJK 字符按双宽计）
def _display_truncate(s: str, limit: int) -> str:
    width = 0
    for i, ch in enumerate(s):
        width += 2 if ord(ch) > 0x2E80 else 1
        if width > limit:
            return s[:i] + "…"
    return s


# token 数简写：1234 -> 1.2k，1000000 -> 1M
def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(n)




def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, indent=2)


# 从工具参数中提取最适合摘要展示的关键字段
def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "read_file": ("path",),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "bash": ("command",),
        "note_save": ("content",),
    }
    keys = keys_by_tool.get(tool_name, ())
    parts = [f"{key}={params[key]!r}" for key in keys if key in params]
    if not parts:
        parts = [f"{key}={value!r}" for key, value in list(params.items())[:2]]
    return _preview(", ".join(parts), max_len)


class LLMStreamBlock(Static):
    """在同一个 Static widget 中累积 LLM 流式 token。"""

    DEFAULT_CSS = "LLMStreamBlock { padding: 0 2; margin-bottom: 1; color: $text; }"

    # 初始化为空文本块
    def __init__(self) -> None:
        super().__init__("")
        self._text = ""
        self._finalized = False

    # 追加一个 token 并刷新显示
    def append_token(self, token: str) -> None:
        if self._finalized:
            return
        self._text += token
        self.update(self._text)

    # 将累积文本渲染为 Markdown，供流式块结束后显示
    def finalize_markdown(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._text.strip():
            self.update(Markdown(self._text, code_theme="monokai"))


class ToolCallBlock(Widget):
    """可折叠的工具调用块：折叠时显示摘要，点击后展开完整 params 和 output。"""

    DEFAULT_CSS = """
    ToolCallBlock {
        height: auto;
        padding: 0 2;
        color: #6b7a92;
    }
    ToolCallBlock:hover > .summary { color: #dbeafe; }
    ToolCallBlock > .summary { color: #6b7a92; }
    ToolCallBlock > .detail {
        display: none;
        padding: 0 2 0 4;
        color: #8b99ad;
    }
    ToolCallBlock.expanded > .detail { display: block; }
    """

    # 初始化工具调用信息
    def __init__(self, tool_name: str, params: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._params_full = _params_str(params)
        self._output = ""
        self._elapsed_ms = 0
        self._is_error = False
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Static(self._summary(), classes="summary")
        yield Static("", classes="detail")

    # 生成摘要行文本
    def _summary(self) -> str:
        if self._tool_name == "note_save" and self._finished and not self._is_error:
            return f"  [green]remembered[/green]  [dim]{self._elapsed_ms}ms[/dim]"

        params_pre = _param_summary(self._tool_name, self._params)
        line = f"  [dim]tool[/dim] [bold]{self._tool_name}[/bold]"
        if params_pre:
            line += f"  [dim]{params_pre}[/dim]"
        if self._finished:
            color = "red" if self._is_error else "green"
            status = "failed" if self._is_error else "done"
            hint = "  [dim](click to expand)[/dim]" if self._output else ""
            line += f"  [{color}]{status}[/{color}]  [dim]{self._elapsed_ms}ms[/dim]{hint}"
        return line

    # 工具调用完成时更新结果并刷新摘要（widget 未挂载时跳过 DOM 更新）
    def set_result(self, output: str, elapsed_ms: int, *, is_error: bool = False) -> None:
        self._output = output
        self._elapsed_ms = elapsed_ms
        self._is_error = is_error
        self._finished = True
        if self.children:
            self.query_one(".summary", Static).update(self._summary())

    # 点击时切换展开/折叠状态
    def on_click(self) -> None:
        if not self._finished:
            return
        if "expanded" in self.classes:
            self.remove_class("expanded")
        else:
            detail = self.query_one(".detail", Static)
            detail.update(
                f"[dim]params[/dim]\n{self._params_full}\n\n"
                f"[dim]output[/dim]\n{self._output}\n\n"
                f"[dim]elapsed:[/dim] {self._elapsed_ms}ms"
            )
            self.add_class("expanded")


class PermissionSelect(Static):
    """内联权限选择控件：挂载在日志流中，键盘焦点无需 ModalScreen。"""

    can_focus = True

    DEFAULT_CSS = """
    PermissionSelect {
        height: auto;
        padding: 1 2;
        margin: 0 2 1 2;
        border: round #2563eb;
        background: #0b1930;
        color: #dbeafe;
    }
    PermissionSelect:focus {
        border: round #60a5fa;
    }
    """

    _CHOICES: tuple[tuple[str, str, str], ...] = (
        ("allow_once",   "Allow once",   "y / 1"),
        ("always_allow", "Always allow", "a / 2"),
        ("deny_once",    "Deny",         "n / 3"),
        ("always_deny",  "Always deny",  "d / 4"),
    )
    _KEY_MAP: dict[str, str] = {
        "y": "allow_once",  "1": "allow_once",
        "a": "always_allow","2": "always_allow",
        "n": "deny_once",   "3": "deny_once",
        "d": "always_deny", "4": "always_deny",
    }

    # 用户作出权限决策时发布，携带工具 ID 和决策字符串
    class Decided(Message):
        # 初始化决策消息，存储控件引用、工具 ID 和决策
        def __init__(self, widget: PermissionSelect, tool_use_id: str, decision: str) -> None:
            self.widget = widget
            self.tool_use_id = tool_use_id
            self.decision = decision
            super().__init__()

    # 初始化控件，存储工具 ID（用于 IPC 回复）
    def __init__(self, tool_use_id: str) -> None:
        super().__init__("")
        self._tool_use_id = tool_use_id
        self._cursor = 0

    def on_mount(self) -> None:
        self.update(self._render_ui())
        self.focus()
        log.debug(
            "PermissionSelect.on_mount  can_focus=%s  focused_after=%r",
            self.can_focus,
            self.app.focused,
        )
        self.app.call_after_refresh(self._log_deferred_focus)

    # 在下一帧记录焦点是否真正转移到本控件
    def _log_deferred_focus(self) -> None:
        log.debug(
            "PermissionSelect.deferred_focus  app.focused=%r  has_focus=%s  focusable=%s",
            self.app.focused,
            self.has_focus,
            self.focusable,
        )

    # 焦点到达时记录，用于确认 focus() 是否真正生效
    def on_focus(self, event: events.Focus) -> None:
        log.debug(
            "PermissionSelect.on_focus  has_focus=%s  app.focused=%r",
            self.has_focus,
            self.app.focused,
        )

    # 焦点离开时记录，用于追踪是否被其他控件抢走焦点
    def on_blur(self, event: events.Blur) -> None:
        log.debug("PermissionSelect.on_blur  app.focused=%r", self.app.focused)

    # 生成带光标高亮的选项列表文本
    def _render_ui(self) -> str:
        lines: list[str] = []
        for i, (_, label, key_hint) in enumerate(self._CHOICES):
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ {label}[/bold cyan]  [dim]{key_hint}[/dim]")
            else:
                lines.append(f"    {label}  [dim]{key_hint}[/dim]")
        lines.append("[dim]  ↑↓ navigate   enter confirm[/dim]")
        return "\n".join(lines)

    # 方向键导航；快捷键直接选择；enter 确认光标位置
    def on_key(self, event: events.Key) -> None:
        log.debug("PermissionSelect.on_key  key=%r  char=%r", event.key, event.character)
        key = event.key
        if key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key == "enter":
            event.stop()
            self._pick(self._CHOICES[self._cursor][0])
        else:
            decision = self._KEY_MAP.get(key)
            if decision is not None:
                event.stop()
                self._pick(decision)

    # 发布决策消息，由宿主 App 负责 IPC 回复和控件清理
    def _pick(self, decision: str) -> None:
        log.debug("PermissionSelect._pick  decision=%s", decision)
        self.post_message(self.Decided(self, self._tool_use_id, decision))


class PermissionBlock(Static):
    """日志里的权限审批摘要"""

    _LABEL_MAP: dict[str, str] = {
        "allow_once":   "allowed (once)",
        "always_allow": "always allowed",
        "deny_once":    "denied",
        "always_deny":  "always denied",
        "timeout":      "⏱ timed out",
    }
    LABEL_MAP = _LABEL_MAP

    DEFAULT_CSS = """
    PermissionBlock {
        height: auto;
        padding: 1 2;
        margin: 1 2 0 2;
        border: round #7f1d1d;
        background: #21121c;
    }
    """

    # 子类提交消息：用户作出权限决策时发布
    class Resolved(Message):
        def __init__(self, block: PermissionBlock, decision: str) -> None:
            self.block = block
            self.decision = decision
            super().__init__()

    # 初始化审批块，记录工具 ID、名称和参数预览
    def __init__(self, tool_use_id: str, tool_name: str, param_preview: str) -> None:
        self._tool_use_id = tool_use_id
        self._tool_name = tool_name
        self._param_preview = param_preview
        self._resolved = False
        super().__init__(self._pending_text(), classes="log-line")

    def _pending_text(self) -> str:
        preview = f"  [dim]{self._param_preview}[/dim]" if self._param_preview else ""
        return f"[bold red]? permission[/bold red]  [bold]{self._tool_name}[/bold]{preview}"

    # 将块收缩为单行摘要并发布 Resolved 消息
    def _resolve(self, decision: str) -> None:
        if self._resolved:
            return
        self._resolved = True
        allowed = decision in ("allow_once", "always_allow")
        icon = "[bold green]✓[/bold green]" if allowed else "[bold red]✗[/bold red]"
        label = self._LABEL_MAP.get(decision, decision)
        preview = f"  [dim]{self._param_preview}[/dim]" if self._param_preview else ""
        self.update(
            f"{icon} permission  [bold]{self._tool_name}[/bold]{preview}  [dim]{label}[/dim]"
        )
        self.post_message(self.Resolved(self, decision))


class SlashCompleteWidget(Static):
    """斜杠命令自动补全弹出框：输入 / 时显示可用 skill 列表并支持键盘筛选与选择。"""

    can_focus = False

    DEFAULT_CSS = """
    SlashCompleteWidget {
        height: auto;
        max-height: 10;
        padding: 0 1;
        margin: 0 2;
        background: transparent;
        color: #e6ebf3;
        overflow-y: auto;
    }
    """

    # 用户选中某条命令时发布
    class Selected(Message):
        # 初始化，携带被选中的 skill 名称
        def __init__(self, skill_name: str) -> None:
            self.skill_name = skill_name
            super().__init__()

    # 初始化，接收全量 (name, description) 列表
    def __init__(self, items: list[tuple[str, str]]) -> None:
        super().__init__("")
        self._all_items = items
        self._filtered: list[tuple[str, str]] = list(items)
        self._cursor = 0

    # 根据查询字符串筛选列表，重置光标并重新渲染
    def set_query(self, query: str) -> None:
        q = query.lower()
        self._filtered = [(n, d) for n, d in self._all_items if not q or q in n.lower()]
        self._cursor = min(self._cursor, max(0, len(self._filtered) - 1))
        if self.is_attached:
            self._redraw()

    # 向上移动光标并重新渲染
    def move_up(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor - 1) % len(self._filtered)
            self._redraw()

    # 向下移动光标并重新渲染
    def move_down(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor + 1) % len(self._filtered)
            self._redraw()

    # 选中当前光标项并发布 Selected 消息
    def select_current(self) -> None:
        if self._filtered:
            self.post_message(self.Selected(self._filtered[self._cursor][0]))

    # 返回当前是否有可选项
    def has_selection(self) -> bool:
        return len(self._filtered) > 0

    # 返回当前高亮的命令名（无可选项时为空串）
    def selected_name(self) -> str:
        if not self._filtered:
            return ""
        return self._filtered[self._cursor][0]

    def on_mount(self) -> None:
        self._redraw()

    # 渲染筛选后的命令列表：Codex 风格——无框深底、白字、灰描述列对齐，选中行蓝底
    def _redraw(self) -> None:
        if not self._filtered:
            self.update("[dim]  no matching commands[/dim]")
            return
        width = max(len(name) for name, _ in self._filtered)
        lines: list[str] = []
        for i, (name, desc) in enumerate(self._filtered):
            label = f"/{name}".ljust(width + 1)
            desc = _display_truncate(desc, 60)
            if i == self._cursor:
                lines.append(f"[on #2563eb] ❯ [bold]{label}[/bold]{desc} [/on #2563eb]")
            else:
                lines.append(f"   {label} [dim]{desc}[/dim]")
        lines.append("[dim]  ↑↓ navigate   tab/enter select   esc dismiss[/dim]")
        self.update("\n".join(lines))


class ModelSelectWidget(Static):
    """模型选择弹窗：↑/↓ 移动光标、enter 确认切换、esc 取消。"""

    can_focus = True

    DEFAULT_CSS = """
    ModelSelectWidget {
        height: auto;
        max-height: 10;
        padding: 0 1;
        margin: 0 2;
        background: transparent;
        color: #e6ebf3;
    }
    """

    class Selected(Message):
        def __init__(self, model: str) -> None:
            self.model = model
            super().__init__()

    class Cancelled(Message):
        pass

    def __init__(self, models: list[str], current: str) -> None:
        super().__init__("")
        self._models = models
        self._current = current
        self._cursor = models.index(current) if current in models else 0

    def on_mount(self) -> None:
        self._redraw()

    def _redraw(self) -> None:
        width = max(len(m) for m in self._models)
        lines = []
        for i, m in enumerate(self._models):
            mark = "✓" if m == self._current else " "
            label = f"{mark} {m}".ljust(width + 3)
            if i == self._cursor:
                lines.append(f"[on #2563eb] ❯ [bold]{label}[/bold] [/on #2563eb]")
            else:
                lines.append(f"   {label}")
        lines.append("[dim]  ↑/↓ select · enter confirm · esc cancel[/dim]")
        self.update("\n".join(lines))

    def _on_key(self, event: events.Key) -> None:
        key = event.key
        if key in ("up", "k"):
            event.stop()
            event.prevent_default()
            self._cursor = (self._cursor - 1) % len(self._models)
            self._redraw()
        elif key in ("down", "j"):
            event.stop()
            event.prevent_default()
            self._cursor = (self._cursor + 1) % len(self._models)
            self._redraw()
        elif key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Selected(self._models[self._cursor]))
        elif key == "escape":
            event.stop()
            event.prevent_default()
            self.post_message(self.Cancelled())


class ChatTextArea(TextArea):
    """支持 Enter 提交、Cmd/Shift/Alt+Enter 换行的多行聊天输入框。"""

    DEFAULT_CSS = """
    ChatTextArea {
        height: auto;
        min-height: 3;
        max-height: 12;
        border: round $surface-lighten-2;
        background: $background;
        padding: 0 1;
        margin: 1 2;
        scrollbar-size-vertical: 1;
    }
    ChatTextArea:focus {
        border: round $accent;
        background: $background;
    }
    """

    # 子类自定义的提交消息，供宿主 App 监听
    class Submitted(Message):
        def __init__(self, area: ChatTextArea) -> None:
            self.text_area = area
            self.value = area.text
            super().__init__()

    # 输入内容以 / 开头且无空格时发布，query 为 / 之后的字符串（可为空串）；None 表示收起弹窗
    class SlashChanged(Message):
        def __init__(self, query: str | None) -> None:
            self.query = query
            super().__init__()

    # 文本变化时检测 / 前缀，通知宿主 App 更新自动补全弹窗
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/") and " " not in text:
            self.post_message(ChatTextArea.SlashChanged(query=text[1:]))
        else:
            self.post_message(ChatTextArea.SlashChanged(query=None))

    # Enter 提交；↑↓/Tab/Esc 路由到自动补全弹窗；Cmd/Shift/Alt+Enter 插入换行；其余键交回 TextArea
    async def _on_key(self, event: events.Key) -> None:
        key = event.key

        popup: SlashCompleteWidget | None = None
        try:
            popup = self.app.query_one(SlashCompleteWidget)
        except NoMatches:
            popup = None

        if key == "enter":
            event.stop()
            event.prevent_default()
            if popup is not None and popup.has_selection():
                # 已输入完整命令时直接提交，否则先补全
                if self.text.strip() != f"/{popup.selected_name()}":
                    popup.select_current()
                    return
            if self.text.strip():
                self.post_message(self.Submitted(self))
            return
        if key in ("alt+enter", "shift+enter", "ctrl+j", "super+enter"):
            event.stop()
            event.prevent_default()
            if not self.read_only:
                self.insert("\n")
            return
        if popup is not None:
            if key == "up":
                event.stop()
                event.prevent_default()
                popup.move_up()
                return
            elif key == "down":
                event.stop()
                event.prevent_default()
                popup.move_down()
                return
            elif key == "tab":
                event.stop()
                event.prevent_default()
                popup.select_current()
                return
            elif key == "escape":
                event.stop()
                event.prevent_default()
                self.post_message(ChatTextArea.SlashChanged(query=None))
                return
        await super()._on_key(event)


class SmoothLogView(VerticalScroll):
    """日志滚动容器：滚轮滚动带缓动动画，减少生硬跳行感。"""

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if event.ctrl or event.shift:
            super()._on_mouse_scroll_down(event)  # 横向滚动保持原行为
            return
        event.stop()
        self.scroll_relative(y=self.app.scroll_sensitivity_y, animate=True, duration=0.3)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if event.ctrl or event.shift:
            super()._on_mouse_scroll_up(event)
            return
        event.stop()
        self.scroll_relative(y=-self.app.scroll_sensitivity_y, animate=True, duration=0.3)


class MaiTuiApp(App[None]):
    """MaiCode TUI：蓝色像素风终端界面，实时展示 agent 执行过程。"""

    TITLE = "MaiCode"
    BINDINGS = [
        Binding("ctrl+q", "quit", "quit"),
    ]
    CSS = """
    Screen { background: #07090e; }
    #header {
        height: 3;
        background: #07090e;
        color: #e6ebf3;
        padding: 1 2 0 2;
        border-bottom: solid #315da9;
    }
    #log-view {
        height: 1fr;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 1;
        background: #07090e;
    }
    #log-view > * {
        max-width: 96;
    }
    Static.user-turn {
        color: #e6ebf3;
        padding: 1 2 0 2;
        border-left: solid #2563eb;
        margin: 1 2 0 2;
    }
    Static.run-header { color: #6b7a92; padding: 1 2 0 2; margin-top: 1; }
    Static.step-divider { color: #566074; padding: 0 2; }
    Static.run-ok { color: #4ade80; padding: 0 2 1 2; }
    Static.run-err { color: #f87171; padding: 0 2 1 2; }
    Static.usage { padding: 0 2; color: #566074; }
    Static.log-line { padding: 0 2; }
    #prompt {
        height: auto;
        min-height: 3;
        max-height: 12;
        margin: 0 2;
        border: round #4d83e8;
        background: #070b14;
        color: #e6ebf3;
        padding: 0 1;
    }
    #prompt:focus {
        border: round #77a8ff;
        background: #070b14;
    }
    #prompt:disabled {
        border: round #315da9;
        color: #8993a7;
    }
    #footer {
        height: 2;
        margin: 0 2;
        color: #566074;
        layout: horizontal;
        align-vertical: middle;
    }
    #footer-left {
        width: 1fr;
        color: #566074;
    }
    #footer-right {
        width: auto;
        color: #566074;
    }
    #footer-right {
        text-align: right;
    }
    """

    # 初始化连接参数和 TUI 内部状态
    def __init__(self, host: str, port: int, replay_run_id: str | None = None) -> None:
        super().__init__()
        self.scroll_sensitivity_y = 1.0  # 滚轮每 tick 1 行（默认 2 行，太灵敏）
        self._host = host
        self._port = port
        self._replay_run_id = replay_run_id
        self._client: SocketClient | None = None
        self._current_llm: LLMStreamBlock | None = None
        self._pending_tool_blocks: dict[str, ToolCallBlock] = {}
        self._pending_permission_blocks: dict[str, PermissionBlock] = {}
        self._session_id: str | None = None
        self._busy = False
        self._last_context_pct: float = 0.0
        self._model: str | None = None
        self._ctx_tokens: int = 0
        self._ctx_window: int = 0
        self._run_started_at: float | None = None
        self._last_run_elapsed: float | None = None
        self._slash_items: list[tuple[str, str]] = []
        self._subagent_run_ids: dict[str, str] = {}  # child run_id -> description
        self._subagent_start_times: dict[str, float] = {}  # child run_id -> start time

    def compose(self) -> ComposeResult:
        yield Label(
            "[bold #eef3fb]MAI-CODE[/bold #eef3fb]  "
            "[#8fb7e8]◆[/#8fb7e8] [dim]connecting...[/dim]",
            id="header",
        )
        yield SmoothLogView(id="log-view")
        yield ChatTextArea(
            id="prompt",
            show_line_numbers=False,
            placeholder="Ask MAI-CODE anything…",
        )
        yield Horizontal(
            Label("", id="footer-left"),
            Label("Ctrl+Q  quit", id="footer-right"),
            id="footer",
        )

    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        self._append(WelcomePanel())
        self.set_interval(1.0, self._update_footer)
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = True
        prompt.border_title = "type a message · enter to send · ⌘/⇧/⌥ + enter for newline"

    # 构建斜杠命令候选列表：内建命令 + 所有已注册 skill
    def _build_slash_items(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = [
            ("compact", "compress context window"),
            ("model", "show or switch LLM model"),
        ]
        try:
            loader = SkillLoader()
            for skill in loader.list_all_skills():
                desc = skill.description.splitlines()[0] if skill.description else ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                items.append((skill.name, desc))
        except Exception:
            pass
        return items

    # 根据 / 前缀查询字符串挂载、更新或移除自动补全弹窗
    def on_chat_text_area_slash_changed(self, event: ChatTextArea.SlashChanged) -> None:
        query = event.query
        if query is None:
            try:
                self.query_one(SlashCompleteWidget).remove()
            except NoMatches:
                pass
            return
        try:
            popup = self.query_one(SlashCompleteWidget)
            popup.set_query(query)
        except NoMatches:
            popup = SlashCompleteWidget(self._slash_items)
            self.mount(popup, before="#prompt")
            popup.set_query(query)

    # 用户选中自动补全项后将 /{name} 填入输入框并移除弹窗
    def on_slash_complete_widget_selected(self, event: SlashCompleteWidget.Selected) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.text = f"/{event.skill_name} "
            prompt.move_cursor(prompt.document.end)
        try:
            self.query_one(SlashCompleteWidget).remove()
        except NoMatches:
            pass

    # 记录按键焦点；当 PermissionSelect 失去焦点后作为兜底处理权限快捷键
    def on_key(self, event: events.Key) -> None:
        log.debug("App.on_key  key=%r  focused=%r", event.key, self.focused)
        if not self._pending_permission_blocks:
            return
        try:
            select = self.query_one(PermissionSelect)
            if select.has_focus:
                return  # PermissionSelect 有焦点时自行处理，事件不会冒泡到这里
            key = event.key
            decision = PermissionSelect._KEY_MAP.get(key)
            if decision:
                event.stop()
                select._pick(decision)
            elif key in ("up", "k"):
                event.stop()
                select._cursor = (select._cursor - 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())
            elif key in ("down", "j"):
                event.stop()
                select._cursor = (select._cursor + 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())
            elif key == "enter":
                event.stop()
                select._pick(PermissionSelect._CHOICES[select._cursor][0])
        except Exception:
            pass

    # 退出前尽力关闭当前 session，失败也不阻塞 TUI 退出
    async def action_quit(self) -> None:
        if self._client is not None and self._session_id is not None:
            try:
                await self._client.send_command("session.close", {"session_id": self._session_id})
            except (IpcError, RuntimeError, OSError):
                self._append(Static("[yellow]warning: failed to close session[/yellow]"))
        self.exit()

    # 将输入框提交内容发送给当前 chat session；用 worker 发送，避免 await 阻塞 App 消息泵
    async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        content = event.value.strip()
        if not content:
            return
        # 检测 /compact 指令
        if content == "/compact":
            event.text_area.text = ""
            if self._client is not None and self._session_id is not None and not self._busy:
                self.run_worker(self._do_compact(), name="compact", exclusive=False)
            return
        # 检测 /model 指令：无参数弹选择器，带参数直接切换
        parts = content.split(None, 1)
        if parts[0] == "/model":
            event.text_area.text = ""
            arg = parts[1].strip() if len(parts) > 1 else ""
            if arg:
                self.run_worker(self._do_set_model(arg), name="set_model", exclusive=False)
            else:
                self.run_worker(self._do_open_model_picker(), name="model_picker", exclusive=False)
            return
        if self._client is None or self._session_id is None or self._busy:
            self._append(Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line"))
            return
        self._busy = True
        prompt = event.text_area
        prompt.text = ""
        prompt.disabled = True
        prompt.read_only = False
        prompt.border_title = "agent is working..."
        self._append(Static(f"[bold]>[/bold] {content}", classes="user-turn"))
        self._update_header("running")
        self.run_worker(self._do_send_message(content), name="send_message", exclusive=False)

    # 在 worker 中执行手动压缩命令，完成后显示结果横幅
    async def _do_compact(self) -> None:
        if self._client is None or self._session_id is None:
            return
        self._append(Static("[dim]⚡ compacting context...[/dim]", classes="log-line"))
        try:
            result = await self._client.send_command(
                "session.compact",
                {"session_id": self._session_id, "focus": ""},
            )
            summary_tokens = result.get("summary_tokens", 0)
            saved_tokens = result.get("saved_tokens", 0)
            self._last_context_pct = 0.0
            self._ctx_tokens = 0
            self._append(Static(
                f"[bold cyan]⚡ Context compacted[/bold cyan]"
                f"  [dim]summary={summary_tokens} tokens  saved≈{saved_tokens} tokens[/dim]",
                classes="log-line",
            ))
        except (IpcError, RuntimeError, OSError) as e:
            self._append(Static(f"[red]compact error: {e}[/red]", classes="log-line"))

    # 拉取模型列表并弹出选择器（/model 无参数时）
    async def _do_open_model_picker(self) -> None:
        if self._client is None:
            self._append(Static("[yellow]disconnected[/yellow]", classes="log-line"))
            return
        try:
            result = await self._client.send_command("session.set_model", {"model": ""})
        except (IpcError, RuntimeError, OSError) as e:
            self._append(Static(f"[red]model error: {e}[/red]", classes="log-line"))
            return
        current = str(result.get("current_model", "?"))
        available = [str(m) for m in result.get("available_models", [])]
        self._model = current
        self._update_footer()
        if not available:
            self._append(Static(
                f"[bold]current model[/bold]  [cyan]{current}[/cyan]"
                f"  [dim](no other models registered)[/dim]",
                classes="log-line",
            ))
            return
        if current not in available:
            available = [current] + available
        try:
            self.query_one(ModelSelectWidget).remove()
        except NoMatches:
            pass
        picker = ModelSelectWidget(available, current)
        self.mount(picker, before="#prompt")
        picker.focus()

    # 模型选择器：确认切换
    def on_model_select_widget_selected(self, event: ModelSelectWidget.Selected) -> None:
        self._dismiss_model_select()
        self.run_worker(self._do_set_model(event.model), name="set_model", exclusive=False)

    # 模型选择器：取消
    def on_model_select_widget_cancelled(self, event: ModelSelectWidget.Cancelled) -> None:
        self._dismiss_model_select()

    # 移除选择器并把焦点还给输入框
    def _dismiss_model_select(self) -> None:
        try:
            self.query_one(ModelSelectWidget).remove()
        except NoMatches:
            pass
        prompt = self._prompt()
        if prompt is not None and not prompt.disabled:
            prompt.focus()

    # 查询或切换模型，结果直接打到日志区
    async def _do_set_model(self, model: str) -> None:
        if self._client is None:
            self._append(Static("[yellow]disconnected[/yellow]", classes="log-line"))
            return
        try:
            result = await self._client.send_command("session.set_model", {"model": model})
        except (IpcError, RuntimeError, OSError) as e:
            self._append(Static(f"[red]model error: {e}[/red]", classes="log-line"))
            return
        current = str(result.get("current_model", "?"))
        available = [str(m) for m in result.get("available_models", [])]
        self._model = current
        self._update_footer()
        prompt = self._prompt()
        if prompt is not None and not prompt.disabled:
            prompt.focus()
        title = "model switched" if model else "current model"
        suffix = f"  [dim]available: {', '.join(available)}[/dim]" if available else ""
        self._append(Static(
            f"[bold green]✓ {title}[/bold green]  [cyan]{current}[/cyan]{suffix}",
            classes="log-line",
        ))

    # 在 worker 中执行 IPC 发送，使 App 消息泵在 agent 运行期间仍能处理键盘/焦点等消息
    async def _do_send_message(self, content: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.send_command(
                "session.send_message",
                {"session_id": self._session_id, "content": content},
            )
        except (IpcError, RuntimeError, OSError) as e:
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
            self._update_header("ready")
            self._append(Static(f"[red]send error: {e}[/red]", classes="log-line"))

    # 处理内联审批控件的用户决策：发送 IPC 响应并恢复输入框
    async def on_permission_select_decided(self, msg: PermissionSelect.Decided) -> None:
        tool_use_id = msg.tool_use_id
        decision = msg.decision
        log.info("permission decided tool_use_id=%s decision=%s", tool_use_id, decision)
        try:
            msg.widget.remove()
            perm_block = self._pending_permission_blocks.pop(tool_use_id, None)
            if perm_block is not None:
                perm_block._resolve(decision)
            if self._client is not None:
                try:
                    await self._client.send_command(
                        "permission.respond",
                        {"tool_use_id": tool_use_id, "decision": decision},
                    )
                except (IpcError, RuntimeError, OSError):
                    pass
            if not self._pending_permission_blocks:
                p = self._prompt()
                if p is not None:
                    p.disabled = False
                    p.read_only = False
                    p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    p.focus()
        except Exception:
            log.exception("on_permission_select_decided failed tool_use_id=%s", tool_use_id)

    # 向日志视图追加一个 widget 并滚动到底部
    def _append(self, widget: Widget) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.mount(widget)
        log_view.scroll_end(animate=False)

    # 结束当前 LLM 流式块（下一个 token 将开启新块）
    def _break_llm(self) -> None:
        if self._current_llm is not None:
            self._current_llm.finalize_markdown()
        self._current_llm = None

    # 将选择控件挂载到 Screen 顶层（#prompt 之前），避免 VerticalScroll 争抢焦点
    def _mount_permission_select(self, select: PermissionSelect) -> None:
        self.mount(select, before="#prompt")

    # 安全获取输入框，便于组件测试中未挂载时跳过 UI 操作
    def _prompt(self) -> ChatTextArea | None:
        try:
            return self.query_one("#prompt", ChatTextArea)
        except Exception:
            return None

    # 底部状态栏文案：模型 / 上下文占用 / 本次运行耗时
    def _footer_status(self) -> str:
        model = self._model or "no model"
        pct = self._last_context_pct
        filled = int(pct * 10)
        if pct >= 0.85:
            bar_color = "bold red"
        elif pct >= 0.70:
            bar_color = "yellow"
        else:
            bar_color = "#566074"
        bar = f"[{bar_color}]{'█' * filled}{'░' * (10 - filled)}[/{bar_color}]"
        if self._ctx_window:
            ctx = (
                f"[dim]{_fmt_tokens(self._ctx_tokens)}/{_fmt_tokens(self._ctx_window)}"
                f"[/dim] {bar} [dim]{pct * 100:.0f}%[/dim]"
            )
        elif pct > 0:
            ctx = f"{bar} [dim]{pct * 100:.0f}%[/dim]"
        else:
            ctx = "[dim]ctx —[/dim]"
        timer = ""
        if self._busy and self._run_started_at is not None:
            elapsed = int(time.monotonic() - self._run_started_at)
            m, s = divmod(elapsed, 60)
            timer = f" │ [cyan]⏱ {m}m{s:02d}s[/cyan]" if m else f" │ [cyan]⏱ {s}s[/cyan]"
        elif self._last_run_elapsed is not None:
            elapsed = int(self._last_run_elapsed)
            m, s = divmod(elapsed, 60)
            timer = f" │ [dim]○ {m}m{s:02d}s[/dim]" if m else f" │ [dim]○ {s}s[/dim]"
        return f"[#8fb7e8]✳[/#8fb7e8] [bold #eef3fb]{model}[/bold #eef3fb] │ {ctx}{timer}"

    # 刷新底部状态栏（由 1s 定时器和关键事件驱动）
    def _update_footer(self) -> None:
        try:
            self.query_one("#footer-left", Label).update(self._footer_status())
        except NoMatches:
            return

    # 生成 context 占用率的彩色进度条字符串
    def _render_ctx_bar(self, pct: float) -> str:
        filled = int(pct * 20)
        bar = "█" * filled + "░" * (20 - filled)
        label = f"ctx:{pct * 100:.1f}%"
        if pct >= 0.85:
            color = "bold red"
        elif pct >= 0.70:
            color = "yellow"
        else:
            color = "dim"
        return f"[{color}]{label} {bar}[/{color}]"

    # 根据连接和运行状态刷新顶部标题
    def _update_header(self, state: str) -> None:
        try:
            header = self.query_one("#header", Label)
        except NoMatches:
            return
        color = {
            "ready": "#79d6a5",
            "running": "#f2c36b",
            "disconnected": "#e57f8a",
            "connecting": "#94a3b8",
        }.get(state, "dim")
        header.update(
            f"[bold #eef3fb]MAI-CODE[/bold #eef3fb]  "
            f"[{color}]● {state}[/{color}]"
        )

    # 管理 SocketClient 生命周期：连接、订阅事件、断线重连
    async def _socket_loop(self) -> None:
        header = self.query_one("#header", Label)

        while True:
            client = SocketClient(self._host, self._port)
            self._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                log.warning("connection refused %s:%s, retrying", self._host, self._port)
                self._update_header("disconnected")
                await asyncio.sleep(2)
                continue

            log.info("connected to %s:%s", self._host, self._port)
            self._client = client
            self._update_header("connecting")
            loop_task = asyncio.create_task(client.run_event_loop())

            async def on_event(event: dict[str, Any]) -> None:
                self._handle_event(event)

            client.on_event(on_event)

            try:
                loop_task.add_done_callback(
                    lambda t: log.error("loop_task failed: %s", t.exception())
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
                params: dict[str, Any] = {
                    "topics": [
                        "session.*",
                        "run.*",
                        "step.*",
                        "tool.*",
                        "llm.*",
                        "log.*",
                        "permission.*",
                        "context.*",
                        "subagent.*",
                        "skill.*",
                    ],
                    "scope": "global",
                }
                if self._replay_run_id is not None:
                    params["replay_from_run"] = self._replay_run_id
                await client.send_command("event.subscribe", params)
                created = await client.send_command("session.create", {"mode": "chat"})
                self._session_id = str(created["session_id"])
                log.info("session created session_id=%s", self._session_id)
                # 启动即查询当前模型，footer 不再显示 no model
                try:
                    info = await client.send_command("session.set_model", {"model": ""})
                    model = str(info.get("current_model") or "")
                    if model:
                        self._model = model
                        self._update_footer()
                except (IpcError, RuntimeError, OSError):
                    pass
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = False
                    prompt.read_only = False
                    prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    prompt.focus()
                self._update_header("ready")
                await loop_task
            except IpcError as e:
                header.update(f"[bold]MAI-CODE[/bold]  [red]subscribe error: {e}[/red]")
            finally:
                if not loop_task.done():
                    loop_task.cancel()
                self._client = None
                self._session_id = None
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = True
                    prompt.read_only = False
                    prompt.border_title = "disconnected, retrying..."
                self._break_llm()
                await client.close()

            self._update_header("disconnected")
            await asyncio.sleep(2)

    # 根据事件 type 路由到对应渲染逻辑；捕获异常防止 socket loop 因单个事件崩溃
    def _handle_event(self, event: dict[str, Any]) -> None:
        try:
            self._handle_event_inner(event)
        except Exception:
            log.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))

    # 实际的事件路由逻辑
    def _handle_event_inner(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "llm.token":
            token = event.get("token", "")
            if self._current_llm is None:
                llm_block = LLMStreamBlock()
                self._append(llm_block)
                self._current_llm = llm_block
            self._current_llm.append_token(token)
            return

        self._break_llm()

        if t == "session.waiting_for_input":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                prompt.focus()
            self._update_header("ready")

        elif t == "session.closed":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.read_only = False
                prompt.border_title = "session closed"
            self._update_header("disconnected")

        elif t == "run.started":
            goal = event.get("goal", "")
            self._run_started_at = time.monotonic()
            self._last_run_elapsed = None
            self._update_footer()
            if goal:
                self._append(Static(f"[dim]▸ {_preview(goal, 88)}[/dim]", classes="run-header"))

        elif t == "skill.invoked":
            skill_name = event.get("skill_name", "")
            arguments = event.get("arguments", "")
            args_preview = _preview(arguments, 80) if arguments else ""
            args_part = f"  [dim]{args_preview}[/dim]" if args_preview else ""
            self._append(Static(
                f"[bold cyan]/{skill_name}[/bold cyan]{args_part}",
                classes="log-line",
            ))

        elif t == "subagent.started":
            run_id = event.get("run_id", "")
            description = event.get("description", "")
            self._subagent_run_ids[run_id] = description
            self._subagent_start_times[run_id] = time.monotonic()
            short_id = run_id[:8] if len(run_id) >= 8 else run_id
            self._append(Static(
                f"[dim]┌─[/dim] [cyan]{_preview(description, 72)}[/cyan]  [dim]{short_id}[/dim]",
                classes="log-line",
            ))

        elif t == "subagent.finished":
            run_id = event.get("run_id", "")
            status = event.get("status", "")
            description = self._subagent_run_ids.pop(run_id, event.get("description", ""))
            start = self._subagent_start_times.pop(run_id, None)
            elapsed = f"  [dim]{time.monotonic() - start:.1f}s[/dim]" if start is not None else ""
            desc_part = f"[cyan]{_preview(description, 72)}[/cyan]{elapsed}"
            if status == "success":
                self._append(Static(
                    f"[dim]└─[/dim] [bold green]✓[/bold green] {desc_part}",
                    classes="log-line",
                ))
            else:
                self._append(Static(
                    f"[dim]└─[/dim] [bold red]✗[/bold red] {desc_part}",
                    classes="log-line",
                ))

        elif t == "step.started":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            step = event.get("step", "")
            self._append(Static(
                f"[dim]step {step}[/dim]",
                classes="step-divider",
            ))

        elif t == "tool.call_started":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            params = event.get("params") or {}
            run_id = event.get("run_id", "")
            tc_block = ToolCallBlock(tool_name, params)
            if run_id in self._subagent_run_ids:
                tc_block.styles.padding = (0, 2, 0, 6)
            self._pending_tool_blocks[tool_use_id] = tc_block
            self._append(tc_block)

        elif t == "tool.call_finished":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            output = str(event.get("output") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(output, elapsed_ms)

        elif t == "tool.call_failed":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            error_msg = str(event.get("error_message") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(error_msg, elapsed_ms, is_error=True)

        elif t == "run.finished":
            if self._run_started_at is not None:
                self._last_run_elapsed = time.monotonic() - self._run_started_at
            self._update_footer()
            status = event.get("status", "")
            steps = event.get("steps", 0)
            reason = event.get("reason") or ""
            if status == "success":
                self._append(Static(
                    f"[bold green]✓ completed[/bold green]  [dim]{steps} steps[/dim]",
                    classes="run-ok",
                ))
            else:
                detail = f"  [dim]{reason}[/dim]" if reason else ""
                self._append(Static(
                    f"[bold red]✗ failed[/bold red]{detail}  [dim]{steps} steps[/dim]",
                    classes="run-err",
                ))

        elif t == "llm.model_selected":
            model = event.get("model", "")
            if model:
                self._model = model
                self._update_footer()

        elif t == "llm.usage":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            pct = float(event.get("context_pct") or 0.0)
            self._last_context_pct = pct
            self._ctx_tokens = int(event.get("context_tokens") or 0)
            self._ctx_window = int(event.get("context_window") or 0)
            self._update_footer()
            if pct < 0.70:
                return
            self._append(Static(
                f"[dim]ctx {pct * 100:.0f}%[/dim]  {self._render_ctx_bar(pct)}",
                classes="usage",
            ))

        elif t == "context.compacted":
            orig = event.get("original_tokens", 0)
            summary = event.get("summary_tokens", 0)
            self._last_context_pct = 0.0
            self._ctx_tokens = 0
            self._append(Static(
                f"[bold cyan]⚡ Context compacted[/bold cyan]"
                f"  [dim]original≈{orig} tokens → summary={summary} tokens[/dim]",
                classes="log-line",
            ))

        elif t == "permission.requested":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            try:
                _focused_repr = repr(self.focused)
            except Exception:
                _focused_repr = "?"
            log.info(
                "permission.requested tool=%s id=%s  app.focused=%s",
                tool_name, tool_use_id, _focused_repr,
            )
            perm_block = PermissionBlock(tool_use_id, tool_name, param_preview)
            self._pending_permission_blocks[tool_use_id] = perm_block
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.border_title = "permission required"
            self._append(perm_block)
            select = PermissionSelect(tool_use_id)
            self._mount_permission_select(select)
            log.debug(
                "PermissionSelect mounted before #prompt  pending=%d",
                len(self._pending_permission_blocks),
            )

        elif t == "permission.denied":
            # 处理超时或断连触发的 deny，用户主动 deny 已在决策处理器中完成
            tool_use_id = str(event.get("tool_use_id", ""))
            decision = str(event.get("decision", "denied"))
            if tool_use_id in self._pending_permission_blocks:
                perm_block = self._pending_permission_blocks.pop(tool_use_id)
                perm_block._resolve(decision)
                try:
                    select = self.query_one(PermissionSelect)
                    select.remove()
                except Exception:
                    pass
                if not self._pending_permission_blocks:
                    p = self._prompt()
                    if p is not None:
                        p.disabled = False
                        p.read_only = False
                        p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                        p.focus()

        elif t == "log.line":
            level = event.get("level", "INFO")
            color = "bold red" if level == "ERROR" else ("yellow" if level == "WARNING" else "dim")
            self._append(Static(
                f"[{color}]{level}[/{color}]  "
                f"[dim]{event.get('source', '')}[/dim]  {event.get('message', '')}",
                classes="log-line",
            ))


# TUI 入口：读取配置并启动 MaiTuiApp
def run(config: MaiConfig, replay_run_id: str | None = None) -> None:
    app = MaiTuiApp(config.host, config.port, replay_run_id=replay_run_id)
    app.run()
