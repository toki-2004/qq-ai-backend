# -*- coding: utf-8 -*-
"""天气监控：IP 定位 + Open-Meteo 当前天气，按天气细分触发自言自语。

本机网络特性（2026-08-30 实测）：
- 系统 DNS 对未缓存域名解析极慢（多网卡 DNS 依次超时，冷解析实测 10-20 秒），
  requests 的 timeout 不覆盖解析阶段，直接请求会无限挂起；
- 可用解析路径是本机隧道 DNS 127.0.0.100:53（国内外域名均能正常解析）。
故本模块内置 UDP DNS 直查该解析器（短超时），失败再回落系统 getaddrinfo；
并沿用 balance.py 的看门狗模式防挂死。
"""
import random
import socket
import struct
import threading
import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

import requests

import petlog

FAST_DNS_SERVER = ("127.0.0.100", 53)
FAST_DNS_TIMEOUT_S = 2.0
IP_API_URL = "http://ip-api.com/json/?fields=status,message,lat,lon,city&lang=zh-CN"
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=%.4f&longitude=%.4f&current=weather_code,wind_speed_10m"
    "&timezone=Asia%%2FShanghai"
)
WEATHER_KINDS = ("sunny", "cloudy", "rainy", "snowy", "foggy", "windy", "stormy")
WEATHER_KIND_ZH = {
    "sunny": "晴", "cloudy": "多云", "rainy": "有雨", "snowy": "有雪",
    "foggy": "有雾", "windy": "起风", "stormy": "雷暴",
}
WINDY_KMH = 38.0  # 风速约 38 km/h（≈6 级风）以上归为"起风"


# ---------- 快速 DNS：直查本机隧道解析器，绕开系统 DNS 慢解析 ----------

def _build_dns_query(host):
    tid = random.randrange(0x10000)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in host.split("."))
    return tid, header + qname + b"\x00" + struct.pack(">HH", 1, 1)


def _skip_name(resp, pos):
    """跳过 DNS 报文里的名字（含压缩指针）。"""
    while pos < len(resp):
        length = resp[pos]
        if length & 0xC0 == 0xC0:  # 压缩指针：0b11 开头
            return pos + 2
        pos += length + 1
        if length == 0:
            return pos
    return pos


def _parse_a_records(resp):
    if len(resp) < 12:
        return []
    _tid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", resp[:12])
    if not (flags & 0x8000) or qd == 0:
        return []
    pos = 12
    for _ in range(qd):  # 跳过问题段
        pos = _skip_name(resp, pos) + 4
    addrs = []
    for _ in range(an):
        pos = _skip_name(resp, pos)
        if pos + 10 > len(resp):
            break
        typ, _cls, _ttl, rdlen = struct.unpack(">HHIH", resp[pos:pos + 10])
        pos += 10
        rdata = resp[pos:pos + rdlen]
        pos += rdlen
        if typ == 1 and len(rdata) == 4:  # A 记录
            addrs.append(".".join(str(b) for b in rdata))
    return addrs


def fast_resolve(host):
    """UDP 直查本机隧道 DNS，返回 A 记录列表；失败返回空列表。"""
    sock = None
    try:
        if not isinstance(host, str) or not host:
            return []
        tid, query = _build_dns_query(host)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(FAST_DNS_TIMEOUT_S)
        sock.sendto(query, FAST_DNS_SERVER)
        resp, _ = sock.recvfrom(4096)
        if len(resp) >= 2 and struct.unpack(">H", resp[:2])[0] == tid:
            return _parse_a_records(resp)
        return []
    except Exception:
        return []
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


_real_getaddrinfo = socket.getaddrinfo


def _fast_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """优先用隧道 DNS 解析；失败直接报错（系统解析慢达 10-20s，会拖住进程退出）。"""
    if isinstance(host, str) and host:
        parts = host.split(".")
        is_ip = len(parts) == 4 and all(p.isdigit() for p in parts)
        if not is_ip:
            ips = fast_resolve(host)
            if ips:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, int(port)))
                        for ip in ips]
            raise socket.gaierror("fast DNS failed: %s" % host)
    return _real_getaddrinfo(host, port, family, type, proto, flags)


