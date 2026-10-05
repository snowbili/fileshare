# TODO —— 本次未做，后续可选

> 里程碑 M0–M4 已完成并通过 61/61 项端到端自检（`py -3.14 tests\run_checks.py`）。
> 下面是按约定**本次不并入**的项目，需要时再挑。

## 一、工程习惯

- [x] `git init` + `.gitignore` + `.gitattributes`（忽略 `data/ shared/ inbox/ downloads/ tests/tmp/ tests/qrref/`）
      ——已建仓库并做了上传前隐私扫描；行尾由 `.gitattributes` 统一
      （文本文件 LF，`.cmd` 强制 CRLF），避免协作者互相覆盖
- [ ] `--self-test` 参数：让程序自带一次全链路自检，不必依赖 `tests/run_checks.py`
- [ ] 文件日志 `data/logs/app.log`（`logging.handlers.RotatingFileHandler`）+ 界面「查看日志」按钮
      ——目前日志只输出到控制台，出问题只能看窗口
- [ ] `zipapp` 打包成单文件 `fileshare.pyz`，方便拷贝／做便携版
- [ ] `/api/state` 增加版本号比较：两机版本不一致时提示升级
- [ ] `app/` 正式做成包（`__init__.py` + 相对导入），避免 `config.py`/`store.py` 这类通用名将来与 pip 包冲突
- [ ] 开机自启：`shell:startup` 快捷方式（可选）

## 二、功能增强

- [ ] **SSE `/api/events` 替代 1.5 秒轮询**：文件变化、对方上线、传输进度实时推送，更省 CPU
- [ ] 右键菜单：下载/打开/重命名/删除/复制链接（现在靠行内小按钮）
- [ ] 多选（Ctrl/Shift 多选后整批拖动或批量下载）
- [ ] 地址栏可直接编辑、可粘贴路径跳转
- [ ] 「打开所在文件夹」按钮（后端 `os.startfile`），下载完直接定位
- [ ] 图片缩略图（浏览器 canvas 缩放，不引入 Pillow）+ 大图懒加载
- [ ] 传输完成系统通知（`Notification`，127.0.0.1 属安全上下文可用）
- [ ] 深色模式精修、字号/密度选项

## 三、产品方向

- [ ] **离线投递队列**：对方不在线时入队，对方上线自动推送（手机/笔记本常离线，价值高）
- [ ] **一次性取件码 `/d/<code>`**：任何浏览器输 8 位码即下载，不需要客户端
- [ ] **目录即频道**：`shared\投递给XXX\` 当定向投递箱，带"已取"标记
- [ ] 局域网轻量聊天/待办条（复用同一通道与 `/api/msg`）

## 四、明确不做（含理由）

- **HTTPS 自签证书**：官方 LocalSend 客户端会因证书校验失败而报错，反而更难用；
  协议里的 `protocol: https` 支持留在以后需要时再加
- **WebRTC / P2P 打洞**：千兆局域网单流 HTTP 已经能打满带宽，收益为 0，复杂度高
- **Pillow / watchdog 等第三方库**：缩略图用浏览器 canvas，目录变化用轮询，保持零依赖
- **`socket.sendfile`**：实测本机 `hasattr(os,'sendfile') == False`（Windows 无 `os.sendfile`），
  `socket.sendfile` 会退化成一个 64KB 的 `sendall` 循环，并不更快；现在用 1MB 分块写入
- **整目录推送**：目前只推单个文件（网络通道与进度模型都要重做，收益有限）

## 五、已知限制（不影响使用，记录备查）

- **`.cmd` 批处理必须保持纯 ASCII**：cmd.exe 是按字节偏移续读批处理文件的，
  一旦文件里有 UTF-8 中文又执行了 `chcp 65001`，它会从行中间接着执行，出现
  `'xxx' is not recognized as an internal or external command` 之类的怪现象
  （表现为日志少行、走到不该走的 `pause`）。中文说明请写在 README / 界面里，别写进 `.cmd`
- 二维码编码器只支持版本 1–10（Byte 模式），足够放局域网 URL；更长的内容会报"内容过长"
- 官方 LocalSend 设备的共享目录**无法浏览**（协议限制），只能推送
- 同机跑两个实例做测试时，需分别设置 `FILESHARE_HOME`（否则共用同一份配置与目录）；
  且**同机两实例如果指纹也相同**，它们无法互相区分（来源 IP 也一样），删掉其中一个的
  `data\fingerprint.txt` 与 `data\machine.txt` 即可
- 上传续传依赖 `<文件名>.part` + `.part.json` 侧车文件；界面会隐藏它们，手动删除 `.part` 即放弃续传
- 拖出下载（`DownloadURL`）是 Chromium 专有；Firefox 下请用「拖到左面板」或「下载」按钮
