# -*- coding: utf-8 -*-
"""后端设置：AI 接口 / 人设 / 主动消息 / QQ 接入。"""
import re
import threading

import requests
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

from ai_client import PRESETS
from config import APP_VERSION, DEFAULT_PERSONA


class SettingsDialog(QDialog):
    models_loaded = pyqtSignal(list, bool)

    def __init__(self, config, rebind_callback=None):
        super().__init__()
        self.config = config
        self._rebind_callback = rebind_callback
        self.setWindowTitle("QQ AI 后端设置")
        self.resize(540, 560)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_ai_tab(), "AI")
        self.tabs.addTab(self._build_qq_tab(), "QQ")

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        self.version_label = QLabel("版本 v%s" % APP_VERSION)
        self.version_label.setAlignment(Qt.AlignRight)
        self.version_label.setStyleSheet("color:#777;")
        lay.addWidget(self.version_label)
        lay.addWidget(btns)

    # ---------- AI ----------
    def _build_ai_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.preset_combo = QComboBox()
        for pid, p in PRESETS.items():
            self.preset_combo.addItem(p["name"], pid)
        current = self.config.get("ai_preset", "deepseek_web2api")
        self.preset_combo.setCurrentIndex(
            list(PRESETS.keys()).index(current) if current in PRESETS
            else list(PRESETS.keys()).index("custom"))
        form.addRow("厂商预设", self.preset_combo)

        self.base_edit = QLineEdit(self.config.get("ai_base_url", ""))
        self.base_edit.setPlaceholderText("https://api.example.com/v1")
        form.addRow("API 地址（OpenAI 兼容）", self.base_edit)

        self.key_edit = QLineEdit(self.config.get("ai_api_key", ""))
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("sk-...（本机内置服务用 sk-local）")
        form.addRow("API Key", self.key_edit)

        model_row = QWidget()
        model_lay = QHBoxLayout(model_row)
        model_lay.setContentsMargins(0, 0, 0, 0)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumContentsLength(24)
        self.model_combo.setCurrentText(self.config.get("ai_model", ""))
        self.model_refresh = QPushButton("刷新")
        self.model_refresh.setFixedWidth(56)
        self.model_refresh.clicked.connect(self._fetch_models)
        model_lay.addWidget(self.model_combo, 1)
        model_lay.addWidget(self.model_refresh)
        form.addRow("模型名", model_row)

        self.web2api_msgs = QSpinBox()
        self.web2api_msgs.setRange(0, 500)
        self.web2api_msgs.setValue(int(self.config.get("ai_web2api_max_messages", 20)))
        self.web2api_msgs.setSuffix(" 条/对话")
        self.web2api_msgs.setToolTip("内置 DeepSeek 网页对话的消息上限，达到后自动开新对话（0=不限制）")
        form.addRow("内置对话消息上限", self.web2api_msgs)

        self.persona_edit = QPlainTextEdit()
        self.persona_edit.setPlainText(self.config.get("ai_persona") or DEFAULT_PERSONA)
        self.persona_edit.setFixedHeight(100)
        form.addRow("人设（system prompt）", self.persona_edit)

        self.ctx_spin = QSpinBox()
        self.ctx_spin.setRange(0, 100)
        self.ctx_spin.setValue(int(self.config.get("ai_context_n", 10)))
        self.ctx_spin.setToolTip("开新对话/重启后，导入最近多少条历史，保证续聊")
        form.addRow("续聊携带历史条数", self.ctx_spin)

        self.fallback_check = QCheckBox("AI 失败时发送兜底文本")
        self.fallback_check.setChecked(bool(self.config.get("ai_fallback_enabled", True)))
        form.addRow(self.fallback_check)
        self.fallback_edit = QLineEdit(self.config.get("ai_fallback_text", "唔……我现在有点短路了"))
        form.addRow("兜底文本", self.fallback_edit)

        self.talk_check = QCheckBox("启用主动消息（时段/天气/健康/随机）")
        self.talk_check.setChecked(bool(self.config.get("self_talk_enabled", True)))
        form.addRow(self.talk_check)
        self.talk_interval = QSpinBox()
        self.talk_interval.setRange(0, 1800)
        self.talk_interval.setValue(int(self.config.get("self_talk_interval", 300)))
        self.talk_interval.setSuffix(" 秒")
        self.talk_interval.setToolTip(
            "任意两条主动消息之间的最小间隔；0 = 只保留情境触发，不做随机闲聊。")
        form.addRow("主动消息冷却间隔", self.talk_interval)

        self.preset_combo.currentIndexChanged.connect(self._apply_preset)
        self._apply_preset(keep=True)
        self.models_loaded.connect(self._on_models_loaded)
        self._fetch_models()

        if self._rebind_callback is not None:
            self.rebind_btn = QPushButton("重新绑定（清空旧登录态，打开登录页切换账号）")
            self.rebind_btn.clicked.connect(self._rebind)
            form.addRow(self.rebind_btn)
        return w

    def _rebind(self):
        if self._rebind_callback is None:
            return
        self.rebind_btn.setEnabled(False)
        self.rebind_btn.setText("正在清空旧登录态并打开登录浏览器……登录完成后关闭控制台窗口")
        self._rebind_callback()

    def _apply_preset(self, _=None, keep=False):
        p = PRESETS.get(self.preset_combo.currentData())
        if not p:
            return
        changed = self.config.get("ai_preset") != self.preset_combo.currentData()
        if keep and changed:
            keep = False
        if keep:
            self.base_edit.setText(self.config.get("ai_base_url", p["base_url"]))
            self.model_combo.setCurrentText(self.config.get("ai_model", p["model"]))
        elif self.preset_combo.currentData() != "custom":
            self.base_edit.setText(p["base_url"])
            self.model_combo.setCurrentText(p["model"])
            if p.get("key"):
                self.key_edit.setText(p["key"])
        if changed or not keep:
            self._fetch_models()

    def _fetch_models(self):
        base = self.base_edit.text().strip().rstrip("/")
        if not base:
            return
        key = self.key_edit.text().strip()
        self.model_refresh.setEnabled(False)
        self.model_refresh.setText("获取中…")
        threading.Thread(target=self._fetch_models_worker, args=(base, key), daemon=True).start()

    def _fetch_models_worker(self, base, key):
        try:
            r = requests.get(base + "/models",
                             headers={"Authorization": "Bearer " + key} if key else {},
                             timeout=8)
            if r.status_code == 200:
                ids = [str(m["id"]) for m in (r.json().get("data") or []) if m.get("id")]
                if ids:
                    self._emit_models(ids, True)
                    return
        except Exception:
            pass
        self._emit_models([], False)

    def _emit_models(self, ids, ok):
        try:
            self.models_loaded.emit(ids, ok)
        except RuntimeError:
            pass

    def _on_models_loaded(self, ids, ok):
        self.model_refresh.setEnabled(True)
        self.model_refresh.setText("刷新")
        current = self.model_combo.currentText()
        if ok:
            self.model_combo.clear()
            self.model_combo.addItems(ids)
            if current:
                self.model_combo.setCurrentText(current)
            elif ids:
                self.model_combo.setCurrentIndex(0)
        else:
            self.model_combo.setToolTip("获取模型列表失败，可手动输入模型名")

    # ---------- QQ ----------
    def _build_qq_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.qq_check = QCheckBox("启用 QQ（OneBot 11，私聊 + 群）")
        self.qq_check.setChecked(bool(self.config.get("qq_bridge_enabled", True)))
        form.addRow(self.qq_check)

        self.qq_url = QLineEdit(self.config.get("qq_bridge_ws_url", "ws://127.0.0.1:3001"))
        self.qq_url.setPlaceholderText("ws://127.0.0.1:3001")
        form.addRow("OneBot WS 地址（NapCat / SnowLuma）", self.qq_url)

        self.qq_token = QLineEdit(self.config.get("qq_bridge_token", ""))
        self.qq_token.setEchoMode(QLineEdit.Password)
        self.qq_token.setPlaceholderText("与 NapCat / SnowLuma 的 token 完全一致；没设就留空")
        form.addRow("Token", self.qq_token)

        self.qq_allowed = QPlainTextEdit("\n".join(
            str(x) for x in (self.config.get("qq_bridge_allowed_ids") or [])))
        self.qq_allowed.setPlaceholderText("每行一个 QQ 号")
        self.qq_allowed.setFixedHeight(64)
        form.addRow("白名单 QQ", self.qq_allowed)

        self.qq_groups = QPlainTextEdit("\n".join(
            str(x) for x in (self.config.get("qq_bridge_allowed_groups") or [])))
        self.qq_groups.setPlaceholderText("每行一个群号；留空不处理群消息")
        self.qq_groups.setFixedHeight(64)
        form.addRow("白名单群", self.qq_groups)

        hint = QLabel(
            "留空白名单时，第一个私聊机器人 QQ 的用户会自动绑定。\n"
            "每个 QQ、每个群的历史分开保存；群里只在被 @ 时回复，其他消息只记录。\n"
            "内置网页 AI 的多会话仍共用一个网页对话；要彻底隔离请用 OpenAI 兼容接口。\n"
            "NapCat / SnowLuma 的 WS 服务建议只监听 127.0.0.1 并设置 token，\n"
            "不要把无 token 的端口暴露到局域网。\n"
            "个人 QQ 自动化不是官方能力，存在风控/封号风险，务必使用小号。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        form.addRow(hint)
        return w

    # ---------- 保存 ----------
    def _save(self):
        self.config.set("ai_preset", self.preset_combo.currentData())
        self.config.set("ai_base_url", self.base_edit.text().strip())
        self.config.set("ai_api_key", self.key_edit.text().strip())
        self.config.set("ai_model", self.model_combo.currentText().strip())
        self.config.set("ai_persona", self.persona_edit.toPlainText().strip() or DEFAULT_PERSONA)
        self.config.set("ai_context_n", self.ctx_spin.value())
        self.config.set("ai_fallback_enabled", self.fallback_check.isChecked())
        self.config.set("ai_fallback_text",
                        self.fallback_edit.text().strip() or "唔……我现在有点短路了")
        self.config.set("ai_web2api_max_messages", self.web2api_msgs.value())
        self.config.set("self_talk_enabled", self.talk_check.isChecked())
        self.config.set("self_talk_interval", self.talk_interval.value())
        self.config.set("qq_bridge_enabled", self.qq_check.isChecked())
        self.config.set("qq_bridge_ws_url", self.qq_url.text().strip())
        self.config.set("qq_bridge_token", self.qq_token.text().strip())
        ids = [x for x in re.split(r"[\s,，;；]+", self.qq_allowed.toPlainText().strip()) if x]
        self.config.set("qq_bridge_allowed_ids", ids)
        groups = [x for x in re.split(r"[\s,，;；]+", self.qq_groups.toPlainText().strip()) if x]
        self.config.set("qq_bridge_allowed_groups", groups)
        self.accept()
