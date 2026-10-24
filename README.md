# QQ AI 后端

[English](README.en.md) | 中文

把内置 AI 接到个人 QQ 的纯后端。没有桌宠窗口、形象、好感度、工作状态、空闲时段标签，
也没有聊天记录网页；它只做一件事：在 QQ 私聊里像真人一样聊天，并在合适的时候主动发消息。

> 个人 QQ 自动化不是官方能力，存在风控、冻结或封号风险。**务必使用小号**。

## 功能

- **QQ 私聊**：通过 OneBot 11（NapCat / SnowLuma）收发私聊文字和图片，支持首个私聊用户自动绑定、长消息分片、去重。
- **主动消息**：时段、天气变化、久坐、长时间没互动、随机闲聊；原来桌宠气泡里的自言自语，现在直接发到 QQ。
- **跨重启续聊**：聊天记录落盘；重启或新开对话时自动导入最近若干条历史，保证同一个对话连贯。
- **人设与 AI 接口可配**：内置 DeepSeek Web2API，或任意 OpenAI 兼容接口（base_url / API Key / model）。
- **电脑上下文**：时间、前台应用、正在播放的音乐、天气、账户余额会进入 system prompt。
- **无界面**：只保留系统托盘（设置 / 退出）。

## 快速开始（发布包）

