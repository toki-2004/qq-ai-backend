# -*- coding: utf-8 -*-
"""OpenAI 兼容对话客户端：后台线程调 /v1/chat/completions，信号回主线程。"""
import base64
import mimetypes
import re
import threading

import requests
from PyQt5.QtCore import QObject, pyqtSignal

import petlog


# 厂商预设：选预设自动填 base_url/model，用户只需填 key（本机服务可免 key）
PRESETS = {
    "deepseek_web2api": {
        "name": "DeepSeekWeb2API（内置免费）",
        "base_url": "http://127.0.0.1:3000/v1",
        "model": "deepseek",
        "key": "sk-local",  # 与 vendor/DeepSeekWeb2API/config.json 的 apiKey 一致
    },
    "deepseek_open": {
        "name": "DeepSeek 开放平台",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "siliconflow": {
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
    },
    "kimi": {
        "name": "Kimi (Moonshot)",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
    },
    "custom": {"name": "自定义", "base_url": "", "model": ""},
}


def strip_citations(text):
    """去掉 AI 联网搜索回复里的 [citation:N] 标签（不显示、不存历史）。"""
    text = re.sub(r"\[citation:\s*\d+\]", "", str(text), flags=re.I)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def image_data_url(path):
    """本地图片 → OpenAI 多模态 data URL（内置 web2api 落盘后上传给 DeepSeek 网页）。"""
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        return "data:%s;base64,%s" % (mime, base64.b64encode(f.read()).decode("ascii"))


class AIClient(QObject):
    """chat() 发起异步请求；system prompt 在 worker 线程组装（含阻塞的系统感知调用，
    不卡 GUI）；reply 信号回 (text, ok, meta)。"""

    reply = pyqtSignal(str, bool, object)

    def __init__(self, config):
        super().__init__()
        self.config = config

    def chat(self, messages, meta=None, system_fn=None, image=None):
        """messages 不含 system 消息时传 system_fn()，在 worker 线程生成 system 前置。"""
        threading.Thread(
            target=self._worker, args=(list(messages), meta, system_fn, image), daemon=True
        ).start()

    def _worker(self, messages, meta, system_fn=None, image=None):
        try:
            system = system_fn() if system_fn else ""
        except Exception as e:
            petlog.log("system prompt build failed: %s" % e)
            system = ""
        if system:
            messages = [{"role": "system", "content": system}] + messages
        if image:
            # 图片读盘/编码留在 worker 线程，主线程不卡
            try:
                last = messages[-1]
                messages[-1] = {"role": last["role"], "content": [
                    {"type": "text", "text": last["content"]},
                    {"type": "image_url", "image_url": {"url": image_data_url(image)}},
                ]}
            except Exception as e:
                petlog.log("image attach failed: %s" % e)
        base = (self.config.get("ai_base_url") or "").rstrip("/")
        model = self.config.get("ai_model") or ""
        key = self.config.get("ai_api_key") or ""
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        # 连接超时短（服务没起来立刻失败），读超时长：内置免费 AI 读网页/思考
        # 可能一两分钟不出结果，此时并没有短路，给足时间别过早弹兜底文本。
        read_timeout = float(self.config.get("ai_timeout_s", 300) or 300)
        try:
            # 与 vendor 的 web2api.log 对时间线用：出问题能看出请求是什么时候发出去的
            petlog.log("ai request start: model=%s msgs=%d image=%s"
                       % (model, len(messages), bool(image)))
            r = requests.post(
                base + "/chat/completions",
                json={"model": model, "messages": messages},
                headers=headers,
                timeout=(10, read_timeout),
            )
            r.raise_for_status()
            text = strip_citations(r.json()["choices"][0]["message"]["content"])
            petlog.log("ai request ok: %d chars" % len(text))
            self.reply.emit(text, True, meta)
        except requests.Timeout as e:
            # 超时只代表"还没回"：会话大概率仍完好（厂商侧可能已把这条消息
            # 写进对话并在继续生成），meta 带标记供上层跳过人设重注入。
            petlog.log("ai request timed out after %ss: %s" % (read_timeout, e))
            self.reply.emit("", False, dict(meta or {}, timeout=True))
        except requests.HTTPError as e:
            # 服务端给了明确错误码（如内置 AI 登录态失效 login_required）时带上，
            # 让上层给出"去重新绑定"这类可操作提示，而不是笼统的兜底文本
            code = ""
            message = ""
            try:
                err = (e.response.json() or {}).get("error") or {}
                code = str(err.get("code") or "")
                message = str(err.get("message") or "")
            except Exception:
                pass
            petlog.log("ai request failed: HTTP %s %s"
                       % (getattr(e.response, "status_code", "?"), code))
            self.reply.emit("", False, dict(meta or {}, error_code=code,
                                            error_message=message))
        except Exception as e:
            petlog.log("ai request failed: %s" % e)
            self.reply.emit("", False, meta)
