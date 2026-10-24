# -*- coding: utf-8 -*-
"""配置读写：默认配置 + 本地 JSON（config.json 含 API Key 与 QQ token，勿提交）。"""
import json
import os
import shutil

APP_VERSION = "0.1.0"

DEFAULT_PERSONA = (
    "你是一个有自己生活、性格和情绪的女生。"
    "你知道这台电脑上的时间、正在用的应用和天气，也通过 QQ 和对方聊天。"
    "你们是平等的，没有主仆或从属关系；怎么称呼对方、保持怎样的距离，"
    "由你自己的感受决定。聊天时像真人一样自然，会接话，也会有自己的判断和小情绪。"
)

DEFAULT_CONFIG = {
    # AI（OpenAI 兼容）
    "ai_preset": "deepseek_web2api",
    "ai_base_url": "http://127.0.0.1:3000/v1",
    "ai_api_key": "sk-local",  # 内置服务的本地 key（与 vendor config.json 一致）
    "ai_model": "deepseek-chat",
    "ai_persona": DEFAULT_PERSONA,
    "ai_context_n": 10,
    "ai_timeout_s": 330,
    "ai_fallback_enabled": True,
    "ai_fallback_text": "唔……我现在有点短路了",
    "ai_web2api_max_messages": 20,
    "chat_history_max": 200,
    # 主动消息（原桌宠自言自语，改为 QQ 发送）
    "self_talk_enabled": True,
    "self_talk_interval": 300,
    # 余额（只进 prompt，不显示）
    "accounts": {},
    "balance_offset": [],
    "poll_interval_sec": 3,
    # QQ / OneBot 11
    "qq_bridge_enabled": True,
    "qq_bridge_ws_url": "ws://127.0.0.1:3001",
    "qq_bridge_token": "",
    "qq_bridge_allowed_ids": [],
    # 睡眠状态（说“午安/晚安”后存这里，重启接着睡）
    "sleep_kind": "",
    "sleep_until": 0,
}


class Config:
    def __init__(self, path):
        self.path = path
        self.data = dict(DEFAULT_CONFIG)
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for key in DEFAULT_CONFIG:
                if key in loaded:
                    self.data[key] = loaded[key]
        except Exception as e:
            # 配置损坏（含 API Key 与全部个性化设置）绝不能静默丢失
            print("[config] 配置文件读取失败，已使用默认配置: %s" % e)
            try:
                os.replace(self.path, self.path + ".broken")
            except Exception:
                pass

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            if os.path.exists(self.path):
                try:
                    shutil.copy2(self.path, self.path + ".bak")
                except Exception:
                    pass
            os.replace(tmp, self.path)  # 原子替换，杜绝“截断后写一半”的坏档
        except Exception as e:
            print("[config] 保存失败: %s" % e)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()