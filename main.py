# -*- coding: utf-8 -*-
"""QQ AI 后端：无桌宠界面，内置 AI 通过 OneBot 11 收发私聊并主动发消息。"""
import os
import sys
import tempfile
import time
from datetime import datetime

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from config import APP_VERSION, Config
import petlog

petlog.install_excepthooks()

from ai_client import AIClient, PRESETS  # noqa: E402
from balance import BalanceMonitor  # noqa: E402
from chat_history import ChatHistory  # noqa: E402
from qq_bridge import QQBridge  # noqa: E402
from running_apps import audio_apps, media_track, running_apps  # noqa: E402
from settings_dialog import SettingsDialog  # noqa: E402
import sleep  # noqa: E402
from sleep import SleepMonitor  # noqa: E402
from weather import WeatherMonitor  # noqa: E402
import web2api  # noqa: E402

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    PROJECT_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = getattr(sys, "_MEIPASS", PROJECT_DIR)
else:
    PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = PROJECT_DIR

CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")
SMOKE = bool(os.environ.get("PET_SMOKE"))


def _input_idle_seconds():
    """距上次键鼠输入的秒数（Windows GetLastInputInfo，无新依赖）。"""
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return max(0.0, (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0)
    except Exception:
        pass
    return 0.0


TAG_ZH = {
    "time_morning": "晨间时段",
    "time_noon": "午间时段",
    "time_afternoon": "午后时段",
    "time_evening": "晚间时段",
    "time_midnight": "深夜时段",
    "time_weekend": "周末时段",
    "health": "久坐健康提醒",
    "attention": "有一阵子没互动了",
    "weather_sunny": "天气晴朗",
    "weather_cloudy": "天气多云",
    "weather_rainy": "天气有雨",
    "weather_snowy": "天气有雪",
    "weather_foggy": "天气有雾",
    "weather_windy": "天气有风",
    "weather_stormy": "天气有风暴",
    "weather": "天气变化",
    "random": "随机闲聊",
}


class SelfTalkMonitor(QObject):
    """情境主动消息：时段/健康/求关注/天气/随机触发后发出 request_talk。"""

    request_talk = pyqtSignal(str)

    TIME_WINDOWS = [
        (5, 11, "time_morning"),
        (11, 14, "time_noon"),
        (14, 18, "time_afternoon"),
        (18, 23, "time_evening"),
        (23, 24, "time_midnight"),
        (0, 5, "time_midnight"),
    ]
    WEEKEND_WINDOW = (9, 21)
    IDLE_MINUTES = 45
    ATTENTION_MINUTES = 90
    WEATHER_KINDS = ("sunny", "cloudy", "rainy", "snowy", "foggy", "windy", "stormy")

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._fire_random)
        self._context_timer = QTimer(self)
        self._context_timer.timeout.connect(self._check_context)
        self._last_talk = 0.0
        self._pending = []
        self._last_attention = time.monotonic()
        self._last_pat = time.monotonic()
        self._last_health = 0.0
        self._fired_rules = {}
        self._last_weather_kind = ""
        self._now_fn = datetime.now
        self._mono_fn = time.monotonic
        self._idle_fn = _input_idle_seconds
        self._schedule()

    def _emit_tag(self, tag, hold=False):
        if not bool(self.config.get("self_talk_enabled", True)):
            return False
        if not self._gate_open():
            if hold and tag not in self._pending:
                self._pending.append(tag)
            return False
        self._last_talk = self._mono_fn()
        self.request_talk.emit(tag)
        return True

    def _interval(self):
        val = self.config.get("self_talk_interval", 300)
        try:
            sec = int(val if val is not None else 300)
        except (TypeError, ValueError):
            sec = 300
        return max(0, sec)

    def _gate_open(self):
        return self._mono_fn() - self._last_talk >= self._interval()

    def _fire_random(self):
        self._check_context()
        if not self._gate_open():
            self._schedule()
            return
        if self._pending:
            self._emit_tag(self._pending.pop(0))
        else:
            self._emit_tag("random")
        self._schedule()

    def _check_context(self):
        now = self._now_fn()
        self._check_time_windows(now)
        self._check_health()
        self._check_attention()

    def _check_time_windows(self, now):
        today = now.date().isoformat()
        weekend = now.weekday() >= 5
        rules = []
        for h1, h2, sub in self.TIME_WINDOWS:
            if h1 <= now.hour < h2:
                rules.append(sub)
        if weekend and self.WEEKEND_WINDOW[0] <= now.hour < self.WEEKEND_WINDOW[1]:
            rules.append("time_weekend")
        for rule in rules:
            if self._fired_rules.get(rule) == today:
                continue
            if self._emit_tag(rule):
                self._fired_rules[rule] = today

    def _check_health(self):
        mono = self._mono_fn()
        idle = self._idle_fn()
        if idle >= self.IDLE_MINUTES * 60 and mono - self._last_health >= self.IDLE_MINUTES * 60:
            if self._emit_tag("health"):
                self._last_health = mono

    def _check_attention(self):
        mono = self._mono_fn()
        if (mono - self._last_pat >= self.ATTENTION_MINUTES * 60
                and mono - self._last_attention >= self.ATTENTION_MINUTES * 60):
            if self._emit_tag("attention"):
                self._last_attention = mono

    def set_weather(self, kind):
        if kind == self._last_weather_kind:
            return
        self._last_weather_kind = kind
        if not bool(self.config.get("self_talk_enabled", True)):
            return
        tag = ("weather_" + kind) if kind in self.WEATHER_KINDS else "weather"
        self._emit_tag(tag, hold=True)

    def note_interaction(self):
        now = self._mono_fn()
        self._last_pat = now
        self._last_talk = now

    def _schedule(self):
        enabled = bool(self.config.get("self_talk_enabled", True))
        sec = self._interval()
        if not enabled:
            self._timer.stop()
            self._context_timer.stop()
            self._pending[:] = []
            return
        if sec > 0:
            self._timer.start(sec * 1000)
        else:
            self._timer.stop()
            self._pending[:] = []
        if not self._context_timer.isActive():
            self._context_timer.start(60000)


