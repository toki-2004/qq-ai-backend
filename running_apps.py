# -*- coding: utf-8 -*-
"""枚举 Windows 上"正在用"的应用（前台窗口 + 正在发声），供 AI 判断主人在做什么。

只采"有窗口标题"的顶层窗口：纯后台进程没有窗口，对判断用户当前活动没意义。
发声应用用 pycaw 枚举活跃音频会话（含后台音乐/视频），svchost 等系统音效剔除。
media_track() 再用系统 PowerShell 查 Windows SMTC 媒体会话，拿到"哪个音乐软件
正在播哪首歌"（网易云/QQ音乐/Spotify/浏览器播放时都会注册系统媒体会话）。
"""
import ctypes
import json
import os
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi
_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_DWM_CLOAKED = 14  # DWMWA_CLOAKED：窗口被系统隐藏（如 UWP/Edge 幽灵窗口）
# 系统桌面/任务栏等与用户活动无关的窗口类
_SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow"}
# Windows 系统音效会话的主机进程（并非用户打开的音乐/视频）
_SYSTEM_AUDIO_EXES = {"svchost.exe"}


def _text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value.strip()


def _class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _cloaked(hwnd):
    cloaked = wintypes.DWORD()
    return (dwmapi.DwmGetWindowAttribute(
        hwnd, _DWM_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0
        and bool(cloaked.value))


def _exe_name(hwnd):
    """窗口所属进程的可执行文件名（如 chrome.exe）；取不到时返回空串。"""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value or pid.value == os.getpid():
        return ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(ctypes.sizeof(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _entry(hwnd):
    """可见、未隐藏且有标题的窗口 -> "exe - 标题"；否则返回 None。"""
    if not user32.IsWindowVisible(hwnd) or _cloaked(hwnd):
        return None
    if _class_name(hwnd) in _SKIP_CLASSES:
        return None
    title = _text(hwnd)[:60]
    if not title:
        return None
    exe = _exe_name(hwnd)
    return "%s - %s" % (exe, title) if exe else title


def running_apps(max_n=12):
    """返回当前打开应用的描述列表（前台窗口在最前）；失败/无可枚举时返回 []。"""
    try:
        fg = user32.GetForegroundWindow()
        first = _entry(fg)
        found = [first] if first else []

        def collect(hwnd, _):
            if len(found) >= max_n:
                return False  # 窗口数足够就停（enum 按 Z 序，前面的更接近前台）
            if hwnd == fg:
                return True
            item = _entry(hwnd)
            if item and item not in found:
                found.append(item)
            return True

        user32.EnumWindows(_EnumWindowsProc(collect), 0)
        return found[:max_n]
    except Exception:
        return []


def audio_apps(max_n=8):
    """返回当前正在发声的应用进程名列表（如后台播放中的音乐/视频）；
    未安装 pycaw / 无音频会话 / 全部失败时返回 []。"""
    try:
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    except Exception:
        return []
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception:
        return []
    meters = []  # [(exe名, 峰值表)]：Active 且有真实进程的会话
    for s in sessions:
        try:
            name = s.Process.name() if s.Process is not None else ""
            state = int(s.State)
        except Exception:
            continue
        if not name or name.lower() in _SYSTEM_AUDIO_EXES or state != 1:
            continue
        try:
            meters.append((name, s._ctl.QueryInterface(IAudioMeterInformation)))
        except Exception:
            pass
    audible = set()
    for _ in range(2):  # 采样两轮，避免恰逢歌曲间奏/静音段漏掉
        for name, meter in meters:
            try:
                if meter.GetPeakValue() > 1e-4:
                    audible.add(name.lower())
            except Exception:
                pass
        if audible:
            break
        time.sleep(0.08)
    out, seen = [], set()
    for name, _ in meters:
        low = name.lower()
        if low in audible and low not in seen:
            seen.add(low)
            out.append(name)
    return out[:max_n]


_MEDIA_TTL = 30.0   # SMTC 查询要起一次 PowerShell（约 0.3-1s），结果缓存一段时间
_media_cache = {"at": 0.0, "data": None}

# 只读查询"正在播放（PlaybackStatus=4）"的媒体会话；脚本为 ASCII，无编码坑
_SMTC_PS1 = r'''
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($WinRtTask, $ResultType) {
  $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
  $netTask = $asTask.Invoke($null, @($WinRtTask))
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime] | Out-Null
$mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
$out = @()
foreach ($s in @($mgr.GetSessions())) {
  $pi = $s.GetPlaybackInfo()
  if ([int]$pi.PlaybackStatus -ne 4) { continue }
  try {
    $props = Await ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
  } catch { continue }
  $app = [string]$s.SourceAppUserModelId
  if ($app -match '[\\/]') { $app = Split-Path -Leaf $app }
  $out += [pscustomobject]@{ App = $app; Title = [string]$props.Title; Artist = [string]$props.Artist }
}
Write-Output ("OK sessions=" + @($mgr.GetSessions()).Count)
if ($out.Count -gt 0) { $out | ConvertTo-Json -Compress } else { Write-Output "[]" }
'''


def _parse_smtc(raw):
    """从 PowerShell 输出中取 JSON 行 -> [{"App","Title","Artist"}, ...]；失败返回 []。"""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                data = json.loads(line)
                return data if isinstance(data, list) else [data]
            except Exception:
                continue  # 非 JSON 的普通输出行（如标记行），跳过继续找 JSON
    return []


def media_track():
    """当前正在播放的媒体曲目 {"app","title","artist"}；无播放/查询失败返回 None。

    通过系统媒体会话（SMTC）拿到的是"播放器级"信息，含后台最小化的音乐软件
    （pycaw 只能给 exe 名、给不了歌名）。有 ~0.3-1s 开销，结果缓存 _MEDIA_TTL
    秒；调用方失败时应回退到 audio_apps() 的进程级列表。
    """
    now = time.time()
    if now - _media_cache["at"] < _MEDIA_TTL:
        return _media_cache["data"]
    data = None
    try:
        import base64
        import subprocess

        enc = base64.b64encode(_SMTC_PS1.encode("utf-16-le")).decode("ascii")
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-EncodedCommand", enc],
            capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW)  # 不闪黑窗
        items = _parse_smtc(r.stdout.decode("utf-8", "replace"))
        if items and items[0].get("Title"):
            data = {"app": items[0].get("App", ""),
                    "title": items[0].get("Title", ""),
                    "artist": items[0].get("Artist", "")}
    except Exception:
        data = None
    _media_cache.update(at=now, data=data)
    return data


if __name__ == "__main__":
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    print("窗口：", " | ".join(running_apps()))
    print("发声：", " | ".join(audio_apps()))
    print("播放：", media_track())
