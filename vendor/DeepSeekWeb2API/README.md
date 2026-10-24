# DeepSeekWeb2API

把 DeepSeek 网页版封装成 OpenAI 兼容 API，支持文本、图片输入和流式输出。

参考项目：[foxhui/WebAI2API](https://github.com/foxhui/WebAI2API)。

## 便携包运行

目录内已经包含便携 Node，正常使用时直接双击脚本即可。

- `00登录.bat`：打开 DeepSeek 登录浏览器，用于首次登录或登录失效后重新登录
- `01启动.bat`：后台启动 API 服务，启动后窗口会倒计时自动关闭
- `02停止.bat`：按 `config.json` 里的端口查找并停止后台服务
- `config.json`：主配置文件

打包或复制给别人时，保留这些目录和文件：

```text
DeepSeekWeb2API/
  node/
  node_modules/
  src/
  config.json
  00登录.bat
  01启动.bat
  02停止.bat
  package.json
  package-lock.json
```

首次使用先双击 `00登录.bat`，在弹出的浏览器窗口里完成 DeepSeek 登录。登录完成后可按 `Ctrl+C` 关闭登录控制台。

之后双击 `01启动.bat` 启动 API。启动成功后服务会继续在后台运行，命令行窗口会在倒计时结束后自动关闭。如需关闭服务，双击 `02停止.bat`。

启动日志会写入：

```text
DeepSeekWeb2API/logs/
```

## 浏览器

项目通过 Playwright 启动受控浏览器，这样才能保存登录态并自动操作 DeepSeek 网页。当前默认配置会优先使用系统默认浏览器：

```json
"browser": {
  "headless": false,
  "channel": "auto",
  "prefer": "default",
  "executablePath": ""
}
```

配置说明：

- `browser.prefer`：`default`、`chrome`、`edge`
- `browser.channel`：`auto`、`msedge`、`chrome`
- `browser.executablePath`：手动指定浏览器 exe 路径，留空则自动检测
- `browser.headless`：是否无界面运行，登录时建议保持 `false`

`prefer: "default"` 会读取 Windows 的默认 `http/https` 浏览器。如果默认浏览器是 Chrome、Edge、Brave、Vivaldi、Opera 或 Chromium，会直接使用它。若默认浏览器不是 Chromium 系列，例如 Firefox，会回退到 Chrome，再回退到 Playwright Chromium。

## 配置

主要配置都在根目录 `config.json`。

- `server.apiKey`：API Key，对应请求头 `Authorization: Bearer ...`
- `server.host` / `server.port`：监听地址和端口
- `server.publicBaseUrl`：对外展示的 API 地址
- `deepseek.url`：DeepSeek 网页入口
- `paths.userDataDir`：浏览器登录态目录
- `paths.tempDir`：临时文件目录
- `limits`：请求超时、上传超时、图片数量和请求体大小限制
- `models`：对外暴露的模型名称、别名和能力开关

模型名称可以在 `models[].id` 中自定义。调用接口时使用这个 `id` 作为 `model`。`aliases` 是兼容别名，`owned_by` 可能会被部分客户端用于分组显示。

## 接口

### `GET /v1/models`

返回 `config.json` 中配置的模型列表。

### `POST /v1/chat/completions`

文本示例：

```bash
curl http://127.0.0.1:3000/v1/chat/completions \
  -H "Authorization: Bearer sk-local" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"deepseek\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with OK\"}]}"
```

图片示例：

```json
{
  "model": "deepseek-vision",
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "这张图里有什么？" },
        { "type": "image_url", "image_url": { "url": "data:image/png;base64,..." } }
      ]
    }
  ]
}
```

流式输出示例：

```json
{
  "model": "deepseek",
  "stream": true,
  "messages": [
    { "role": "user", "content": "写五行 HTTP streaming 简介" }
  ]
}
```

`stream: true` 会通过 Chromium CDP 读取 DeepSeek 网页 `chat/completion` 的增量数据，并实时转换成 OpenAI `chat.completion.chunk`。

## 注意事项

- 同一浏览器页面会串行处理请求，避免多个请求同时操作网页。
- 图片支持 `data:image/...;base64,...` 和公网 `http(s)` 图片 URL。
- 历史消息会合并进当前 prompt，因为网页自动化无法直接设置 OpenAI 风格的独立上下文。
- 这是网页封装，不是官方 DeepSeek API。DeepSeek 网页 DOM 或灰度功能变化时，选择器可能需要更新。
