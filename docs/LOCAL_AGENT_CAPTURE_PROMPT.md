# 给本地 agent 的提示词：配置 Token 自动抓取（Clash / Windows 代理）

> 这是自动抓取的「渠道 2」。用 Clash Verge Rev 的电脑先试「渠道 1」：开始菜单「配置自动获取登录」（免安装版 `配置自动获取.cmd`，仓库里 `setup-capture.cmd`，见 [CAPTURE_SETUP.md](CAPTURE_SETUP.md)）；**用其他代理软件（Clash for Windows、Mihomo Party、FlClash、v2rayN 等）或脚本中途停下时，用本提示词**。
>
> 用法：在**要使用工作台的那台 Windows 电脑**上，打开有终端权限的本地 agent（Claude Code、Codex 等），把下面「提示词正文」整段复制进去，**先把开头「我的情况」里的 xxx 换成你的答案**（不知道就写「不知道」，agent 会自己查）。agent 每次要改东西前都会先问你。
>
> 不知道自己的梯子叫什么：看屏幕右下角托盘里的小图标（可能要点「^」展开），鼠标停在上面会显示名字；或者打开任务管理器看正在运行的程序。
>
> 路线 B 用到的 `/proxy.pac` 需要工作台已包含对应改动（见引入本文件的 PR）。
>
> 不想折腾代理和证书的同学不需要这份提示词，直接用工作台「设置 → 首次接入」的页面脚本方式即可（企业微信 Ctrl+Shift+Alt+D → 预约页右键「开发者工具」→ Console 粘贴脚本）。代价是登录失效不能自动恢复，刷新预约页或超过 60 分钟要重新粘贴。

---

## 提示词正文

你在一台 Windows 10/11 电脑上，帮电脑的主人配置「南科大场地预约工作台」的 Token 自动抓取。请用中文和我沟通。

### 我的情况（我填的，不知道的写了「不知道」）

- 我用的梯子 / 代理软件：xxx（例如 Clash Verge、Clash for Windows、Mihomo Party、FlClash、v2rayN、NekoBox、某某 VPN / 加速器；没有梯子就写「没有」）
- 它平时开着吗：xxx（开着 / 关着 / 不知道）
- 有没有开「TUN / 虚拟网卡 / 增强模式 / 全局」：xxx（开了 / 没开 / 不知道）
- 工作台是怎么装的：xxx（安装程序 / 免安装 zip / 仓库代码 / 不知道）

这几项只是参考：**以第 0 步你实际查到的为准**。填了「不知道」或者和你查到的不一致时，用一句话告诉我你认出的是什么软件、什么状态，再继续。如果我用的是商业 VPN / 加速器这类不能自己加规则的软件，直接告诉我，建议走页面脚本方式（路线 C），不要尝试修改它。

### 目标

让**企业微信内置浏览器**访问 `reservation.sustech.edu.cn` 的流量经过本机的 mitmdump（`127.0.0.1:8080`），工作台（`http://127.0.0.1:8766`）就能自动拿到本人登录后的 Token。**除了这一个域名，其他任何流量的走向都不能改变。**

### 硬性规则（任何一条做不到就停下来问我）

1. **每一次写操作之前**：先说明要改什么、为什么、怎么撤销，等我回复「可以」再做。只读检查不用问。
2. 改任何文件之前先备份到 `原文件名.bak-sustech-<日期时间>`，并告诉我备份路径。
3. 不改订阅链接、代理节点、代理组；不切换 Clash 的全局/规则模式、不开关 TUN、不开关系统代理，除非我明确同意。
4. 不结束、不重启任何不是工作台自己启动的进程（包括 Clash、企业微信、其他 Python）。端口被占用就报告占用者，不要杀。
5. 证书只能装到**当前用户**的「受信任的根证书颁发机构」，而且只能装本机 mitmproxy 自己生成的证书（`%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.cer`）。不装到本地计算机存储，不从别处复制证书，**绝不复制、上传、打印 `mitmproxy-ca.pem` 等私钥文件**。
6. 不关闭杀毒软件/防火墙，不改系统级 PowerShell 执行策略（需要时只用 `-ExecutionPolicy Bypass` 运行单个脚本）。
7. 不打印、不保存 Token、Cookie、手机号、学号、Clash 的 `secret`；需要展示时只显示前 6 位。
8. 不调用任何下单相关接口，不启动抢场/监控任务，不修改 `config.toml` 里的 `[safety]`、`[user]`、`[order]`，不碰 `watch-orders.json`。
9. 遇到和本文描述不一致的情况（软件版本不同、文件找不到、规则已存在但名字不同等），先报告实际情况和你的建议，不要自己猜着改。

