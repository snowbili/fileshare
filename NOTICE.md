# NOTICE

本项目（FileShare，局域网文件互传）为独立实现：代码库中**不包含**来自 LocalSend 的任何源代码，
全部代码为 Python 标准库 + 自写前端（原生 HTML/CSS/JS），无第三方运行期依赖。

---

## 一、协议参考

本项目的互通协议实现，参考了 LocalSend 的公开协议文档：

| 项目 | 地址 | 许可 |
|---|---|---|
| LocalSend（客户端） | https://github.com/localsend/localsend | Apache License 2.0，Copyright 2022-2026 Tien Do Nam |
| LocalSend 协议规范 | https://github.com/localsend/protocol | 见该仓库说明 |

**参考的具体内容**（均为协议行为约定，非代码）：

- 组播发现：UDP 端口 53317 / 组播地址 224.0.0.167，`announce` 与 `register` 的消息字段与交互流程
- 文件传输：`prepare-upload` → `upload` → `cancel` 的端点、参数与错误码
- 反向下载：`prepare-download` → `download`
- 设备信息字段：`alias` / `version` / `deviceModel` / `deviceType` / `fingerprint` / `port` / `protocol` / `download`
- 设备类型枚举：`mobile` / `desktop` / `web` / `headless` / `server`
- 指纹（fingerprint）用于区分设备、避免自发现的设计

**商标声明**：本项目**未使用** LocalSend 的名称、图标、Logo 或商标作为自身标识，
也不声称与 LocalSend 项目或其作者存在隶属、赞助或官方关系。
（依 Apache License 2.0 第 6 条，该许可不授予商标使用权。）
本项目对 LocalSend 的引用仅为说明协议来源与互通目标：即能与官方客户端互相发现并收发文件。

---

## 二、AI 编程声明

本项目由作者在 **AI 编程助手（Cline）** 的协助下开发：

- 架构设计、代码编写、测试脚本与文档，均为作者与 AI 协作产出
- 所有功能均在真实环境（Windows 11 + Python 3.12 / 3.14）中经过自动化测试验证，
  测试项与运行方式见 `README.md` 的「自检」一节与 `tests/` 目录
- AI 参与的部分不降低使用风险：软件按「现状」提供，见 `LICENSE` 中的免责条款

---

## 三、第三方组件

**运行期依赖：无。** 本项目只使用 Python 标准库，因此不需要 `pip install`。

测试期使用了以下组件（**不参与分发**，已被 `.gitignore` 排除）：

| 组件 | 用途 | 许可 |
|---|---|---|
| [qrcode](https://www.npmjs.com/package/qrcode)（npm） | 仅用于交叉验证本项目自带的二维码编码器 `app/web/qr.js` | MIT |

`app/web/qr.js` 是依据 ISO/IEC 18004 自行编写的二维码编码器（Byte 模式、纠错级别 L/M/Q/H、版本 1–10），
未复制任何第三方实现；与 npm `qrcode` 的逐模块比对脚本为 `tests/qr_compare.js`。

---

## 四、安全与隐私提示

- 本工具面向**可信的家庭 / 办公局域网**，传输默认**不加密**。请勿把端口映射到公网，跨公网请使用 VPN。
- 运行期目录 `data/`（`data/config.json`、`machine.txt`、`fingerprint.txt`、`token.txt`）包含本机信息，
  已通过 `.gitignore` 排除，**请勿提交到版本库**。