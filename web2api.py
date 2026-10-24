# -*- coding: utf-8 -*-
"""内置 DeepSeekWeb2API 生命周期管理。

vendor/DeepSeekWeb2API 是 D:\\pythonitems\\DeepSeekWeb2API 的本地拷贝
（便携 Node + 源码，GitHub 不入库）。职责：
- 探测 127.0.0.1:3000 是否已有 web2api 服务（自己的或用户手启的都直接用）；
- 未绑定（vendor 无登录态）时以可见控制台跑 --login，首次启动弹浏览器登录；
- 重新绑定会先清空登录档案，弹出的浏览器无登录态，可直接登录/切换账号；
- 已绑定则后台静默启动服务，桌宠退出时停止自己拉起的进程。
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import requests
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

import petlog

if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(_BASE, "vendor", "DeepSeekWeb2API")
PORT = 3000
BASE_URL = "http://127.0.0.1:%d" % PORT

_proc = None  # 自己拉起的服务进程，退出时回收


def _node_exe():
    exe = os.path.join(VENDOR_DIR, "node", "node.exe")
    return exe if os.path.exists(exe) else "node"


def is_bound():
    """登录态是否存在（data/user-data 下有内容）。"""
    d = os.path.join(VENDOR_DIR, "data", "user-data")
    try:
        return os.path.isdir(d) and bool(os.listdir(d))
    except OSError:
        return False


def clear_login_state():
    """把旧登录浏览器档案改名备份，再让 --login 弹出无登录态的全新浏览器。

    以前是直接 rmtree：一旦重新绑定，旧档案（含本地缓存/登录态）就永久没了，
    而"重新绑定"本意只是换一次登录，不该销毁用户数据。现在只改名，可回滚。
    返回备份目录路径（没得备份时为空串）。"""
    d = os.path.join(VENDOR_DIR, "data", "user-data")
    if not os.path.isdir(d):
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = "%s.bak-%s" % (d, stamp)
    n = 1
    while os.path.exists(bak):  # 同一秒内重复重新绑定也别撞名
        n += 1
        bak = "%s.bak-%s-%d" % (d, stamp, n)
    try:
        os.rename(d, bak)
    except OSError as e:
        # 改名失败（被占用等）也绝不删：宁可这次登录页带着旧登录态（用户仍可手动
        # 退出登录），也不给用户造成不可恢复的数据损失。
        petlog.log("login state backup failed: %s" % e)
        return ""
    _prune_login_backups(d)
    return bak


def _prune_login_backups(profile_dir, keep=3):
    """只保留最近 keep 份备份（每份约 30MB，避免无限堆积）。

    ponytail: 固定保留 3 份够用；要留更多改 keep，或把备份挪到别的盘。"""
    parent = os.path.dirname(profile_dir)
    prefix = os.path.basename(profile_dir) + ".bak-"
    try:
        baks = sorted(n for n in os.listdir(parent) if n.startswith(prefix))
    except OSError:
        return
    for name in baks[:-keep] if keep > 0 else baks:
        shutil.rmtree(os.path.join(parent, name), ignore_errors=True)


def _our_browsers():
    """本服务 user-data 目录下的无头浏览器主进程（父进程不是同类浏览器的那些）。

    psutil 已经是运行依赖（running_apps.py 在用），这里不再引入新库。"""
    profile = os.path.join(VENDOR_DIR, "data", "user-data").lower()
    try:
        import psutil
    except Exception:
        return []
    procs = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = p.info
            name = (info.get("name") or "").lower()
            if name not in ("msedge.exe", "chrome.exe", "chromium.exe", "brave.exe"):
                continue
            cmd = " ".join(info.get("cmdline") or []).lower()
            if profile not in cmd:
                continue
            procs.append((p.pid, p.ppid(), info.get("create_time") or 0))
        except Exception:
            continue
    pids = {pid for pid, _ppid, _t in procs}
    roots = [(pid, t) for pid, ppid, t in procs if ppid not in pids]
    return roots


def _kill_tree(pid):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           capture_output=True)
        else:
            os.kill(pid, 9)
        return True
    except Exception:
        return False


def sweep_stray_browsers():
    """清理残留的无头浏览器（本服务 profile 的）。返回清掉的主进程数。

    规则：服务没在跑 → 全清（都是上次留下的孤儿）；服务在跑 → 只留最新的一套，
    其余是"页面被关后又 start() 出新的一套、旧的没回收"漏出来的。"""
    roots = _our_browsers()
    if not roots:
        return 0
    if service_alive():
        roots.sort(key=lambda item: item[1], reverse=True)
        victims = [pid for pid, _t in roots[1:]]
    else:
        victims = [pid for pid, _t in roots]
    for pid in victims:
        petlog.log("web2api: killing stray headless browser pid=%s" % pid)
        _kill_tree(pid)
    return len(victims)


def session_status(check=False, timeout_s=3):
    """内置 AI 是否真的进了对话界面（None=服务没给出结果/查不了）。

    check=True 让服务端同步查一次（会开/导航浏览器，几十秒级别），
    check=False 只读它最近一次的自检结果（服务刚起、还没查完时是 None）。"""
    url = BASE_URL + ("/health?check=1" if check else "/health")
    try:
        r = requests.get(url, timeout=timeout_s)
        if r.status_code != 200:
            return None
        value = r.json().get("loggedIn")
        return value if isinstance(value, bool) else None
    except Exception:
        return None


def wait_session_status(timeout_s=60):
    """起服务/重连后确认一次登录态；超时或服务不支持则返回 None（视为未知，按可用处理）。"""
    return session_status(check=True, timeout_s=timeout_s)


def service_alive():
    """端口上是否已有 web2api 服务（200=匹配，401=在跑但 key 不同，都算可用）。"""
    try:
        r = requests.get(BASE_URL + "/v1/models", timeout=2)
        return r.status_code in (200, 401)
    except Exception:
        return False


def _run_node(args, new_console):
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_CONSOLE if new_console else subprocess.CREATE_NO_WINDOW
    # 后台服务的输出落盘（以前直接 DEVNULL）：出问题只有一句"短路了"，
    # 完全看不到是登录失效、网页端空响应还是真超时，没法排查
    out = None
    if not new_console:
        try:
            log_path = os.path.join(VENDOR_DIR, "web2api.log")
            if os.path.exists(log_path) and os.path.getsize(log_path) > 1024 * 1024:
                os.remove(log_path)
            out = open(log_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            out = None
    return subprocess.Popen(
        [_node_exe(), os.path.join(VENDOR_DIR, "src", "index.js")] + args,
        cwd=VENDOR_DIR, creationflags=flags,
        stdout=out if out is not None else (None if new_console else subprocess.DEVNULL),
        stderr=subprocess.STDOUT if out is not None else (None if new_console else subprocess.DEVNULL),
    )


def start_service(timeout_s=30):
    """后台静默启动服务并等待就绪；已有服务在跑则直接返回 True。"""
    global _proc
    if service_alive():
        return True
    _proc = _run_node([], new_console=False)
    for _ in range(timeout_s * 2):
        if service_alive():
            return True
        if _proc.poll() is not None:
            return False
        threading.Event().wait(0.5)
    return service_alive()


def stop_service():
    """停止自己拉起的服务（taskkill 带上 Playwright 浏览器子进程树）。"""
    global _proc
    p, _proc = _proc, None
    if p is not None and p.poll() is None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           capture_output=True)
        else:
            p.terminate()


def run_login_and_start():
    """可见控制台跑 --login（弹全新无登录态浏览器），用户登录完关掉控制台后自动拉起服务。"""
    stop_service()
    bak = clear_login_state()
    if bak:
        petlog.log("login state backed up to %s" % bak)
    login = _run_node(["--login"], new_console=True)
    login.wait()
    return start_service()


def kill_port_listener():
    """重新绑定前清掉 3000 端口上的旧服务（无论是不是自己拉起的）。"""
    stop_service()
    if not service_alive():
        sweep_stray_browsers()   # 服务已停：把它的无头浏览器一起收掉（不然白占几百 MB）
        return
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             creationflags=subprocess.CREATE_NO_WINDOW,
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(":%d" % PORT) and parts[3] == "LISTENING":
                subprocess.run(["taskkill", "/PID", parts[4], "/T", "/F"],
                               creationflags=subprocess.CREATE_NO_WINDOW,
                               capture_output=True)
    except Exception:
        pass
    for _ in range(10):
        if not service_alive():
            break
        threading.Event().wait(0.3)
    sweep_stray_browsers()       # 旧服务杀掉后，浏览器若还在就是孤儿，一并清理


def apply_max_messages(n):
    """同步内置服务的对话消息上限：写 vendor 配置；运行中值不同则后台重启服务生效。"""
    try:
        path = os.path.join(VENDOR_DIR, "config.json")
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        cur = int((d.get("conversation") or {}).get("maxMessages", 20))
        n = int(n)
        if cur == n:
            return
        d.setdefault("conversation", {})["maxMessages"] = n
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        return
    threading.Thread(target=_restart_for_config, daemon=True).start()


def restart_async():
    """后台重启内置服务 = 新一段对话（换人设后用，见 main._new_conversation_if_persona_changed）。

    与断线重连同标准：整段换掉进程，网页端就不会再带着旧人设的那段对话记录。
    ponytail: 整段重启够用（几秒）；要秒级换对话再给 vendor 加个新对话端点。"""
    threading.Thread(target=_restart_for_config,
                     kwargs={"force": True}, daemon=True).start()


def _restart_for_config(force=False):
    """配置变化后重启自己拉起的服务（外部服务不强制重启，下次启动自然生效）。

    force=True（换人设要新对话）连 3000 上那个不是本进程拉起的服务一起换掉：
    否则残留/手动启动的服务里那段旧人设对话换不掉，新人设根本没生效。"""
    if not service_alive():
        return
    if force:
        kill_port_listener()   # service_alive() 已确认 3000 上是 web2api，换掉它=新对话
    else:
        stop_service()
    start_service()


atexit.register(stop_service)


class Manager(QObject):
    """给桌宠用的异步封装：ensure_async / rebind_async，结果经 status 信号回主线程。"""

    status = pyqtSignal(bool, str)  # (ok, message)

    LOGIN_LOST_MSG = "内置 AI 的 DeepSeek 登录态已失效，请在设置页点「重新绑定」重新登录"
    SWEEP_INTERVAL_MS = 15 * 60 * 1000   # 每 15 分钟扫一次残留浏览器

    def __init__(self, parent=None):
        super().__init__(parent)
        # 常驻看门狗：即使没有重启桌宠，也把漏出来的无头浏览器收掉
        timer = QTimer(self)
        timer.timeout.connect(self._sweep_async)
        timer.start(self.SWEEP_INTERVAL_MS)
        self._sweep_timer = timer

    def _sweep_async(self):
        threading.Thread(
            target=lambda: sweep_stray_browsers(), daemon=True).start()

    def ensure_async(self):
        threading.Thread(target=self._ensure, daemon=True).start()

    def _ensure(self):
        # 上一次留下的孤儿浏览器先清掉（一套就是 8 个进程、几百 MB）
        try:
            stray = sweep_stray_browsers()
            if stray:
                petlog.log("web2api: swept %d stray browser(s) at startup" % stray)
        except Exception as e:
            petlog.log("web2api: sweep failed: %s" % e)
        if service_alive():
            # 复用手头这个服务（桌宠重启时常见）：它可能早就掉了登录态，同步查一次
            if wait_session_status() is False:
                self.status.emit(False, self.LOGIN_LOST_MSG)
            else:
                self.status.emit(True, "")
            return
        if not is_bound():
            self.status.emit(False, "login")
            ok = run_login_and_start()
            self.status.emit(*self._started_result(ok))
            return
        ok = start_service()
        self.status.emit(*self._started_result(ok))

    def _started_result(self, ok):
        if not ok:
            return False, "内置 AI 启动失败，请看设置页重新绑定"
        logged_in = wait_session_status()
        if logged_in is False:
            # 服务活着但进不去对话界面（登录态过期）——早点说，别让用户等到聊天失败
            return False, self.LOGIN_LOST_MSG
        return True, "内置 AI 已就绪"

    def rebind_async(self):
        threading.Thread(target=self._rebind, daemon=True).start()

    def _rebind(self):
        kill_port_listener()
        ok = run_login_and_start()
        if not ok:
            self.status.emit(False, "重新绑定失败，请重试或手动启动 vendor 服务")
        elif wait_session_status() is False:
            self.status.emit(False, "重新绑定后仍进不去对话界面，请重试或在弹出的浏览器里完成登录")
        else:
            self.status.emit(True, "重新绑定完成，内置 AI 已就绪（旧登录档案已备份，聊天记录未受影响）")
