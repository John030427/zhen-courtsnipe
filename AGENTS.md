# 给 AI agent 的说明（Codex、Claude Code 等）

在用户的 Windows 电脑上、本仓库目录里操作工作台时遵守这里的做法。用户每次都是让 agent 启动服务的，所以启动方式决定了用户桌面上会不会多出一个窗口。

## 启动、停止、检查

**不要**为了跑服务新开 PowerShell / 命令行窗口，也不要在前台运行 `run-local.ps1` 或 `uvicorn`：服务会挂在那个窗口上，用户一关窗口服务就停了。用启动器：

```powershell
$env:SUSTECH_NO_DIALOG = "1"                          # 出错时打印原因，不弹对话框卡住命令
.\.venv\Scripts\python.exe launcher.py                # 启动（已在运行就复用）
.\.venv\Scripts\python.exe launcher.py --stop --quiet # 停止
```

- 启动：服务在后台运行，没有任何窗口，命令结束、关掉浏览器都不会停。等 `/api/health` 通过后返回，打印 `OPEN http://127.0.0.1:8766/`，退出码 0；退出码 1 表示没起来，原因已打印，日志在 `logs\server.log`。
- 启动后过一会儿再查一次 `/api/health`。如果你的沙箱在命令或会话结束时连带结束了服务，不要改用开窗口的方式，请用户双击 `start-workbench.vbs`。
- 端口 8766 被别的程序占用时，启动器会报告并退出。把情况告诉用户，**不要**结束那个程序。
- 停止：退出码 0 已停止；**3 表示有抢场或监控在运行，什么都没停**，告诉用户，不要自己加 `--force`；1 表示没停下来。旧版前台 uvicorn 服务没有退出回调，需用户按 Ctrl+C，不能宣称启动器已经停止它。
- 检查：`GET http://127.0.0.1:8766/api/health`，只汇报 app、commit、data_dir 和是否有任务在运行。
- 重启 = 先停止（确认退出码 0），再启动。有任务在运行时不要重启。
- 用户自己启动优先双击 `start-workbench.vbs` 或直接指向 pythonw 的快捷方式，退出是网页左下角「退出服务」或 `stop-workbench.cmd`。`run-local.ps1` 默认也是后台启动，只有 `-Console` 是前台开发模式。

## 不要做

- 不启动、不停止抢场或监控任务，不调用下单接口，不替用户点「学校验证」。
- 不修改 `config.toml`、`watch-orders.json`、`.local-capture\` 里的内容，不清空待核实（uncertain）的订单记录。
- 不打印、不上传 Token、Cookie、手机号、学号，也不输出 token 预览。
- 不改 Clash / VPN / 系统代理 / 证书，不改系统 PowerShell 执行策略（需要时只对单个脚本用 `-ExecutionPolicy Bypass`）。
- 只结束本项目自己的进程；别的 Python、Clash、企业微信一律不碰。
