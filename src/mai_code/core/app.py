from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import UTC
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import anthropic
import httpx
import mai_code
from mai_code.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    PermissionRespondCommand,
    PermissionRespondResult,
    PongResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCompactCommand,
    SessionCompactResult,
    SessionCreateCommand,
    SessionCreateResult,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionSendMessageCommand,
    SessionSendMessageResult,
    SessionSetModelCommand,
    SessionSetModelResult,
)
from mai_code.core.bus.envelope import EventPushEnvelope
from mai_code.core.config import MaiConfig, get_config
from mai_code.core.events.bus import EventBus
from mai_code.core.llm.provider import AnthropicProvider
from mai_code.core.logging_setup import setup_logging
from mai_code.core.mcp.server import McpServerManager
from mai_code.core.permissions.manager import PermissionManager
from mai_code.core.permissions.storage import load_policy_file
from mai_code.core.runner import AgentRunner
from mai_code.core.runs import events_file, new_run_id
from mai_code.core.session import SessionManager, SessionStore
from mai_code.core.trace.record import TraceRecord
from mai_code.core.trace.writer import TraceWriter
from mai_code.core.transport.ipc_broadcaster import IpcEventBroadcaster
from mai_code.core.transport.socket_server import SocketServer, get_connection_writer

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: MaiConfig | None = None
        self._running_runs: set[asyncio.Task[Any]] = set()
        self._sessions: SessionManager | None = None
        self._permission_manager: PermissionManager | None = None
        self._mcp_manager: McpServerManager | None = None
        self._detected_models: list[str] | None = None  # 端点 /v1/models 探测缓存

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=mai_code.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

    # 将 EventBus 事件写入 trace（作为 EventBus 订阅者）
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict,
            )
        )

    # 启动一次 agent run：异步创建 AgentRunner 并立即返回 run_id
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._sessions is not None
        cmd = AgentRunCommand.model_validate(params)
        session = await self._sessions.create(mode="one_shot", title=cmd.goal[:40])
        run_id = new_run_id()
        run_task = asyncio.create_task(
            self._sessions.send_message(session.id, cmd.goal, run_id=run_id)
        )
        self._running_runs.add(run_task)
        run_task.add_done_callback(self._running_runs.discard)
        return AgentRunResult(run_id=run_id)

    # 创建 chat 或 one_shot session，并返回 session_id
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        cmd = SessionCreateCommand.model_validate(params)
        session = await self._sessions.create(mode=cmd.mode, title=cmd.title)
        return SessionCreateResult(session_id=session.id, status=session.status)

    # 向 session 发送一条用户消息并同步等待对应 run 完成
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        cmd = SessionSendMessageCommand.model_validate(params)
        run_id = await self._sessions.send_message(cmd.session_id, cmd.content)
        return SessionSendMessageResult(run_id=run_id)

    # 返回 session 的完整 Anthropic messages 历史
    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        cmd = SessionGetHistoryCommand.model_validate(params)
        messages = await self._sessions.get_history(cmd.session_id)
        return SessionGetHistoryResult(messages=messages)

    # 接收客户端权限审批响应，resolve 对应挂起的 Future
    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        cmd = PermissionRespondCommand.model_validate(params)
        logger.info(
            "permission.respond received tool_use_id=%s decision=%s",
            cmd.tool_use_id, cmd.decision,
        )
        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            return PermissionRespondResult()
        self._permission_manager.respond(cmd.tool_use_id, cmd.decision)
        return PermissionRespondResult()

    # 手动压缩 session thread，将摘要持久化写入 thread.jsonl
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result  # type: ignore[no-any-return]

    # 查询或切换当前 LLM 模型（作用于后续所有 run）
    async def _session_set_model_handler(self, params: dict[str, Any]) -> SessionSetModelResult:
        assert self._config is not None
        cmd = SessionSetModelCommand.model_validate(params)
        if cmd.model:
            self._config.llm.default_model = cmd.model
            logger.info("default model switched to %s", cmd.model)
        # 可切换模型优先取用户白名单（llm.models），否则探测端点 /v1/models，最后退回默认模型
        if self._config.llm.models:
            available = list(self._config.llm.models)
        else:
            detected = await self._detect_endpoint_models()
            available = detected or [self._config.llm.default_model]
        return SessionSetModelResult(
            current_model=self._config.llm.default_model,
            available_models=available,
        )

    # 探测端点支持的模型列表，结果缓存。两种策略：
    # 1) Anthropic SDK 的 /v1/models（官方端点与部分网关）
    # 2) OpenAI 风格 GET {origin}/models、{origin}/v1/models（DeepSeek 等兼容网关）
    # 全部失败返回空列表，调用方回退默认模型
    async def _detect_endpoint_models(self) -> list[str]:
        if self._detected_models is not None:
            return self._detected_models
        assert self._config is not None
        llm = self._config.llm
        base = (llm.base_url or os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
        key = (
            llm.api_key
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
        self._detected_models = models
        logger.info("detected %d models from endpoint", len(models))
        return self._detected_models

    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        await self._sessions.close(cmd.session_id)
        return SessionCloseResult(status="closed")

    # 注册客户端事件订阅，可选先回放 events.jsonl 历史再接收实时流
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()

        replayed_count = 0
        if cmd.replay_from_run is not None:
            replayed_count = await self._replay_events(
                cmd.replay_from_run, writer, cmd.topics
            )

        assert self._broadcaster is not None
        sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

    # 从 events.jsonl 向 writer 回放匹配 topic 的历史事件，返回已回放条数
    async def _replay_events(
        self,
        run_id: str,
        writer: asyncio.StreamWriter,
        topics: list[str],
    ) -> int:
        path = events_file(run_id)
        if not path.exists():
            for candidate in Path("~/.mai/sessions").expanduser().glob(
                f"*/runs/{run_id}/events.jsonl"
            ):
                path = candidate
                break
        if not path.exists():
            return 0

        count = 0
        for line in path.read_text().splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type: str = event.get("type", "")
            if not any(fnmatch.fnmatch(event_type, p) for p in topics):
                continue
            envelope = EventPushEnvelope(event=event)
            writer.write(envelope.model_dump_json().encode() + b"\n")
            count += 1

        if count:
            await writer.drain()
        return count

    # 启动守护进程：加载配置、初始化日志、启动 trace、启动 TCP 服务器，并等待退出信号
    async def run(self) -> None:
        self._start_time = time.monotonic()
        self._config = get_config()
        setup_logging(self._config)

        if self._config.trace.enabled:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(trace_path)
            await self._trace.start()
            self._bus.subscribe(self._trace_event_handler)

        policy_file = Path("~/.mai/policy.toml").expanduser()
        self._permission_manager = PermissionManager(
            policy_file=policy_file,
            timeout_s=self._config.permission.timeout_s,
        )
        logger.info(
            "permission manager: timeout_s=%.1f  persistent=%d entries",
            self._config.permission.timeout_s,
            len(load_policy_file(policy_file)),
        )

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)
        sessions_root = Path("~/.mai/sessions").expanduser()
        store = SessionStore(sessions_root)
        assert self._config is not None
        compact_provider = AnthropicProvider(
            self._config.llm.default_model,
            base_url=self._config.llm.base_url or None,
            api_key=self._config.llm.api_key or None,
        )

        self._mcp_manager = McpServerManager()
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
            await self._mcp_manager.start_all(self._config.mcp.servers)

        self._sessions = SessionManager(
            store,
            runner_factory=lambda: AgentRunner(
                self._config,  # type: ignore[arg-type]
                bus=self._bus,
                trace=self._trace,
                permission_manager=self._permission_manager,
                mcp_manager=self._mcp_manager,
            ),
            bus=self._bus,
            provider=compact_provider,
        )

        server = SocketServer(
            self._config.host,
            self._config.port,
            self._broadcaster,
            trace=self._trace,
        )
        server.register("core.ping", self._ping_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("session.compact", self._session_compact_handler)
        server.register("session.set_model", self._session_set_model_handler)

        addr = await server.start()
        logger.info("mai-core %s listening addr=%s", mai_code.__version__, addr)
        logger.info("config: %s", self._config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        if sys.platform != "win32":
            # Unix / macOS：asyncio 原生支持信号处理器
            loop.add_signal_handler(signal.SIGINT, shutdown.set)
            loop.add_signal_handler(signal.SIGTERM, shutdown.set)
        else:
            # Windows 的 asyncio 不支持 add_signal_handler，改用标准 signal 模块
            # 信号处理器在主线程执行，与事件循环同线程，调用 shutdown.set() 安全
            signal.signal(signal.SIGINT, lambda *_: shutdown.set())
            signal.signal(signal.SIGTERM, lambda *_: shutdown.set())

        await shutdown.wait()

        logger.info("shutting down")
        for run_task in list(self._running_runs):
            run_task.cancel()
        if self._running_runs:
            await asyncio.gather(*self._running_runs, return_exceptions=True)
        if self._mcp_manager is not None:
            await self._mcp_manager.stop_all()
        await server.stop()
        if self._trace is not None:
            await self._trace.stop()


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    asyncio.run(CoreApp().run())
