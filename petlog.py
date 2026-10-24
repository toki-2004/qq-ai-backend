# -*- coding: utf-8 -*-
"""极简文件日志：把关键事件与全局异常落到 pet_debug.log，便于排查无控制台场景。"""
import os
import sys
import time
import threading

# 冻结（onedir/onefile）时日志写到 exe 同目录，与 config.json 一致，便于排查；
# 源码运行时写到项目目录。
if getattr(sys, "frozen", False):
    _DIR = os.path.dirname(sys.executable)
else:
    _DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(_DIR, "pet_debug.log")
_MAX_BYTES = 512 * 1024
_lock = threading.Lock()


def log(msg):
    try:
        with _lock:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > _MAX_BYTES:
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.read()[-64 * 1024:]
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    f.write(tail)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(time.strftime("[%H:%M:%S] ") + str(msg) + "\n")
    except Exception:
        pass


def install_excepthooks():
    """全局异常钩子：GUI 槽函数与子线程的异常默认只会打到看不见的控制台。"""

    def _fmt(t, v, tb):
        import traceback
        return "".join(traceback.format_exception(t, v, tb))

    def _sys_hook(t, v, tb):
        log("EXCEPTION:\n" + _fmt(t, v, tb))
        sys.__excepthook__(t, v, tb)

    def _thread_hook(args):
        log("THREAD EXCEPTION (%s):\n" % (args.thread.name if args.thread else "?")
            + _fmt(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
