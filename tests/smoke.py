# -*- coding: utf-8 -*-
"""QQ AI 后端回归测试：python tests/smoke.py（自动 offscreen，不访问网络）。"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PET_SMOKE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from config import APP_VERSION, DEFAULT_CONFIG, DEFAULT_PERSONA  # noqa: E402
from chat_history import ChatHistory  # noqa: E402
import qq_bridge as qq_mod  # noqa: E402

app = QApplication([])
_passed = 0
_failed = []


def check(name, cond):
    global _passed
    if cond:
        _passed += 1
        print("  PASS", name)
    else:
        _failed.append(name)
        print("  FAIL", name)


class FakeCfg:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


# 1. 配置只保留后端项
check("version is set", APP_VERSION == "0.1.3")
for gone in ("affection_enabled", "work_label_enabled", "pet_image",
             "webchat_enabled", "balloon_text", "auto_start"):
    check("removed GUI config: %s" % gone, gone not in DEFAULT_CONFIG)
check("qq bridge enabled by default", DEFAULT_CONFIG.get("qq_bridge_enabled") is True)
check("persona is equal and has no pet wording",
      "平等" in DEFAULT_PERSONA and "桌宠" not in DEFAULT_PERSONA
      and "主人" not in DEFAULT_PERSONA)

# 2. OneBot 消息解析 / 发送
check("extracts text segments", qq_mod.extract_text([
    {"type": "text", "data": {"text": "你"}},
    {"type": "image", "data": {"url": "x"}},
    {"type": "text", "data": {"text": "好"}},
]) == "你好")
check("strips CQ codes", qq_mod.extract_text("hi[CQ:face,id=1]") == "hi")
check("chunks long replies", len(qq_mod.chunk_text("x" * 3100)) == 3)
check("extracts image url", qq_mod.extract_image([
    {"type": "text", "data": {"text": "看图"}},
    {"type": "image", "data": {"url": "https://example.com/a.jpg"}},
]) == "https://example.com/a.jpg")
check("extracts CQ image url", qq_mod.extract_image(
    "看图[CQ:image,file=a.jpg,url=https://example.com/a.jpg]")
    == "https://example.com/a.jpg")
check("rejects non-http image downloads",
      qq_mod.download_image("file:///tmp/a.jpg") == "")

cfg = FakeCfg()
br = qq_mod.QQBridge(cfg)
received = []
br.messageReceived.connect(lambda u, t: received.append((u, t)), Qt.DirectConnection)
br.handle_event({"post_type": "message", "message_type": "private", "self_id": 1,
                 "user_id": 2, "message_id": 10,
                 "message": [{"type": "text", "data": {"text": "hi"}}]})
check("binds first private user", br.allowed_ids() == ["2"]
      and cfg.data["qq_bridge_allowed_ids"] == ["2"]
      and received == [("2", "hi")])
br.handle_event({"post_type": "message", "message_type": "private", "self_id": 1,
                 "user_id": 2, "message_id": 10,
                 "message": [{"type": "text", "data": {"text": "hi"}}]})
br.handle_event({"post_type": "message", "message_type": "private", "self_id": 1,
                 "user_id": 3, "message_id": 11,
                 "message": [{"type": "text", "data": {"text": "other"}}]})
check("dedupes and ignores non-whitelisted users", len(received) == 1
      and br.allowed_ids() == ["2"])
br.handle_event({"post_type": "message", "message_type": "group", "self_id": 1,
                 "user_id": 2, "message_id": 12, "message": "group"})
check("ignores group messages", len(received) == 1)
sent = []
br.send_text = lambda uid, text: sent.append((uid, text)) or True
br._allowed = {"3", "2"}
br.broadcast("hello")
check("broadcast reaches every whitelisted user in order",
      sent == [("2", "hello"), ("3", "hello")])
br2 = qq_mod.QQBridge(FakeCfg())
check("broadcast without binding is dropped safely", br2.broadcast("x") is False)

image_calls = []
br._running = True
br._fetch_image = lambda uid, text, url: image_calls.append((uid, text, url))
br.handle_event({"post_type": "message", "message_type": "private", "self_id": 1,
                 "user_id": 2, "message_id": 13,
                 "message": [{"type": "image",
                              "data": {"url": "https://example.com/a.jpg"}}]})
QTest.qWait(100)
check("image messages are accepted",
      image_calls == [("2", "", "https://example.com/a.jpg")])
br._running = False

br3 = qq_mod.QQBridge(FakeCfg({"qq_bridge_allowed_ids": ["1"]}))
br3._connect = lambda: None
br3.config.data["qq_bridge_allowed_ids"] = ["9"]
br3.start()
check("start reloads the whitelist after settings change", br3.allowed_ids() == ["9"])
br3.stop()

# 3. 聊天记录持久化与上下文
tmp = tempfile.mkdtemp()
hist_path = os.path.join(tmp, "hist.json")
h1 = ChatHistory(hist_path, 200)
h1.append("user", "第一句")
h1.append("assistant", "第一句回复")
h2 = ChatHistory(hist_path, 200)
check("history survives reload",
      h2.context(10) == [{"role": "user", "content": "第一句"},
                         {"role": "assistant", "content": "第一句回复"}])
h2.append("assistant", "主动消息", kind="selftalk")
check("proactive messages stay in history", h2.context(10)[-1]["content"] == "主动消息")

# 4. 主动消息触发器
import main as main_mod  # noqa: E402

t = main_mod.SelfTalkMonitor(FakeCfg({
    "self_talk_enabled": True, "self_talk_interval": 0}))
tags = []
t.request_talk.connect(tags.append, Qt.DirectConnection)
t.set_weather("sunny")
t.set_weather("sunny")
t.set_weather("rainy")
check("weather triggers only on change", tags == ["weather_sunny", "weather_rainy"])
tags.clear()

class FixedNow:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt

from datetime import datetime  # noqa: E402

t._now_fn = FixedNow(datetime(2026, 10, 24, 9, 0))
t._fired_rules = {}
t._check_time_windows(t._now_fn())
first = list(tags)
t._check_time_windows(t._now_fn())
check("time window fires once per day",
      "time_morning" in first and tags == first)

# 5. 后端主流程：QQ 消息 -> AI -> 回复；重启后导入历史
smoke_hist = os.path.join(tempfile.gettempdir(), "qqai_smoke_history.json")
if os.path.exists(smoke_hist):
    os.remove(smoke_hist)
b = main_mod.QQAI()
check("backend starts headless", b._tray is None and b.qq is not None)
out = []
b.qq.send_text = lambda uid, text: out.append(("user", uid, text)) or True
b.qq.broadcast = lambda text: out.append(("broadcast", None, text)) or True
captured = []
b.ai.chat = lambda messages, meta=None, system_fn=None, image=None: captured.append(
    (messages, meta, system_fn, image))
b._start_mono -= 11
b._on_qq_message("123", "在吗")
check("QQ message is recorded and sent to the AI queue",
      b.history.items[-1]["content"] == "在吗"
      and captured and captured[-1][1]["user_id"] == "123")
meta = captured[-1][1]
b._on_ai_reply("在。", True, meta)
check("AI reply is recorded and sent back to the user",
      b.history.items[-1]["content"] == "在。"
      and out[-1] == ("user", "123", "在。"))

b._convo_primed = False
b._ai_busy = False
captured.clear()
b.history.append("user", "继续")
b._ask_ai([{"role": "user", "content": "继续"}],
          {"kind": "chat", "user_id": "123", "skip_history_last": True})
msgs = captured[-1][0]
check("new conversation imports prior history",
      len(msgs) > 1 and any(m["role"] == "assistant" for m in msgs))
prompt = captured[-1][2]()
check("prompt knows it is chatting through QQ and can see images",
      "QQ" in prompt and "图片" in prompt)

b._ai_busy = False
captured.clear()
b._ask_ai([{"role": "user", "content": "再说"}],
          {"kind": "chat", "user_id": "123", "skip_history_last": True})
check("built-in follow-up reuses the server-side conversation",
      len(captured[-1][0]) == 1)

img = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
img.write(b"png")
img.close()
b._ai_busy = False
captured.clear()
b._on_qq_image("123", "看看这个", img.name)
check("QQ image is queued with its text",
      captured and captured[-1][3] == img.name
      and b.history.items[-1]["content"] == "看看这个")
b._on_ai_reply("看到了。", True, captured[-1][1])
check("image temp file is removed after reply", not os.path.exists(img.name))

img2 = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
img2.write(b"png")
img2.close()
b._ai_busy = False
captured.clear()
b._on_qq_image("123", "", img2.name)
check("image-only message gets a placeholder",
      b.history.items[-1]["content"] == "（对方发来一张图片）")
b._on_ai_reply("看到了。", True, captured[-1][1])

b._ai_busy = False
captured.clear()
out.clear()
b._on_selftalk_tag("weather_sunny")
b._on_ai_reply("今天天气不错。", True, captured[-1][1])
check("proactive speech is broadcast through QQ",
      out[-1] == ("broadcast", None, "今天天气不错。"))

# 6. 设置保存：只写后端字段
from settings_dialog import SettingsDialog  # noqa: E402

cfg = FakeCfg(dict(DEFAULT_CONFIG))
dlg = SettingsDialog(cfg)
dlg.qq_allowed.setText("123, 456")
dlg._save()
check("settings parse whitelist and keep a version",
      cfg.get("qq_bridge_allowed_ids") == ["123", "456"]
      and dlg.version_label.text() == "版本 v0.1.3")

b._open_settings()
opened = b._settings_dlg
check("settings opens from the backend", opened is not None and opened.isVisible())
opened.hide()
b._open_settings()
check("hidden settings window is shown again", b._settings_dlg is opened
      and opened.isVisible())
opened.reject()

app.quit()
print("\nRESULT: %d passed, %d failed" % (_passed, len(_failed)))
if _failed:
    print("FAILED:", "; ".join(_failed))
    sys.exit(1)