1. 安装并登录 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 或
   [SnowLuma](https://github.com/SnowLuma/SnowLuma)，登录一个 **QQ 小号**。
2. 在 NapCat WebUI 里添加 `WS 正向服务器`（OneBot 11）：
   - 监听地址：`127.0.0.1`
   - 端口：`3001`
   - 消息格式：`array`
   - Token：自己设一个随机串
3. 下载 Release 里的 `QQAIBackend-v*.zip`，解压后运行 `QQAIBackend.exe`。
4. 右键托盘图标 →「设置…」：
   - **AI**：选厂商预设，填 API Key / 模型；用内置免费 AI 就保留默认值。
   - **QQ**：填 WS 地址 `ws://127.0.0.1:3001` 和与 NapCat/SnowLuma 完全一致的 Token。
5. 用你的主号私聊这个机器人小号，发一句话。第一次私聊的用户会被自动加入白名单。
6. 之后正常聊天即可；主动消息也会发给白名单里的 QQ。

### NapCat 配置要点

- 选项名称在不同版本里可能写作 `WS正向服务器` / `WebSocket 服务器`；要选 **NapCat 作为服务端、本程序作为客户端连接** 的那一种。
- **Token 必须与设置里完全一致**。token 错误时 NapCat 会先接受连接、随后返回
  `{"retcode":1403,"message":"token验证失败"}` 并断开；日志里会看到
  `qq bridge: onebot error 1403` 和 `disconnected code=1000`。
- 只监听 `127.0.0.1`，不要把这个无 TLS 的端口暴露到局域网或公网。
- 如果同时运行多个客户端（例如桌宠和本后端），它们会都收到同一条私聊，可能重复回复；请只开一个。

### SnowLuma 配置要点

- 下载 Windows 完整包（Lite 版需要 Node.js 24+），解压后运行 `launcher.bat`，
  打开 `http://localhost:5099`，用启动日志里的初始密码登录并修改密码。
- 保持官方 QQ NT 已登录，在 WebUI 首页把 QQ 进程接入。
- 「节点配置 → WS 服务端」新建：监听 `127.0.0.1`、端口 `3001`、路径 `/`、
  角色 `Universal`、消息格式 `数组`、授权 Token 自己生成，关闭“上报自身消息”。
- 本程序设置里填同一个 `ws://127.0.0.1:3001` 和 Token。NapCat 与 SnowLuma 不能同时占用 3001。

## AI 设置

- **内置 DeepSeek Web2API（免费）**：默认 `http://127.0.0.1:3000/v1` + `sk-local`。
  首次启动需要登录 DeepSeek；登录态只保存在本机。登录失效时 QQ 会收到提示，
  打开托盘「设置 → AI → 重新绑定」重新登录即可。
- **OpenAI 兼容接口**：厂商预设或自定义，填 `base_url`（以 `/v1` 结尾）、`API Key`、`model`。
- **人设**：设置页的文本框就是 system prompt 的主体。修改人设后自动按“新对话”处理，
  下一条消息会重新注入人设并导入最近历史。

## 主动消息

触发来源：时段（晨间/午间/午后/晚间/深夜/周末）、天气变化、久坐 45 分钟、
90 分钟没有互动、随机闲聊。任意两条主动消息之间受「主动消息冷却间隔」限制，
设为 `0` 只保留情境触发和随机关闭。

- 主动消息只发给白名单里的 QQ；还没有人私聊绑定时会被丢弃（日志有记录）。
- 说「晚安」睡 8 小时、说「午安」睡 2 小时；睡眠期间只回固定的一句，不会主动发消息。
- 所有主动消息也会写入聊天记录，重启后仍能接上上下文。

## 聊天记录与续聊

- 记录文件：程序目录下的 `chat_history.json`，最多保留 `chat_history_max` 条。
- 每次启动后的第一条 AI 请求会带上最近 `ai_context_n` 条历史（默认 10 条），
  所以重启后还是同一个对话。
- 内置网页 AI 在服务端也保留对话；自定义 OpenAI 兼容接口则每次都携带历史。
- QQ 私聊图片会下载后交给 AI 识图（单张，最大 15 MB），文字和图片可以同发。
  识图需要 AI 模型支持视觉：内置 DeepSeek Web2API 支持；DeepSeek 开放平台的
  `deepseek-chat` 是纯文本模型，收到图片会报错或忽略。群聊仍不支持。

## 配置文件

`config.json` 与 exe 同目录（源码运行时在项目根目录）。常用项：

| 键 | 说明 |
| --- | --- |
| `ai_preset` / `ai_base_url` / `ai_api_key` / `ai_model` | AI 接口 |
| `ai_persona` | 人设（system prompt 主体） |
| `ai_context_n` | 开新对话时导入的历史条数 |
| `ai_web2api_max_messages` | 内置 DeepSeek 单对话消息上限（0=不限） |
| `self_talk_enabled` / `self_talk_interval` | 主动消息开关 / 冷却秒数 |
| `qq_bridge_enabled` | 是否启用 QQ |
| `qq_bridge_ws_url` | NapCat WS 地址，默认 `ws://127.0.0.1:3001` |
| `qq_bridge_token` | 与 NapCat 一致的 token |
| `qq_bridge_allowed_ids` | 允许的 QQ 号列表；留空自动绑定第一个私聊用户 |
| `accounts` / `poll_interval_sec` | 余额查询账号与轮询间隔（只进上下文，不显示） |

`config.json` 含 API Key 与 QQ token，已在 `.gitignore` 中排除，不要提交或分享。

## 从源码运行

```bash
pip install -r requirements.txt
python main.py
```

内置 AI 还需要在 `vendor/DeepSeekWeb2API` 里安装依赖：

```bash
cd vendor/DeepSeekWeb2API
npm install
```

`web2api.py` 会优先使用 `vendor/DeepSeekWeb2API/node/node.exe`，没有则回退到 PATH 里的 `node`。

测试（不访问网络）：

```bash
python tests/smoke.py
```

## 打包

```bash
python -m PyInstaller QQAIBackend.spec --noconfirm
```

然后把 `vendor/DeepSeekWeb2API`（含 `node/`、`node_modules/`）复制到
`dist/QQAIBackend/vendor/`，删除其中的 `data/`、`tmp/`、`web2api.log` 后再压缩发布。
发布包不要包含 `config.json`、`chat_history.json` 和登录态。

## 风险与边界

- 非官方 QQ 自动化：风控、冻结、封号风险由使用者承担，**只用小号**。
- 只支持私聊文字；不支持图片、群聊、@、撤回等事件。
- NapCat 升级后如果网络配置项名称变化，按“WS 正向服务器 / 本程序作为客户端连接”来选。
