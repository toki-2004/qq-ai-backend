# -*- coding: utf-8 -*-
"""OneBot 11 私聊桥：NapCat 收 QQ 消息 <-> 本地 AI 后端。

只负责收发消息；AI 生成、聊天记录与主动消息都由 main.py 统一调度，
这样 QQ 对话与主动消息共用同一段历史，重启后也能接着聊。
"""
import json
import re

from PyQt5.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt5.QtNetwork import QAbstractSocket, QNetworkRequest
from PyQt5.QtWebSockets import QWebSocket

import petlog

CQ_RE = re.compile(r"\[CQ:[^\]]+\]")


def extract_text(message):
    """OneBot 消息段只取 text；图片/表情等先忽略。"""
    if isinstance(message, list):
        parts = []
        for segment in message:
            if isinstance(segment, dict) and segment.get("type") == "text":
                data = segment.get("data") or {}
                parts.append(str(data.get("text") or ""))
        return "".join(parts).strip()
    if isinstance(message, str):
        return CQ_RE.sub("", message).strip()
    return ""


def chunk_text(text, limit=1500):
    text = str(text or "").strip()
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]


class QQBridge(QObject):
    messageReceived = pyqtSignal(str, str)  # user_id, text

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._ws = QWebSocket()
        self._ws.connected.connect(self._on_connected)
        self._ws.disconnected.connect(self._on_disconnected)
        self._ws.textMessageReceived.connect(self._on_text)
        self._ws.error.connect(self._on_error)
        self._reconnect = QTimer(self)
        self._reconnect.setSingleShot(True)
        self._reconnect.timeout.connect(self._connect)
        self._allowed = self._load_allowed()
        self._running = False
        self._seq = 0
        self._seen = []
        self._seen_set = set()

    def _load_allowed(self):
        raw = self.config.get("qq_bridge_allowed_ids", [])
        if not isinstance(raw, (list, tuple, set)):
            raw = [raw]
        return {str(x).strip() for x in raw if str(x).strip()}

    def allowed_ids(self):
        return sorted(self._allowed)

    def start(self):
        if self._running:
            return
        self._running = True
        self._connect()

    def stop(self):
        self._running = False
        self._reconnect.stop()
        self._ws.close()

    def restart(self):
        self.stop()
        self.start()

    def _connect(self):
        if not self._running:
            return
        url = str(self.config.get("qq_bridge_ws_url", "") or "").strip()
        if not url:
            petlog.log("qq bridge: empty ws url")
            return
        request = QNetworkRequest(QUrl(url))
        token = str(self.config.get("qq_bridge_token", "") or "").strip()
        if token:
            request.setRawHeader(b"Authorization", ("Bearer " + token).encode("utf-8"))
        petlog.log("qq bridge: connecting %s" % url)
        self._ws.open(request)

    def _on_connected(self):
        self._reconnect.stop()
        petlog.log("qq bridge: connected")

    def _on_disconnected(self):
        petlog.log("qq bridge: disconnected code=%s reason=%s"
                   % (self._ws.closeCode(), self._ws.closeReason()))
        self._schedule_reconnect()

    def _on_error(self, _error):
        petlog.log("qq bridge: socket error: %s" % self._ws.errorString())
        self._schedule_reconnect()

    def _schedule_reconnect(self):
        if self._running and not self._reconnect.isActive():
            self._reconnect.start(5000)

    def _on_text(self, raw):
        try:
            event = json.loads(raw)
        except Exception:
            petlog.log("qq bridge: invalid json")
            return
        if event.get("retcode"):
            petlog.log("qq bridge: onebot error %s: %s"
                       % (event.get("retcode"), event.get("message") or event.get("wording") or ""))
            return
        self.handle_event(event)

    def handle_event(self, event):
        if not isinstance(event, dict):
            return
        if event.get("post_type") != "message" or event.get("message_type") != "private":
            return
        user_id = str(event.get("user_id") or "")
        if not user_id or user_id == str(event.get("self_id") or ""):
            return
        message_id = str(event.get("message_id") or "")
        if message_id and not self._mark_seen(message_id):
            return
        if not self._allowed:
            # shortcut: 首个私聊用户自动绑定；换人时在设置里改 allowed_ids
            self._allowed.add(user_id)
            self.config.set("qq_bridge_allowed_ids", sorted(self._allowed))
            petlog.log("qq bridge: bound first private user %s" % user_id)
        if user_id not in self._allowed:
            petlog.log("qq bridge: ignored user %s" % user_id)
            return
        text = extract_text(event.get("message"))
        if not text:
            return
        petlog.log("qq bridge: received %d chars from %s" % (len(text), user_id))
        self.messageReceived.emit(user_id, text)

    def _mark_seen(self, message_id):
        if message_id in self._seen_set:
            return False
        if len(self._seen) >= 500:
            old = self._seen.pop(0)
            self._seen_set.discard(old)
        self._seen.append(message_id)
        self._seen_set.add(message_id)
        return True

    def send_text(self, user_id, text):
        if not self._running or self._ws.state() != QAbstractSocket.ConnectedState:
            petlog.log("qq bridge: send dropped (not connected)")
            return False
        target = int(user_id) if str(user_id).isdigit() else user_id
        sent = False
        for part in chunk_text(text):
            self._seq += 1
            payload = {
                "action": "send_private_msg",
                "params": {"user_id": target, "message": part, "auto_escape": True},
                "echo": "qqai-%d" % self._seq,
            }
            self._ws.sendTextMessage(json.dumps(payload, ensure_ascii=False))
            sent = True
            petlog.log("qq bridge: sent %d chars to %s" % (len(part), user_id))
        return sent

    def broadcast(self, text):
        """主动消息：发给白名单里的每个 QQ。还没人私聊绑定时只能丢弃。"""
        if not self._allowed:
            petlog.log("qq bridge: no bound user, message dropped")
            return False
        ok = False
        for user_id in sorted(self._allowed):
            ok = self.send_text(user_id, text) or ok
        return ok