### 第 0 步：只读摸底（不用问，直接做，做完汇报）

1. 找到工作台的程序和配置（不确定就问我）：
   - 安装版：程序在 `%LOCALAPPDATA%\Programs\SustechReservation\app`（含 `app.py`、`clash-extension.js`），自带 Python 在同级的 `runtime\`；配置在 `%LOCALAPPDATA%\SustechReservation\data\config.toml`。
   - 免安装版：解压目录里的 `app\` 和 `runtime\`；配置同样在 `%LOCALAPPDATA%\SustechReservation\data\config.toml`。
   - 仓库版：程序和 `config.toml` 都在仓库目录，Python 在 `.venv`。
2. 工作台是否在运行：`GET http://127.0.0.1:8766/api/config` 能否返回。没运行就让我双击桌面「南科大场地预约」（仓库版双击 `start-workbench.cmd`），端口必须是 8766。
3. 读取：
   - `GET /api/capture/clash`：`enabled`（是否抓取模式）、`listening`、`captured`、`port_conflict`
   - `GET /api/mitm/state`
   - `config.toml` 里 `[token] capture_mode` 的值（只读）
4. 识别本机代理软件（看进程和安装目录，不要改）：
   - Clash Verge Rev：进程 `clash-verge.exe` / `verge-mihomo.exe`，配置目录 `%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\`
   - 其他 Clash 内核客户端：Clash for Windows、Mihomo Party、FlClash、Clash Nyanpasu、旧版 Clash Verge 等（规则语法和 Clash Verge Rev 相同，放规则的位置不同）
   - 非 Clash 内核客户端：v2rayN、NekoBox、Hiddify 等（Xray / sing-box，规则写法不同）
   - 其他 VPN 客户端
   - 都没有
5. 读 Windows 当前代理设置（只读）：`HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings` 的 `ProxyEnable`、`ProxyServer`、`AutoConfigURL`。
6. 如果是 Clash Verge Rev：从它的运行配置里找 `external-controller` 和 `secret`（secret 不要显示），调用本机控制接口读取 `mode`（rule/global/direct）、`tun.enable`；看全局扩展脚本里是否已有 `reservation.sustech.edu.cn` 相关规则，以及用的代理名（可能是 `SUSTech-MITM` 或 `MITM-Reservation`）。
7. `%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.cer` 是否存在；当前用户根证书里是否已有名为 mitmproxy 的证书（`certutil -user -store Root mitmproxy`，只读）。

汇报格式：一张表，列出上面每一项的结果，然后说明你建议走哪条路线（A/B/C）以及理由，**等我确认路线**。

路线选择规则：
- 有 Clash Verge Rev 并且平时在用 → **路线 A**
- 有其他 Clash 内核客户端并且平时在用 → **路线 A2**
- 有 v2rayN 等非 Clash 内核客户端并且平时在用 → **路线 D**
- 其他 VPN 客户端，或上面几条做不到 → 先报告，不要改；退回页面脚本方式（路线 C）
- 没有任何代理软件，系统代理也没开 → **路线 B**
- 我说不想装证书/不想动代理 → **路线 C**（页面脚本，什么都不改）

### 公共步骤：打开工作台的抓取模式并准备证书（路线 A、A2、B、D 都要）

1. 如果 `capture_mode` 不是 `"mitm"`：备份 `config.toml`，只把 `[token] capture_mode` 改成 `"mitm"`，其他行一个字都不动。工作台约 1 秒内自动重新读取配置。
2. `POST http://127.0.0.1:8766/api/mitm/restart`（不要带 Origin 头），再看 `/api/mitm/state` 的 `listening` 和 `proc_alive` 都是 true。若 `port_conflict` 为 true，说明 8080 被别的程序占了：报告占用进程，停下。
3. 确认 `%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.cer` 已生成。
4. 征得我同意后运行：`certutil -user -addstore Root "%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.cer"`。Windows 会弹出安全警告，需要**我本人**点「是」。
   - 告诉我它的影响：这台电脑上信任了一个本机生成的证书；私钥只在这台电脑的 `.mitmproxy` 文件夹里，不要分享这个文件夹。
   - 撤销方法：`certutil -user -delstore Root mitmproxy`