class fast_dns:
    """上下文管理器：天气拉取期间把 socket.getaddrinfo 换成快速解析。"""

    def __enter__(self):
        socket.getaddrinfo = _fast_getaddrinfo

    def __exit__(self, *exc):
        socket.getaddrinfo = _real_getaddrinfo


# ---------- 天气分类 ----------

def classify(code, wind_kmh):
    """WMO 天气代码 + 风速 -> 天气类别（见 WEATHER_KINDS）。"""
    code = int(code)
    wind_kmh = float(wind_kmh or 0)
    if code in (95, 96, 99):
        return "stormy"   # 雷暴
    if code in (71, 73, 75, 77, 85, 86):
        return "snowy"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rainy"
    if code in (45, 48):
        return "foggy"
    if wind_kmh >= WINDY_KMH:
        return "windy"
    if code == 0:
        return "sunny"
    return "cloudy"  # 1/2/3：少云/多云/阴


def fetch_weather():
    """IP 定位 + 当前天气；返回 (天气类别, 详情文本)。"""
    with fast_dns():
        resp = requests.get(IP_API_URL, timeout=5)
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError("定位失败: %s" % data.get("message", data))
        lat, lon = float(data["lat"]), float(data["lon"])
        city = data.get("city", "")
        resp2 = requests.get(OPEN_METEO_URL % (lat, lon), timeout=8)
        cur = resp2.json()["current"]
    code = int(cur["weather_code"])
    wind = float(cur.get("wind_speed_10m") or 0)
    kind = classify(code, wind)
    detail = "%s%s，风速 %.1f km/h（%s 查询）" % (
        (city + "，") if city else "", WEATHER_KIND_ZH.get(kind, kind),
        wind, time.strftime("%m-%d %H:%M"))
    return kind, detail


class WeatherMonitor(QObject):
    """当前天气监控：定时拉取并按天气分类发信号（带看门狗防挂死）。"""

    weatherChanged = pyqtSignal(str)  # 仅天气类别变化时发出，供触发情景
    weatherUpdated = pyqtSignal(str, str)  # 每次查询都发：类别 + 可直接放进 prompt 的详情
    weatherError = pyqtSignal(str)

    WATCHDOG_MS = 20000
    REFRESH_MS = 60 * 60 * 1000  # 每小时重新定位并查询一次

    def __init__(self, config, fetch_fn=None, watchdog_ms=None):
        super().__init__()
        self.config = config
        self._fetch_fn = fetch_fn or fetch_weather
        self._fetching = False
        self._timeouts = 0
        self._last_kind = ""
        self._watchdog_ms = watchdog_ms or self.WATCHDOG_MS
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_timeout)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)

    def start(self):
        self.refresh()
        self._timer.start(self.REFRESH_MS)

    def refresh(self):
        if self._fetching:
            return
        self._fetching = True
        self._watchdog.start(self._watchdog_ms)
        threading.Thread(target=self._worker, daemon=True).start()

    def _on_timeout(self):
        if not self._fetching:
            return
        self._fetching = False
        self._timeouts += 1
        petlog.log("weather fetch TIMEOUT (#%d)" % self._timeouts)
        self.weatherError.emit("天气查询超时")
        if self._timeouts >= 3:
            QTimer.singleShot(60000, self.refresh)  # 连续超时后放慢重试
        else:
            self.refresh()

    def _worker(self):
        try:
            kind, detail = self._fetch_fn()
            self._timeouts = 0
            petlog.log("weather fetch ok: %s" % detail)
            self.weatherUpdated.emit(kind, detail)
            if kind != self._last_kind:
                self._last_kind = kind
                self.weatherChanged.emit(kind)
        except Exception as e:
            petlog.log("weather fetch error: %s" % e)
            self.weatherError.emit(str(e))
        finally:
            self._fetching = False
