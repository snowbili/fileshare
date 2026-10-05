# FileShare —— 局域网文件互传（左右两栏、拖拽即传）

> 互通协议参考 [LocalSend](https://github.com/localsend/localsend)（Apache-2.0，Copyright 2022-2026 Tien Do Nam）
> 及其[协议规范](https://github.com/localsend/protocol)；本项目为**独立实现**，未使用其源代码、名称或商标。
> 本项目在 **AI 编程助手（Cline）** 的协助下开发。详见 [NOTICE.md](NOTICE.md)。

一个不需要装客户端、不需要服务器、不需要账号的局域网传输工具：
左边是**我的共享目录**，右边是**对方电脑的共享目录**，两边都能拖进拖出，并兼容
[LocalSend](https://github.com/localsend/localsend) 协议（可与手机官方 App 互发）。

* 后端：Python 3 **纯标准库**（`http.server` + `socket`），**零第三方依赖**，不需要 pip install
* 前端：原生 HTML/CSS/JS，无构建、无 CDN，离线可用
* 窗口：用 Edge/Chrome 的 `--app` 模式打开，看起来就是桌面软件

---

## 一、快速开始

### 运行环境

* **Python 3.8 或更高**（实测通过 **3.12.9** 与 **3.14.6**；3.7 理论可用但已停止维护）
* **不需要 pip install**：只用 Python 标准库，零第三方依赖，因此不受 wheel / ABI 兼容性影响
* 界面用系统自带的 Edge / Chrome 打开即可（拖出到资源管理器依赖 Chromium 内核，建议 Edge）
* `run.cmd` 会按 **3.14 → 3.13 → … → 3.8** 的顺序自动挑选可用解释器，都没有时再回退 `python` / `python3`；
  也可以手动指定版本：`py -3.12 app\server.py --open`

### 启动

1. 双击 **`run.cmd`**（首次会创建 `shared/`、`inbox/`、`downloads/`、`data/` 并打开界面）
2. 如果另一台电脑连不上你：**右键 `firewall.cmd` → 以管理员身份运行**（放行 TCP 8099 与 UDP 53317，仅私有网络）

界面上的三处拖拽：

| 操作 | 结果 |
|---|---|
| 资源管理器 → **左面板** | 文件复制进共享目录（支持整个文件夹，自动建子目录） |
| **右面板** → 资源管理器任意文件夹 | 建立下载，直接落到目标文件夹（本机 `downloads/` 还会留一份副本） |
| 左面板 → **右面板** | 推送给对方（对方是本工具则落到它的接收目录） |
| 右面板 → **左面板** | 下载进本机共享目录（页内拖拽兜底） |

其它入口：`Ctrl+V` 粘贴截图/文本即共享；右面板文件可点「下载」或「复制链接」；
左面板可改名/删除；`F5` 刷新；退格返回上级；**二维码**按钮让手机浏览器直接下载。

---

## 二、两台电脑怎么用

1. 两台电脑都双击 `run.cmd`
2. 同网段时自动相互发现（UDP 组播 `224.0.0.167:53317`）；发现不到就点 **扫描设备**，
   或点 **添加设备** 手动填 `192.168.1.100` / `192.168.1.100:8099`
3. 在右侧下拉里选中对方电脑的名字
4. A 把文件拖进左面板 → B 在右面板刷新即可看到 → B 拖到桌面即完成下载

设备列表里会标注对方类型：

* `可浏览`：对方也是本工具，能看目录、能双向传
* `仅推送`：对方是**官方 LocalSend**（如手机 App）。官方协议没有"列目录"接口，
  所以右面板看不到内容，但可以把左边文件拖到右下区域推过去，对方手机上会弹接受提示

---

## 三、手机怎么用

* **不装任何 App**：点顶栏「二维码」，手机连同一个 WiFi 扫码，浏览器里就能浏览并下载你共享的文件
* **装了官方 LocalSend**：双向互通
  * 手机 → 电脑：手机端选电脑发送，本工具会弹「接受/拒绝」（可在设置里改成自动接收）
  * 电脑 → 手机：把文件拖到右面板的该设备上，手机端会弹出接受提示

---

## 四、目录结构

```
fileshare\
├─ app\               程序本体（改代码只改这里）
│  ├─ server.py       入口：路由、静态托管、安全校验、单实例
│  ├─ fsapi.py        共享目录 API：列目录 / 上传 / 下载(Range) / 新建 / 改名 / 删除
│  ├─ peer.py         与对方通信：代理下载、推送、拉取、设备探测
│  ├─ discovery.py    发现：UDP 组播 announce + register 双向发现
│  ├─ localsend.py    LocalSend v2.2 兼容层
│  ├─ store.py        运行期状态（设备/传输队列/会话）
│  ├─ config.py       配置与路径
│  ├─ util.py         文件名清洗、路径安全、MIME、哈希
│  └─ web\            前端（index.html / app.js / style.css / qr.js）
├─ shared\            左面板 = 我共享出去的
├─ inbox\             接收到的（别人推给我、LocalSend 发来的）
├─ downloads\         从对方拖出下载时留的副本
├─ data\              config.json / token.txt / fingerprint.txt / state.json
├─ tests\             自检脚本（见第七节）
├─ run.cmd            一键启动（Edge/Chrome 应用窗口）
├─ open-browser.cmd   用默认浏览器打开
└─ firewall.cmd       放行防火墙（需管理员）
```

---

## 五、端口与命令行

默认 HTTP 端口 **8099**（故意避开官方 LocalSend 的 53317，避免抢端口），
UDP 发现端口固定 **53317**。

```
py -3.14 app\server.py [选项]
  --port 8099            改 HTTP 端口
  --bind 0.0.0.0         监听地址
  --share  D:\xx         改共享目录
  --inbox  D:\yy         改接收目录
  --downloads D:\zz      改下载目录
  --alias  我的电脑      改显示名
  --no-discovery         关闭组播发现（只用扫描/手动添加）
  --scan-now             启动后立即扫一遍网段
  --open                 启动后用 Edge/Chrome 应用窗口打开
  --browser              用默认浏览器打开
  --no-window            只跑服务不开窗口
```

设置项也可以直接在界面「设置」里改（写入 `data/config.json`）。
`FILESHARE_HOME` 环境变量可把运行期目录搬到别处（做便携版或同机多实例测试）：

```
set FILESHARE_HOME=D:\fileshare-data
```

---

## 六、安全说明

设计目标是**可信家庭/办公局域网**，默认不做加密传输，但做了这些防护：

* **Host 白名单**：只接受 `127.0.0.1 / localhost / 本机 IP / 本机名`，挡住 DNS rebinding
* **Origin 校验**：浏览器写操作必须来自本机允许的来源，挡住 CSRF
* **一次性 token**：页面启动注入 `data/token.txt` 里的 token，写操作必须携带；跨站脚本拿不到
* **跨机身份头**：`X-FS-Peer` 只对 `/api/fs/upload`、`/api/fs/mkdir` 生效，且**不在 CORS 白名单**里，不能伪造
* **路径限制**：所有路径 `realpath` + `commonpath` 校验，拒绝 `..`、盘符、UNC、符号链接逃逸
* **最小暴露**：对端只能读 `share`，写入只能进 `share`/`inbox`；删除需要 token（可在设置里彻底关掉）
* **可选 PIN**：设置里填 PIN 后，与官方 LocalSend 互通时需要 PIN
* **CORS 收紧**：只在来源地址确实属于本机白名单时才回显 `Access-Control-Allow-Origin`，
  不再使用 `*` —— 避免"你在浏览器里打开的任意网页"跨域读取本机共享目录
* **注意**：为方便使用，拖出下载默认会在本机 `downloads/` 目录**自动留一份副本**
  （同时进入传输队列）。不需要的话可在「设置」里关闭「拖出下载时留一份副本」。

⚠️ 请**不要**把 8099 端口映射/转发到公网。需要跨公网传输请用 VPN（如 Tailscale/ZeroTier）。

---

## 七、自检

```
py -3.14 tests\run_checks.py           # 端到端自检：起两个实例，跑完 M0~M4 关键路径（61 项）
node tests\qr_compare.js               # 二维码编码器与 npm qrcode 逐模块交叉验证（232 项）
py -3.14 tests\unit_identity.py        # 指纹/自身判定单元校验（16 项）
py -3.14 tests\identity_clash_live.py  # 复现"两台机器指纹相同互看不见"并验证修复（10 项）
py -3.14 tests\discovery_live.py       # 真实网卡上的组播自动发现
py -3.14 tests\sniff_discovery.py 20   # 现场抓包：局域网上到底有哪些设备在广播
tests\dom_check.py                     # 前端渲染校验（需先用 Edge 无头模式 dump DOM，见文件头注释）
```

`run_checks.py` 覆盖：Host 伪造/CSRF/token、中文名 RFC 5987、Windows 保留名清洗、
同名自动改名、sha256 校验、Range 分段、chunked 拒绝、路径穿越、上传中断不留残文件、
keep-alive 帧完整性、组播/register 发现、右面板浏览、推送/拉取/代理下载（含 3MB 文件哈希比对）、
LocalSend 全流程（prepare-upload/upload/cancel/反向下载/待确认队列）。
结果写入 `tests/run_checks_result.txt`。

> 说明：`tests/qrref/` 是只给二维码验证用的 npm 依赖（`qrcode`），运行本工具本身完全不需要 Node。

---

## 八、常见问题

**1. 发现不到对方？**
先跑一次现场抓包，它会直接告诉你问题在哪：

```
py -3.14 tests\sniff_discovery.py 20
```

* 报"⚠指纹与本机相同"→ 见下面第 2 条
* 一个包都没收到 → 路由器 AP/客户端隔离、防火墙拦了 UDP 53317（管理员运行 `firewall.cmd`），
  或 53317 被同机运行的官方 LocalSend 独占
* 能收到包但列表为空 → 点「扫描设备」或「添加设备」手填对方 IP

**2. 能看到手机上的 LocalSend，却看不到另一台跑 FileShare 的电脑？**
几乎可以肯定是**两台机器指纹（fingerprint）相同**：把 fileshare 文件夹整体拷到另一台电脑时，
`data\fingerprint.txt` 也被一起带走了，于是双方都把对方当成"自己"而丢弃；
而手机 LocalSend 的指纹不同，所以照常出现。

本工具已修复（指纹相同但来源 IP 不是本机 → 仍当作对方设备，并在设备列表显示 ⚠），
启动时也会自动检测"data 目录来自别的机器"并生成新指纹。处理办法：

1. 把最新版 `app\` 目录拷到另一台电脑（或至少让两台都更新到本版本）
2. 重启两台电脑上的 FileShare；也可以删掉 `data\fingerprint.txt`、`data\machine.txt` 让它重新生成
3. 「设置」里能看到**本机指纹**，两台机器的指纹应当不同

**3. 对方连不上我？**
管理员运行 `firewall.cmd`；确认没有安全软件拦截 Python；确认端口没被占用（可在设置或
`data\config.json` 里改 `http_port`）。

**4. 端口被占用 / 重复双击 run.cmd？**
程序会自动探测：若已有实例在跑，就直接打开窗口，不会报错。想换端口用 `--port`。

**5. 为什么官方 LocalSend 设备右边是空的？**
官方协议只定义了"推送/接收某个会话的文件"，没有浏览对方目录的接口。所以该设备只支持推送。

**6. 拖出到桌面没反应？**
`DownloadURL` 拖出是 Chromium（Edge/Chrome）专有机制；Firefox 不支持。
兜底：把右面板文件拖到**左面板**（下载进共享目录），或点行内「下载」按钮。

**7. 大文件传输中断了怎么办？**
上传（本机 → 共享目录）支持续传：同名 `.part` 文件与元数据会保留，重传时自动从断点继续。
下载走浏览器原生能力，中断后重新拖一次即可。

**8. 想彻底清理？**
删除整个 `fileshare` 目录即可；删防火墙规则：
`netsh advfirewall firewall delete rule name="FileShare TCP 8099"`（UDP 53317 同理）。

---

## 九、致谢与声明

### 协议参考

本项目的互通能力参考了 **LocalSend** 的公开协议文档：

* [localsend/localsend](https://github.com/localsend/localsend) —— Apache License 2.0，Copyright 2022-2026 Tien Do Nam
* [localsend/protocol](https://github.com/localsend/protocol) —— LocalSend 协议规范

**本项目是独立实现，不是 LocalSend 的衍生作品**：代码库中不包含来自 LocalSend 的任何源代码，
仅依照协议文档实现了发现与传输行为（端点、字段、错误码），因此能与官方客户端互相发现与收发文件。
项目**未使用** LocalSend 的名称、图标或商标，也不声称与其存在隶属或官方关系
（依 Apache License 2.0 第 6 条，该许可不授予商标使用权）。

### AI 编程声明

本项目在 **AI 编程助手（Cline）** 的协助下开发：架构设计、代码、测试脚本与文档均为协作产出，
并在真实环境（Windows 11 + Python 3.12 / 3.14）中经过自动化测试验证，测试项见上方「自检」一节。
软件按「现状」提供，完整免责条款见 [LICENSE](LICENSE)。

### 第三方组件

* **运行期依赖：无**（只用 Python 标准库）
* 测试期使用 npm 的 [qrcode](https://www.npmjs.com/package/qrcode)（MIT）**交叉验证**本项目自带的
  二维码编码器 `app/web/qr.js`；该组件不参与分发。`qr.js` 系依据 ISO/IEC 18004 自行编写，未复制第三方实现。

更多细节见 [NOTICE.md](NOTICE.md)。