5. 提醒我**完全退出企业微信再重新打开**（托盘图标右键退出），证书才会生效。

### 路线 A：Clash Verge Rev（已有 Clash）

目标：只加一条「`reservation.sustech.edu.cn` → 本机 8080」规则，其他规则、节点、DNS 不变。

1. 必须是**规则模式（rule）**才生效。如果当前是全局模式，告诉我原因，问我是否切换，不要自己切。
   同时 Clash 的「系统代理」或「TUN」至少要开一个，否则企业微信的流量根本不经过 Clash。两个都没开时不要替我打开，先问我（也可以改走路线 B）。
2. 规则内容以工作台目录里的 `clash-extension.js` 为准（`applySustechMitmExtension`）：新增一个 `type: http, server: 127.0.0.1, port: 8080` 的代理，把 `DOMAIN,reservation.sustech.edu.cn,<代理名>` 放在规则最前面；重复应用不会累加。
3. 如果 **TUN 开着**：必须给工作台和抓取进程的学校出站加直连例外，否则会回环（2026-10-18 出现过 502）：
   - 安装版 / 免安装版：`runtime\pythonw.exe` 和 `runtime\python.exe` 两个都要加（工作台和抓取代理跑在 `pythonw.exe` 下）。
   - 仓库版真实解释器路径：`<工作台>\.venv\Scripts\python.exe -c "import sys,pathlib; print(pathlib.Path(sys._base_executable).resolve())"`，同目录的 `pythonw.exe` 也加上。
   - 以 Clash 连接面板里实际看到的 `processPath` 为准，可能不止一个。
   - 调用 `applySustechMitmExtension(config, { processPaths: [上面确认的绝对路径] })`，它会在学校规则前加 `AND,((DOMAIN,…),(PROCESS-PATH,…)),DIRECT`，并在 blacklist 模式的 `dns.fake-ip-filter` 里追加这个域名；如果 DNS 不是 blacklist 模式，它会报错，这时停下来问我。
4. 写法：生成一份新的「全局扩展脚本」全文，要求：
   - 如果原脚本已有 `function main(config, profileName)`，把原函数改名为 `userMainBeforeSustech` 并完整保留，新的 `main` 先调用它，再调用 `applySustechMitmExtension`；
   - 把 `clash-extension.js` 里的 `applySustechMitmExtension` 函数原样贴进去（去掉 `module.exports` 那一行）；
   - 文件里用注释标出 `// ==== SUSTech reservation capture BEGIN/END ====`，便于以后删除；
   - 如果原脚本里已经有针对 `reservation.sustech.edu.cn` 的规则（比如旧的 `MITM-Reservation`），不要再加第二份，先告诉我，按我的决定替换或保留。
5. 把新旧脚本的差异给我看，等我确认。然后**优先让我自己**在 Clash Verge 里操作：左侧「订阅」→「全局扩展脚本」→ 编辑 → 粘贴 → 保存。如果我让你直接写文件，先备份原文件，写完提醒我在 Clash Verge 里点刷新/重新加载。
6. 撤销方法：删除 BEGIN/END 之间的内容并恢复原 `main` 名字，或直接用备份文件覆盖后重新加载。

### 路线 A2：其他 Clash 内核客户端（Clash for Windows、Mihomo Party、FlClash、Nyanpasu…）

规则内容和路线 A 完全一样（同一个内核）：一个 `type: http, server: 127.0.0.1, port: 8080` 的代理 + 放在最前面的 `DOMAIN,reservation.sustech.edu.cn,<代理名>`；TUN 开着时同样要加进程直连例外。只是**放的位置因客户端而异**：

1. 先只读查清这个客户端（和版本）在哪里放「不随订阅更新被覆盖」的自定义规则：例如 Clash for Windows 的 Parsers / Mixin，Mihomo Party 的「覆写」，FlClash / Nyanpasu 的覆写或脚本。**不要直接改订阅生成的配置文件**，下次更新订阅会被覆盖。
2. 把找到的位置、准备写入的内容（YAML 或 JS，以该客户端支持的格式为准，逻辑照搬 `clash-extension.js` 的 `applySustechMitmExtension`）和撤销方法告诉我，等我确认。
3. 优先让我自己在客户端界面里粘贴保存；我让你代劳时先备份再写。写完让我在客户端里重新加载配置。
4. 规则模式、系统代理 / TUN 至少开一个，这两条要求和路线 A 相同，不要替我切换。
5. 做不到「只影响这一个域名」就停下报告，退回路线 C。

