# Mai-Code Desktop（Electron 桌面端）

Mai-Code 的桌面客户端。**Python 后端几乎零改动**——桌面端是继 CLI / TUI 之后的第三个客户端，走同一条 JSON-RPC 2.0 over NDJSON / TCP 协议（见仓库根目录 [WIRE_PROTOCOL.md](../WIRE_PROTOCOL.md)）；唯一的后端新增是 `config.probe_llm` 命令（复用 `core/llm/probe.py` 的端点探测），供设置面板"验证"使用。

```
desktop/
  main/       主进程
    main.js     窗口、IPC、文件夹选择、直连端点探测兜底
    daemon.js   DaemonManager：每工作区一个 daemon 连接
    preload.cjs contextBridge（renderer 无 Node、有 CSP）
  renderer/   渲染层：无构建步骤的原生 HTML/CSS/JS
    app.js      工作区、事件流、权限审批、设置、像素品牌（数据取自 TUI 源码）
```

## 运行（开发模式）

```bash
cd desktop
npm install        # 首次；Electron 二进制走 npmmirror 可加速：
                   # ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ node node_modules/electron/install.js
npm start
```

## 功能

- **工作区隔离**：侧边栏顶部切换工作区；"添加项目文件夹"后，每个项目获得**独立 mai-core 进程**（cwd = 项目目录，端口 7437 起依次分配），bash / 读写文件都发生在项目内——类似 VS Code 每窗口一个进程。会话与工作区的归属记录在本地 localStorage。
- **主页面**：Claude 风格布局，流式 token、工具调用卡片（参数/输出可展开、耗时、失败分类与重试）、权限审批卡片（允许一次 / 总是允许 / 拒绝）、上下文水位条、Step 徽标。
- **设置面板**：深色 / 浅色主题；修改 base_url / api_key / 默认模型；"验证并获取模型"走 daemon 的 `config.probe_llm`（空字段回退已保存配置），daemon 不可达时由主进程直连端点探测兜底。
- **品牌**：MAI-CODE 像素标题与天秤吉祥物图标，像素数据直接取自 `src/mai_code/tui/app.py`，与 TUI 完全一致。
- daemon 断线自动重连；未配置 API Key 时提示。

## 调试辅助

```bash
MAI_SCREENSHOT=/tmp/shot.png npm start                     # 启动后截屏并退出（无需录屏权限）
MAI_SCREENSHOT_DELAY_MS=30000 ...                          # 截屏前等待
MAI_E2E_GOAL="目标" ...                                    # 启动后自动发送一条消息
MAI_E2E_VIEW=settings ...                                  # 启动后打开设置面板
MAI_E2E_THEME=light ...                                    # 以浅色主题启动
```

## 路线（下一步）

- [ ] electron-builder 打包：macOS DMG（签名 + 公证）/ Windows NSIS
- [ ] PyInstaller 冻结 mai-core 为 sidecar 二进制，摆脱对仓库 `.venv` 的依赖
- [ ] electron-updater 自动更新
- [ ] 会话内 `session.compact` 手动压缩按钮、trace 回放视图

## 安全说明

renderer 无 Node 权限（`contextIsolation` + CSP），所有 daemon 通信都在主进程；API key 只存在 `~/.mai/config.toml`，`config.get_llm` 只回传 `has_api_key` 布尔值，key 明文永不出后端。
