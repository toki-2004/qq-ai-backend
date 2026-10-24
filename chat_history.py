# -*- coding: utf-8 -*-
"""对话历史：JSON 持久化（跨重启续聊）。"""
import json
import os
import re
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


class HistoryStore:
    """按会话分文件：每个私聊 QQ、每个群各一份历史。"""

    def __init__(self, base_dir, max_n=200, legacy_path=None):
        self.base_dir = base_dir
        self.max_n = max(10, int(max_n or 200))
        self.legacy_path = legacy_path
        self._cache = {}
        self._legacy_seeded = False
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, key):
        safe = re.sub(r"[^0-9A-Za-z_-]", "_", str(key or "proactive"))
        return os.path.join(self.base_dir, safe + ".json")

    def _normalize(self, key, hist):
        if key.startswith("private-"):
            prefix = key[len("private-"):] + "/私聊："
        elif key.startswith("group-"):
            prefix = key[len("group-"):] + "/群聊："
        else:
            return False
        changed = False
        for item in hist.items:
            if item.get("role") != "user":
                continue
            content = str(item.get("content") or "")
            if not content.startswith(prefix):
                item["content"] = prefix + content
                changed = True
        return changed

    def get(self, key):
        key = str(key or "proactive")
        if key in self._cache:
            return self._cache[key]
        hist = ChatHistory(self._path(key), self.max_n)
        if self._normalize(key, hist):
            hist.save()
        self._cache[key] = hist
        return hist

    def private(self, user_id):
        key = "private-%s" % user_id
        seed = not self._legacy_seeded
        self._legacy_seeded = True
        hist = self.get(key)
        if seed and not hist.items and self.legacy_path and os.path.exists(self.legacy_path):
            old = ChatHistory(self.legacy_path, self.max_n)
            if old.items:
                hist.items = old.items[-self.max_n:]
                self._normalize(key, hist)
                hist.save()
                petlog.log("history migrated to %s" % hist.path)
        return hist

    def group(self, group_id):
        return self.get("group-%s" % group_id)

    def proactive(self):
        return self.get("proactive")
