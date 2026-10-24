# -*- coding: utf-8 -*-
"""OneBot 11 私聊桥：NapCat/SnowLuma 收 QQ 消息 <-> 本地 AI 后端。

只负责收发消息；AI 生成、聊天记录与主动消息都由 main.py 统一调度，
这样 QQ 对话与主动消息共用同一段历史，重启后也能接着聊。
"""
import html
import ipaddress
import json
import os
import re
import ssl
import tempfile
import threading
import urllib.request
from urllib.parse import urlparse

from PyQt5.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt5.QtNetwork import QAbstractSocket, QNetworkRequest
from PyQt5.QtWebSockets import QWebSocket

import petlog

CQ_RE = re.compile(r"\[CQ:[^\]]+\]")
CQ_IMAGE_RE = re.compile(r"\[CQ:image,([^\]]+)\]")
MAX_IMAGE_BYTES = 15 * 1024 * 1024
IMAGE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


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


def _cq_params(raw):
    params = {}
    for part in raw.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            params[key.strip()] = html.unescape(value)
    return params


def extract_image(message):
    """返回 OneBot 消息里第一张可下载图片的 URL。"""
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "image":
                continue
            data = segment.get("data") or {}
            url = str(data.get("url") or data.get("file") or "").strip()
            if url.startswith(("http://", "https://")):
                return url
        return ""
    if isinstance(message, str):
        match = CQ_IMAGE_RE.search(message)
        if not match:
            return ""
        data = _cq_params(match.group(1))
        url = str(data.get("url") or data.get("file") or "").strip()
        return url if url.startswith(("http://", "https://")) else ""
    return ""


def has_mention(message, self_id):
    """群里是否 @ 了机器人。"""
    if not self_id:
        return False
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "at":
                continue
            data = segment.get("data") or {}
            if str(data.get("qq") or "") == str(self_id):
                return True
        return False
    if isinstance(message, str):
        return ("[CQ:at,qq=%s]" % self_id) in message
    return False


def _image_suffix(content_type, url):
    mime = str(content_type or "").split(";")[0].strip().lower()
    if mime in IMAGE_SUFFIXES:
        return IMAGE_SUFFIXES[mime]
    path = urlparse(url).path.lower()
    for suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        if path.endswith(suffix):
            return ".jpg" if suffix == ".jpeg" else suffix
    return ""


