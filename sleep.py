# -*- coding: utf-8 -*-
"""睡眠状态机：说"午安"睡 2 小时、说"晚安"睡 8 小时。

醒来条件（谁先到算谁）：
  * 计时到点（午安 2h / 晚安 8h）；
  * 现实时间到 14:30（午安）或 08:30（晚安）。
睡着期间任何输入只回 SLEEP_TEXT（不计入聊天记录）、主动消息冻结。
状态落 config（sleep_kind / sleep_until），重启不会“睡一半自己醒了”。
"""
import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

import petlog

SLEEP_TEXT = "呼……呼……（似乎睡得正香）"
NAP_HOURS = 2
NIGHT_HOURS = 8
WAKING_CLOCK = {"nap": (14, 30), "night": (8, 30)}   # 现实时间兜底
KIND_ZH = {"nap": "午睡", "night": "夜睡"}
WAKE_PROMPTS = {
    "nap": "（你刚刚睡醒午觉，还迷迷糊糊的，揉揉眼睛、打个哈欠说句话，"
           "一两句，口语化，符合刚睡醒的状态。）",
    "night": "（你刚刚睡醒，是早晨刚起床的迷糊状态，声音软软的，"
             "道个早安，一两句，口语化。）",
}


def trigger_kind(text):
    """用户这句话里有没有"午安"/"晚安"；返回 'nap' / 'night' / ''。"""
    text = str(text or "")
    if "午安" in text:
        return "nap"
    if "晚安" in text:
        return "night"
    return ""


def next_clock(now, hh, mm):
    """从 now 往后最近的一个 hh:mm（今天没到就今天，过了就明天）。"""
    t = time.localtime(now)
    target = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hh, mm, 0, 0, 0, -1))
    return target if target > now else target + 86400


class SleepMonitor(QObject):
    """woke 信号带回睡的种类（nap/night），由主程序负责说醒来后的第一句话。"""

    woke = pyqtSignal(str)

    def __init__(self, config, now_fn=time.time):
        super().__init__()
        self.config = config
        self._now = now_fn
        self._kind = ""
        self._until = 0.0
        self._wake_pending = ""     # 重启期间睡醒的：留给主程序补一句"刚醒"
        self._restore()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.check)
        self._timer.start(30000)    # 30 秒看一眼就够（醒来那一刻差点无所谓）

    # ---------- 状态 ----------
    def is_asleep(self):
        return bool(self._kind) and self._now() < self._until

    def kind(self):
        return self._kind if self.is_asleep() else ""

    def until(self):
        return self._until if self.is_asleep() else 0.0

    def take_pending_wake(self):
        kind, self._wake_pending = self._wake_pending, ""
        return kind

    # ---------- 进出睡眠 ----------
    def put_to_sleep(self, kind):
        """按 kind（nap/night）进入睡眠；返回醒来时间戳。"""
        kind = "night" if kind == "night" else "nap"
        hours = NIGHT_HOURS if kind == "night" else NAP_HOURS
        now = self._now()
        by_timer = now + hours * 3600
        by_clock = next_clock(now, *WAKING_CLOCK[kind])
        self._kind = kind
        self._until = min(by_timer, by_clock)
        self.config.set("sleep_kind", kind)
        self.config.set("sleep_until", int(self._until))
        petlog.log("sleep: %s until %s (timer %s / clock %s)" % (
            kind, time.strftime("%m-%d %H:%M", time.localtime(self._until)),
            time.strftime("%H:%M", time.localtime(by_timer)),
            time.strftime("%H:%M", time.localtime(by_clock))))
        return self._until

    def check(self):
        """定时看一眼：到点就醒（发 woke 信号）。"""
        if not self._kind or self._now() < self._until:
            return False
        kind = self._kind
        self._clear()
        petlog.log("sleep: woke up (%s)" % kind)
        self.woke.emit(kind)
        return True

    def wake_now(self):
        """立刻叫醒（用户手动/测试用）。"""
        if not self._kind:
            return False
        kind = self._kind
        self._clear()
        self.woke.emit(kind)
        return True

    # ---------- 内部 ----------
    def _clear(self):
        self._kind = ""
        self._until = 0.0
        self.config.set("sleep_kind", "")
        self.config.set("sleep_until", 0)

    def _restore(self):
        kind = str(self.config.get("sleep_kind") or "")
        until = float(self.config.get("sleep_until", 0) or 0)
        if kind not in ("nap", "night") or not until:
            return
        if self._now() < until:
            self._kind, self._until = kind, until
            petlog.log("sleep: restored, until %s" % time.strftime(
                "%m-%d %H:%M", time.localtime(until)))
            return
        # 程序没开的时候已经睡够了：醒来该说一句，交给主程序启动后补
        self._wake_pending = kind
        self._clear()
