# -*- coding: utf-8 -*-
"""对话历史：JSON 持久化（跨重启续聊）。"""
import json
import os
import time

from PyQt5.QtCore import QObject, pyqtSignal

import petlog


class ChatHistory(QObject):
    """保存全部消息（含主动消息），context() 只取参与对话的条目。"""

    changed = pyqtSignal()

    def __init__(self, path, max_n=200):
        super().__init__()
        self.path = path
        self.max_n = max(10, int(max_n or 200))
        self.items = []
        self.rev = 0
        self.load()

    def load(self):
        """读不成（写一半/被占用/坏档）时保留现场再开空表，绝不静默覆盖。"""
        data = None
        for attempt in range(3):
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                break
            except FileNotFoundError:
                self.items = []
                return
            except Exception:
                if attempt < 2:
                    time.sleep(0.3)
        if data is None:
            try:
                if os.path.exists(self.path):
                    os.replace(self.path, "%s.broken-%s" % (
                        self.path, time.strftime("%Y%m%d-%H%M%S")))
                    petlog.log("chat history unreadable, kept as .broken-*")
            except OSError:
                pass
            self.items = []
            return
        self.items = data.get("messages", []) if isinstance(data, dict) else []

    def save(self):
        """原子写：先写 .tmp 再 os.replace，读者不会读到截断的文件。"""
        if not self.items:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > 10:
                    petlog.log("skip saving empty history over a non-empty file")
                    return
            except OSError:
                pass
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"messages": self.items[-self.max_n:]},
                          f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception as e:
            petlog.log("chat history save failed: %s" % e)
            try:
                os.remove(tmp)
            except OSError:
                pass

    def append(self, role, content, kind="chat"):
        self.rev += 1
        self.items.append({
            "role": role, "content": content,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind,
        })
        self.items = self.items[-self.max_n:]
        self.save()
        self.changed.emit()

    def context(self, n):
        """最近 n 条 user/assistant 消息（OpenAI messages 格式，不含 ts）。"""
        msgs = [m for m in self.items if m.get("role") in ("user", "assistant")]
        return [{"role": m["role"], "content": m["content"]} for m in msgs[-n:]]