### 路线 D：v2rayN 等非 Clash 内核客户端

目标相同：只有 `reservation.sustech.edu.cn` 交给本机 `127.0.0.1:8080`（HTTP 代理），其他流量不变。写法不同：

1. 只读查清客户端名称、版本和内核（Xray / sing-box），以及它是否支持「自定义 HTTP 出站 / 服务器」和「按域名分流到指定出站」的路由规则。
2. 方案：新增一个指向 `127.0.0.1:8080` 的 HTTP 出站（或自定义服务器），再加一条放在最前面的路由规则 `domain:reservation.sustech.edu.cn` → 这个出站。以该版本的实际界面和配置格式为准，**不要套用 Clash 的写法**。
3. 把要改的位置、内容和撤销方法告诉我，等我确认；优先让我在界面里操作，代劳时先备份。不要删改已有的路由规则、节点和订阅。
4. 客户端开着 TUN（虚拟网卡）时，同样要让工作台的 Python（见路线 A 第 3 步的路径）访问学校时直连，避免回环；客户端不支持按进程分流时停下报告。
5. 客户端做不到「按域名把流量交给一个本机 HTTP 代理」时，停下报告，退回路线 C。

### 路线 B：没有 Clash（用 Windows「使用设置脚本」）

原理：工作台在 `http://127.0.0.1:8766/proxy.pac` 提供代理规则，只有 `reservation.sustech.edu.cn` 走本机 8080（8080 不通时自动直连），其他一律直连；工作台的抓取模式关掉时，这份规则自动变成全部直连。

1. 先确认 `ProxyEnable` 不是 1、`AutoConfigURL` 为空。**如果已有别的代理或设置脚本，停下来报告**，不要覆盖。
2. 打开 `http://127.0.0.1:8766/proxy.pac` 看内容，确认里面有 `PROXY 127.0.0.1:8080; DIRECT`（如果是全部 `DIRECT`，说明公共步骤第 1 步没生效）。
3. 让我自己操作（优先）：Windows 设置 → 网络和 Internet → 代理 →「使用设置脚本」打开 → 脚本地址填 `http://127.0.0.1:8766/proxy.pac` → 保存。
   如果我让你代劳：先导出备份 `reg export "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" <备份路径>.reg`，再只设置 `AutoConfigURL`。
4. 完全退出并重新打开企业微信。
5. 撤销方法：同一个页面关掉「使用设置脚本」（或删除 `AutoConfigURL`）；不再使用时把 `capture_mode` 改回 `"page"`。
6. 注意：企业微信内置浏览器是否遵守 Windows 的「设置脚本」**尚未在真实环境验证**。如果下面的验证步骤一直抓不到，就是不遵守：撤销本路线，改用路线 C，并把现象告诉我。

### 路线 C：页面脚本（什么都不改）

告诉我工作台「设置 → 首次接入」里的步骤即可：企业微信按 Ctrl+Shift+Alt+D 开启调试模式 → 打开预约页 → 右键「开发者工具」→ Console → 粘贴工作台复制的连接脚本 → 回车 → 点「连接本机控制台」→ 切换一次场馆。

### 验证（路线 A、A2、B、D）

1. 在企业微信里打开预约页面（已经开着就刷新一下）。
2. `GET /api/capture/clash`：`captured` 变成 true；`GET /api/connect/state`：`auth.token_set` 为 true（只显示前 6 位）。
3. 征得我同意后 `POST /api/connect/test`：这会向学校发**一次只读查询**（不下单），返回 `ok: true` 才算成功。
4. 路线 A 且开了 TUN：在 Clash 连接面板确认「企业微信 → 学校」走本机 MITM，「Python → 学校」走 DIRECT。
5. 再确认一般网页和企业微信聊天正常。

任何一步失败：报告现象和你看到的状态值，给出撤销步骤，不要反复重试或扩大改动范围。

### 结束时的汇报

用这个格式：

- 走的路线：A / B / C
- 改了什么：每个文件/设置一行，带备份路径
- 证书：是否安装、指纹前 8 位
- 验证结果：captured、只读复验结果
- 撤销步骤：逐条可执行的命令或操作
- 还没解决的问题
