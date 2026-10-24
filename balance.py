# -*- coding: utf-8 -*-
"""DeepSeek 开放平台余额监控：轮询多账号，增减时发信号。"""
import threading
import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from platforms import PROVIDERS

import petlog


def parse_account(acc):
    """兼容旧格式（纯字符串 = DeepSeek Key）与新格式（平台 + Key）。"""
    if isinstance(acc, str):
        return "deepseek", acc
    return acc.get("platform", "deepseek"), acc.get("api_key", "")


def normalize_total(total):
    """余额归一：|总额| < 0.005 视为 0。

    部分平台接口会返回 -0.0 / 极小负值，直接显示会变成 "-¥0.00"，
    并被余额方向逻辑误判为"余额下降=工作中"（其实根本没开始工作）。
    """
    try:
        total = float(total)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if abs(total) < 0.005 else total


class BalanceMonitor(QObject):
    balanceUpdated = pyqtSignal(float, str)  # 总额, 显示文本
    balanceUp = pyqtSignal(str)              # 增加动画文本
    balanceDown = pyqtSignal(str)            # 减少动画文本
    balanceFlat = pyqtSignal(str)            # 不变（无动画文本，仅方向）
    fetchError = pyqtSignal(str)

    # requests 的 timeout 不覆盖域名解析（getaddrinfo 无超时）：DNS 被拦截/
    # 卡死时 worker 线程会永久挂起。看门狗超过此时长即放弃本次查询并重试，
    # 旧线程为 daemon，晚到的结果仍会正常刷新显示。
    POLL_TIMEOUT_MS = 20000

    def __init__(self, config, poll_timeout_ms=None):
        super().__init__()
        self.config = config
        self._last_total = None
        self._polling = False
        self._timeouts = 0  # 连续超时次数：连续 3 次后放慢重试节奏
        self._poll_timeout_ms = poll_timeout_ms or self.POLL_TIMEOUT_MS
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_poll_timeout)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.poll)

    def start(self):
        self.set_interval(int(self.config.get("poll_interval_sec", 3)))
        self.poll()

    def set_interval(self, seconds):
        """余额轮询间隔（秒），最低 1 秒，可在设置面板调整。"""
        self._timer.start(max(1, seconds) * 1000)

    def poll(self):
        if self._polling:
            return  # 上一轮尚未结束，跳过本次，避免线程堆积
        accounts = self.config.get("accounts", {}) or {}
        if not accounts:
            self.balanceUpdated.emit(0.0, "未绑定账号")
            return
        self._polling = True
        petlog.log("balance poll start (%d accounts)" % len(accounts))
        self._watchdog.start(self._poll_timeout_ms)
        threading.Thread(target=self._worker, args=(accounts,), daemon=True).start()

    def _on_poll_timeout(self):
        """看门狗：worker 超时未归（DNS 卡死等），放弃并重试；连续超时则退避。"""
        if not self._polling:
            return  # 结果其实已经回来了（watchdog 只是晚了一步）
        self._polling = False
        petlog.log("balance poll TIMEOUT (#%d)" % self._timeouts)
        self._timeouts += 1
        self.fetchError.emit("查询超时")
        if self._timeouts >= 3:
            QTimer.singleShot(60000, self.poll)  # 连续 3 次超时后每分钟重试一次
        else:
            self.poll()

    def _worker(self, accounts):
        try:
            total = 0.0
            ok = 0
            errors = []
            for name, acc in accounts.items():
                platform, key = parse_account(acc)
                provider = PROVIDERS.get(platform)
                if provider is None:
                    errors.append("%s: 未知平台 %s" % (name, platform))
                    continue
                try:
                    amount, _currency = provider.fetch(key)
                    total += amount
                    ok += 1
                except Exception as e:
                    errors.append("%s: %s" % (name, e))
            if not ok:
                petlog.log("balance fetch all failed: %s" % "；".join(errors))
                self.fetchError.emit("；".join(errors))
                return

            total = normalize_total(total)
            text = "¥%.2f" % total
            petlog.log("balance fetch ok: %s (emit from thread %s)" % (text, threading.get_ident()))
            if self._last_total is None or abs(total - self._last_total) < 0.005:
                self.balanceUpdated.emit(total, text)
                self.balanceFlat.emit(text)
            elif total > self._last_total:
                self.balanceUpdated.emit(total, text)
                self.balanceUp.emit("+¥%.2f" % (total - self._last_total))
            else:
                self.balanceUpdated.emit(total, text)
                self.balanceDown.emit("-¥%.2f" % (self._last_total - total))
            self._last_total = total
            self._timeouts = 0
        finally:
            self._polling = False