def download_image(url, timeout=30):
    """下载一张 QQ 图片到临时文件；失败返回空字符串。

    shortcut: 只校验协议和 IP 字面量，域名解析到内网的情况未拦截；OneBot
    默认只监听本机，换公网部署时再加 DNS 解析校验。
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ""
        try:
            ip = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            ip = None
        if ip and (ip.is_private or ip.is_loopback or ip.is_link_local
                   or ip.is_reserved or ip.is_multicast):
            petlog.log("qq bridge: refused private image url")
            return ""
        context = ssl.create_default_context()
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
            if getattr(response, "status", 200) != 200:
                return ""
            suffix = _image_suffix(response.headers.get("Content-Type"), url)
            if not suffix:
                petlog.log("qq bridge: unsupported image type")
                return ""
            length = int(response.headers.get("Content-Length") or 0)
            if length > MAX_IMAGE_BYTES:
                petlog.log("qq bridge: image too large")
                return ""
            fd, path = tempfile.mkstemp(prefix="qqai_img_", suffix=suffix)
            total = 0
            try:
                with os.fdopen(fd, "wb") as f:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise ValueError("image too large")
                        f.write(chunk)
                if total == 0:
                    raise ValueError("empty image")
                return path
            except Exception:
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise
    except Exception as e:
        petlog.log("qq bridge: image download failed: %s" % e)
        return ""


class QQBridge(QObject):
    messageReceived = pyqtSignal(str, str)  # user_id, text
    imageReceived = pyqtSignal(str, str, str)  # user_id, text, local image path
    groupMessageReceived = pyqtSignal(str, str, str, str, str, bool)
    # group_id, user_id, sender, text, local image path, mentioned

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
        self._allowed_groups = self._load_allowed_groups()
        self._running = False
        self._seq = 0
        self._seen = []
        self._seen_set = set()

    def _load_allowed(self):
        raw = self.config.get("qq_bridge_allowed_ids", [])
        if not isinstance(raw, (list, tuple, set)):
            raw = [raw]
        return {str(x).strip() for x in raw if str(x).strip()}

    def _load_allowed_groups(self):
        raw = self.config.get("qq_bridge_allowed_groups", [])
        if not isinstance(raw, (list, tuple, set)):
            raw = [raw]
        return {str(x).strip() for x in raw if str(x).strip()}

    def allowed_ids(self):
        return sorted(self._allowed)

    def allowed_groups(self):
        return sorted(self._allowed_groups)

    def start(self):
        if self._running:
            return
        self._allowed = self._load_allowed()  # 设置页改白名单后重启桥即生效
        self._allowed_groups = self._load_allowed_groups()
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
        if event.get("post_type") != "message":
            return
        if event.get("message_type") == "group":
            self._handle_group(event)
            return
        if event.get("message_type") != "private":
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
        image_url = extract_image(event.get("message"))
        if not text and not image_url:
            return
        if image_url:
            petlog.log("qq bridge: image from %s" % user_id)
            threading.Thread(target=self._fetch_image,
                             args=(user_id, text, image_url), daemon=True).start()
            return
        petlog.log("qq bridge: received %d chars from %s" % (len(text), user_id))
        self.messageReceived.emit(user_id, text)

    def _fetch_image(self, user_id, text, url):
        path = download_image(url)
        if not self._running:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return
        if not path:
            if text:
                self.messageReceived.emit(user_id, text)
            else:
                self.send_text(user_id, "图片没下载下来，再发一次吧。")
            return
        petlog.log("qq bridge: image ready from %s" % user_id)
        self.imageReceived.emit(user_id, text, path)

    def _handle_group(self, event):
        group_id = str(event.get("group_id") or "")
        user_id = str(event.get("user_id") or "")
        self_id = str(event.get("self_id") or "")
        if not group_id or not user_id or user_id == self_id:
            return
        if group_id not in self._allowed_groups:
            return
        message_id = str(event.get("message_id") or "")
        if message_id and not self._mark_seen(message_id):
            return
        message = event.get("message")
        text = extract_text(message)
        image_url = extract_image(message)
        if not text and not image_url:
            return
        sender = event.get("sender") or {}
        name = str(sender.get("card") or sender.get("nickname") or user_id)
        mentioned = has_mention(message, self_id)
        if image_url and not mentioned:
            self.groupMessageReceived.emit(group_id, user_id, name, text, "", False)
            return
        if image_url:
            threading.Thread(
                target=self._fetch_group_image,
                args=(group_id, user_id, name, text, mentioned, image_url),
                daemon=True).start()
            return
        petlog.log("qq bridge: group %s received %d chars from %s"
                   % (group_id, len(text), user_id))
        self.groupMessageReceived.emit(group_id, user_id, name, text, "", mentioned)

    def _fetch_group_image(self, group_id, user_id, name, text, mentioned, url):
        path = download_image(url)
        if not self._running:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return
        if not path:
            if text:
                self.groupMessageReceived.emit(
                    group_id, user_id, name, text, "", mentioned)
            elif mentioned:
                self.send_group_text(group_id, "图片没下载下来，再发一次吧。")
            return
        petlog.log("qq bridge: group %s image ready from %s" % (group_id, user_id))
        self.groupMessageReceived.emit(
            group_id, user_id, name, text, path, mentioned)

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

    def send_group_text(self, group_id, text):
        if not self._running or self._ws.state() != QAbstractSocket.ConnectedState:
            petlog.log("qq bridge: group send dropped (not connected)")
            return False
        target = int(group_id) if str(group_id).isdigit() else group_id
        sent = False
        for part in chunk_text(text):
            self._seq += 1
            payload = {
                "action": "send_group_msg",
                "params": {"group_id": target, "message": part, "auto_escape": True},
                "echo": "qqai-%d" % self._seq,
            }
            self._ws.sendTextMessage(json.dumps(payload, ensure_ascii=False))
            sent = True
            petlog.log("qq bridge: sent %d chars to group %s" % (len(part), group_id))
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