class QQAI:
    STARTUP_AI_DELAY_S = 10.0
    FAIL_HINTS = {
        "login_required":
            "内置 AI 的 DeepSeek 登录态失效了，去托盘「设置 → AI」点「重新绑定」重新登录一下吧。",
        "deepseek_not_logged_in":
            "内置 AI 好像没进到对话界面，去托盘「设置 → AI」点「重新绑定」重新登录一下吧。",
        "empty_response": "这次没拿到回复内容（网页端偶尔会这样），等一下再说一次试试？",
        "deepseek_timeout": "这条回复等了 5 分钟还没生成完，我先不等了；可以再问一次。",
    }

    def __init__(self):
        self.config = Config(CONFIG_PATH)
        self._start_mono = time.monotonic()
        if (self.config.get("ai_preset") == "deepseek_web2api"
                and str(self.config.get("ai_base_url", "")).endswith(":8000/v1")):
            self.config.set("ai_base_url", "http://127.0.0.1:3000/v1")
        if (self.config.get("ai_preset") == "deepseek_web2api"
                and not self.config.get("ai_api_key")):
            self.config.set("ai_api_key", PRESETS["deepseek_web2api"]["key"])

        self.talk = SelfTalkMonitor(self.config)
        self.ai = AIClient(self.config)
        hist_path = os.path.join(PROJECT_DIR, "chat_history.json")
        if SMOKE:
            hist_path = os.path.join(tempfile.gettempdir(), "qqai_smoke_history.json")
        self.history = ChatHistory(hist_path, self.config.get("chat_history_max", 200))
        self.sleep = SleepMonitor(self.config)
        self.weather = WeatherMonitor(self.config)
        self.balance = BalanceMonitor(self.config)
        self.qq = QQBridge(self.config)
        self.qq.messageReceived.connect(self._on_qq_message)
        self.web2api = web2api.Manager()
        self.web2api.status.connect(self._on_web2api_status)
        self._last_weather = ""
        self._last_balance = ""
        self._settings_dlg = None
        self._tray = None
        self._pending_sleep = ""
        self._convo_primed = False
        self._ai_busy = False
        self._ai_queue = []
        self._persona_active = str(self.config.get("ai_persona", "")).strip()
        self._wire()
        self._apply_sleep_state()
        if not SMOKE:
            self.weather.start()
            self.balance.start()
            if (self.config.get("ai_preset") == "deepseek_web2api"
                    and os.path.isdir(web2api.VENDOR_DIR)):
                self.web2api.ensure_async()
            if bool(self.config.get("qq_bridge_enabled", False)):
                self.qq.start()
            self._setup_tray()

    # ---------- 信号 ----------
    def _wire(self):
        self.talk.request_talk.connect(self._on_selftalk_tag)
        self.sleep.woke.connect(self._on_wake)
        self.weather.weatherUpdated.connect(self._on_weather_updated)
        self.weather.weatherChanged.connect(self.talk.set_weather)
        self.weather.weatherError.connect(lambda msg: petlog.log("weather error: %s" % msg))
        self.balance.balanceUpdated.connect(self._on_balance_updated)
        self.balance.fetchError.connect(lambda msg: petlog.log("balance error: %s" % msg))
        self.ai.reply.connect(self._on_ai_reply)

    def _on_weather_updated(self, kind, detail):
        self._last_weather = detail

    def _on_balance_updated(self, total, text):
        accounts = self.config.get("accounts", {}) or {}
        if text == "未绑定账号" or not accounts:
            self._last_balance = ""
        else:
            self._last_balance = text

    def _on_web2api_status(self, ok, msg):
        if msg:
            self._convo_primed = False
        if msg == "login":
            self._notify("即将清空旧登录态并打开全新的 DeepSeek 登录浏览器；登录完成后关闭那个控制台窗口。")
            if self._tray is not None:
                self._tray.showMessage("需要登录 DeepSeek", "请完成登录，未绑定前 AI 无法回复。",
                                       QSystemTrayIcon.Information, 8000)
            return
        if msg:
            self._notify(msg)

    # ---------- AI ----------
    def _system_prompt(self, tag="", persona=True):
        parts = []
        if persona:
            text = str(self.config.get("ai_persona", "")).strip()
            if text:
                parts.append(text)
        parts.append(
            "你是一个有真实生活感的人，有自己的生活、性格、情绪和判断。"
            "你和对方是平等的，没有主仆或从属关系；怎么称呼对方、保持怎样的距离，"
            "由你自己的感受决定。"
            "你们此刻通过 QQ 私聊，你看不到对方的表情和周围环境，只能读到文字。"
            "你能自然感知到这台电脑上的时间、正在用的应用、声音和天气；"
            "这些是你此刻知道的事，聊天时顺手用，不要逐条播报，也不要解释自己是怎么知道的。"
        )
        parts.append("当前时间：%s。" % time.strftime("%Y-%m-%d %H:%M:%S"))
        apps = [a for a in running_apps()
                if "DesktopPet.exe" not in a and "桌宠" not in a]
        if apps:
            parts.append("这台电脑上当前打开着这些应用（排最前的为前台窗口）：%s。"
                         % "；".join(apps))
        aud = audio_apps()
        if aud:
            parts.append("正在发声的应用（含后台播放的音乐/视频）：%s。" % "、".join(aud))
        tr = media_track()
        if tr:
            parts.append("正在播放音乐：%s - %s（%s）。"
                         % (tr.get("title"), tr.get("artist"), tr.get("app")))
        if self._last_weather:
            parts.append("你所在城市的天气（每小时更新）：%s。" % self._last_weather)
        if self._last_balance:
            parts.append("当前账户余额合计：%s。" % self._last_balance)
        if tag:
            parts.append("你这次主动开口的缘由：%s。" % TAG_ZH.get(tag, tag))
        parts.append(
            "像真人聊天一样自然回应：口语化，可以有自己的小情绪和追问；"
            "不必每次只说一两句话，但别写成报告、长篇独白或重复上面的信息。"
        )
        return "\n".join(parts)

    def _ask_ai(self, messages, meta=None):
        meta = dict(meta or {})
        remain = (self._start_mono + self.STARTUP_AI_DELAY_S) - time.monotonic()
        if remain > 0:
            QTimer.singleShot(int(remain * 1000) + 20,
                              lambda: self._ask_ai(messages, meta))
            return
        self._ai_queue.append((list(messages), meta))
        self._pump_ai()

    def _pump_ai(self):
        if self._ai_busy or not self._ai_queue:
            return
        messages, meta = self._ai_queue.pop(0)
        self._ai_busy = True
        try:
            builtin = self.config.get("ai_preset") == "deepseek_web2api"
            fresh = not builtin or not self._convo_primed
            if fresh:
                self._convo_primed = True
            n = int(self.config.get("ai_context_n", 10) or 10)
            ctx = self.history.context(n)
            if meta.get("skip_history_last") and ctx:
                ctx = ctx[:-1]
            msgs = (ctx if fresh else []) + messages
            self.ai.chat(
                msgs, meta,
                system_fn=lambda: self._system_prompt(meta.get("tag", ""), persona=fresh))
        except Exception as e:
            petlog.log("ai dispatch failed: %s" % e)
            self._ai_busy = False
            self._pump_ai()

    def _on_ai_reply(self, text, ok, meta):
        meta = meta or {}
        kind = meta.get("kind", "chat")
        self._ai_busy = False
        if self._pending_sleep and kind == "chat":
            pending, self._pending_sleep = self._pending_sleep, ""
            self._enter_sleep(pending)
        if not ok:
            if not meta.get("timeout"):
                self._convo_primed = False
            if bool(self.config.get("ai_fallback_enabled", True)):
                self._deliver(meta, self._failure_hint(meta))
        else:
            self.history.append("assistant", text, kind=kind)
            self._deliver(meta, text)
        self._pump_ai()

    def _deliver(self, meta, text):
        user_id = str(meta.get("user_id") or "")
        if user_id:
            self.qq.send_text(user_id, text)
        else:
            self.qq.broadcast(text)

    def _failure_hint(self, meta):
        if meta.get("timeout"):
            return "这条回复超过 5 分钟还没出来，我先不等了；等一下再问一次吧。"
        code = str(meta.get("error_code") or "")
        if code in self.FAIL_HINTS:
            return self.FAIL_HINTS[code]
        return str(self.config.get("ai_fallback_text", "唔……我现在有点短路了"))

    # ---------- QQ 消息 ----------
    def _on_qq_message(self, user_id, text):
        if self.sleep.is_asleep():
            self.qq.send_text(user_id, sleep.SLEEP_TEXT)
            return
        self.talk.note_interaction()
        kind = sleep.trigger_kind(text)
        if kind:
            self._pending_sleep = kind
        self.history.append("user", text)
        self._ask_ai([{"role": "user", "content": text}],
                     {"kind": "chat", "user_id": user_id, "skip_history_last": True})

    # ---------- 主动消息 ----------
    def _on_selftalk_tag(self, tag):
        reason = TAG_ZH.get(tag, tag)
        self._ask_ai(
            [{"role": "user",
              "content": "（你想主动找对方搭话。缘由：%s。请像一个真实的人那样"
                         "自然开口：结合你感知到的情境（正在播放的音乐、当前"
                         "打开的应用、天气等）说一句应景的话，口语化，不要生硬"
                         "复述背景文字。）" % reason}],
            {"kind": "selftalk", "tag": tag})

    def _on_wake(self, kind):
        petlog.log("sleep: wake line for %s" % kind)
        self._ask_ai([{"role": "user", "content": sleep.WAKE_PROMPTS.get(kind, "")}],
                     {"kind": "wake"})

    def _enter_sleep(self, kind):
        until = self.sleep.put_to_sleep(kind)
        petlog.log("sleep: entered %s until %s" % (
            kind, time.strftime("%m-%d %H:%M", time.localtime(until))))

    def _apply_sleep_state(self):
        if self.sleep.is_asleep():
            petlog.log("sleep: still asleep at startup")
            return
        kind = self.sleep.take_pending_wake()
        if kind:
            QTimer.singleShot(4000, lambda: self._on_wake(kind))

    def _notify(self, text):
        """系统/状态类主动消息：同样走 QQ 白名单。"""
        petlog.log("notify: %s" % text)
        self.qq.broadcast(text)

    # ---------- 设置 ----------
    def _open_settings(self):
        if self._settings_dlg is not None:
            try:
                if not self._settings_dlg.isVisible():
                    self._settings_dlg.show()
                self._settings_dlg.raise_()
                self._settings_dlg.activateWindow()
                return
            except RuntimeError:
                self._settings_dlg = None
        dlg = SettingsDialog(self.config, rebind_callback=self._rebind_web2api)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.destroyed.connect(self._on_settings_destroyed)
        dlg.accepted.connect(self._on_settings_applied)
        self._settings_dlg = dlg
        dlg.show()

    def _rebind_web2api(self):
        self.web2api.rebind_async()

    def _on_settings_destroyed(self):
        self._settings_dlg = None

    def _on_settings_applied(self):
        self._settings_dlg = None
        self.talk._schedule()
        self.balance.set_interval(int(self.config.get("poll_interval_sec", 3)))
        self.balance.poll()
        builtin = self.config.get("ai_preset") == "deepseek_web2api"
        if builtin:
            web2api.apply_max_messages(self.config.get("ai_web2api_max_messages", 20))
            if os.path.isdir(web2api.VENDOR_DIR):
                self.web2api.ensure_async()
        self._new_conversation_if_persona_changed()
        if bool(self.config.get("qq_bridge_enabled", False)):
            self.qq.restart()
        else:
            self.qq.stop()

    def _new_conversation_if_persona_changed(self):
        persona = str(self.config.get("ai_persona", "")).strip()
        if persona == self._persona_active:
            return
        self._persona_active = persona
        self._convo_primed = False
        petlog.log("persona changed: starting a new conversation")
        if self.config.get("ai_preset") == "deepseek_web2api":
            web2api.restart_async()
        self._notify("人设已换，下一条消息起新开一段对话。")

    # ---------- 托盘 ----------
    def _setup_tray(self):
        icon_path = os.path.join(BUNDLE_DIR, "icon.png")
        if not os.path.isfile(icon_path):
            icon_path = os.path.join(PROJECT_DIR, "icon.png")
        icon = QIcon(icon_path) if os.path.isfile(icon_path) else QIcon.fromTheme("applications-games")
        tray = QSystemTrayIcon(icon)
        menu = QMenu()
        menu.addAction("设置…", self._open_settings)
        menu.addSeparator()
        menu.addAction("退出", QApplication.instance().quit)
        tray.setContextMenu(menu)
        tray.setToolTip("QQ AI 后端 v%s" % APP_VERSION)
        tray.activated.connect(
            lambda reason: self._open_settings() if reason == QSystemTrayIcon.Trigger else None)
        tray.show()
        self._tray = tray
        petlog.log("tray ready")
        tray.showMessage("QQ AI 后端已启动",
                         "右键托盘图标打开设置；首次使用请先私聊机器人 QQ 完成绑定。",
                         QSystemTrayIcon.Information, 5000)


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("QQAIBackend")
    app.setQuitOnLastWindowClosed(False)
    backend = QQAI()
    if SMOKE:
        QTimer.singleShot(3500, app.quit)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()