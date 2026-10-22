from __future__ import annotations

import copy
import html
import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Any

import websocket
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizeGrip,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "overlay": {
        "enabled": True,
        "x": 24,
        "y": 180,
        "width": 326,
        "height": 420,
        "opacity": 0.84,
        "font_size": 13,
        "hotkey": "F8",
        "click_through": False,
    },
    "websocket": {"host": "127.0.0.1", "port": 8765},
    "score": {
        "fkdr_weight": 1.0,
        "wlr_weight": 1.0,
        "kdr_weight": 0.5,
        "fkdr_cap": 4.0,
        "wlr_cap": 4.0,
        "kdr_cap": 4.0,
    },
    "categories": {"high": 4.0, "good": 2.5, "decent": 1.5},
}

CATEGORY_COLORS = {
    "high": "#fb6471",
    "good": "#ffad61",
    "decent": "#f4d35e",
    "low": "#63d5a3",
    "unknown": "#9ba7b8",
}

URL_PATTERN = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+", re.IGNORECASE
)


class CopyableLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and not self.hasSelectedText():
            self.clicked.emit()


def load_config() -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    if isinstance(value, dict) and isinstance(config.get(key), dict):
                        config[key].update(value)
        except (OSError, json.JSONDecodeError) as error:
            print(f"[PikaStats Overlay] Could not read config.json: {error}")
    else:
        save_config(config)
    return config


def save_config(config: dict[str, Any]) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"[PikaStats Overlay] Could not save config.json: {error}")


def finite_number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0.0 else default
    except (TypeError, ValueError):
        return default


def score_player(player: dict[str, Any], config: dict[str, Any]) -> float | None:
    if player.get("status") not in {"ready", "refreshing", "retrying", "nicked"}:
        return None
    if any(player.get(key) is None for key in ("fkdr", "wlr", "kdr")):
        return None
    weights = config["score"]
    fkdr = min(finite_number(player.get("fkdr")), finite_number(weights.get("fkdr_cap"), 4.0))
    wlr = min(finite_number(player.get("wlr")), finite_number(weights.get("wlr_cap"), 4.0))
    kdr = min(finite_number(player.get("kdr")), finite_number(weights.get("kdr_cap"), 4.0))
    return (
        fkdr * finite_number(weights.get("fkdr_weight"), 1.0)
        + wlr * finite_number(weights.get("wlr_weight"), 1.0)
        + kdr * finite_number(weights.get("kdr_weight"), 0.5)
    )


def categorize(score: float | None, config: dict[str, Any]) -> str:
    if score is None:
        return "unknown"
    thresholds = config["categories"]
    if score >= finite_number(thresholds.get("high"), 4.0):
        return "high"
    if score >= finite_number(thresholds.get("good"), 2.5):
        return "good"
    if score >= finite_number(thresholds.get("decent"), 1.5):
        return "decent"
    return "low"


def prepare_players(players: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    prepared = []
    for raw in players:
        if not isinstance(raw, dict):
            continue
        player = dict(raw)
        player["name"] = str(player.get("name", "Unknown"))[:32]
        player["score"] = score_player(player, config)
        player["category"] = categorize(player["score"], config)
        prepared.append(player)
    prepared.sort(
        key=lambda player: (
            player.get("score") is None,
            -(player["score"] or 0.0),
            player["name"].casefold(),
        )
    )
    return prepared


class SocketClient(QObject):
    """Reconnectable WebSocket reader. Network work stays off the Qt/UI thread."""

    message_received = Signal(dict)
    connection_changed = Signal(bool, str)

    def __init__(self, uri: str):
        super().__init__()
        self.uri = uri
        self._stop = threading.Event()
        self._socket: websocket.WebSocket | None = None
        self._thread = threading.Thread(target=self._run, name="PikaStats-WebSocket", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass
        self._thread.join(timeout=2.0)

    def send_json(self, message: dict[str, Any]) -> bool:
        connection = self._socket
        if connection is None:
            return False
        try:
            connection.send(json.dumps(message, separators=(",", ":")))
            return True
        except Exception:
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            connection = None
            try:
                connection = websocket.create_connection(self.uri, timeout=2.5, enable_multithread=True)
                self._socket = connection
                connection.settimeout(1.0)
                self.connection_changed.emit(True, "Connected")
                while not self._stop.is_set():
                    try:
                        payload = connection.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if payload is None:
                        break
                    if isinstance(payload, bytes):
                        payload = payload.decode("utf-8", errors="replace")
                    try:
                        message = json.loads(payload)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if isinstance(message, dict):
                        self.message_received.emit(message)
            except Exception as error:
                if not self._stop.is_set():
                    self.connection_changed.emit(False, "Waiting for ViaProxy")
            finally:
                self._socket = None
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            self._stop.wait(2.0)
        self.connection_changed.emit(False, "Disconnected")


class GlobalHotkey(threading.Thread):
    """Polls a system-wide hotkey without registering a keyboard hook or taking focus."""

    VK = {
        **{f"F{number}": 0x6F + number for number in range(1, 25)},
        "SPACE": 0x20,
        "INSERT": 0x2D,
        "HOME": 0x24,
        "END": 0x23,
        "PAUSE": 0x13,
        "SCROLLLOCK": 0x91,
        "TAB": 0x09,
        "GRAVE": 0xC0,
    }
    MODIFIERS = {"CTRL": 0x11, "SHIFT": 0x10, "ALT": 0x12}

    def __init__(self, hotkey: str, callback):
        super().__init__(name="PikaStats-Hotkey", daemon=True)
        parts = [part.strip().upper() for part in hotkey.split("+") if part.strip()]
        if parts and parts[-1] in self.VK:
            self.key = self.VK[parts[-1]]
        elif parts and len(parts[-1]) == 1 and parts[-1].isalnum():
            self.key = ord(parts[-1])
        else:
            self.key = 0x77
        self.modifiers = {self.MODIFIERS[part] for part in parts[:-1] if part in self.MODIFIERS}
        self.callback = callback
        self.stop_event = threading.Event()

    @staticmethod
    def _is_down(user32, key: int) -> bool:
        return bool(user32.GetAsyncKeyState(key) & 0x8000)

    def run(self) -> None:
        if os.name != "nt":
            return
        import ctypes

        user32 = ctypes.windll.user32
        was_down = False
        while not self.stop_event.is_set():
            modifiers_down = all(self._is_down(user32, key) for key in self.modifiers)
            pressed = modifiers_down and self._is_down(user32, self.key)
            if pressed and not was_down:
                self.callback()
            was_down = pressed
            self.stop_event.wait(0.025)

    def stop(self) -> None:
        self.stop_event.set()


class DragHeader(QFrame):
    def __init__(self, parent: "OverlayWindow"):
        super().__init__(parent)
        self.window = parent
        self._drag_offset = None
        self.setObjectName("header")
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class ClickableFrame(QFrame):
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
        else:
            super().mousePressEvent(event)


class OverlayWindow(QWidget):
    toggle_requested = Signal()

    def __init__(self, config: dict[str, Any], socket_client: SocketClient):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        if config["overlay"].get("click_through", False):
            flags |= Qt.WindowType.WindowTransparentForInput
        super().__init__(None, flags)
        self.setObjectName("overlayWindow")
        self.config = config
        self.socket_client = socket_client
        self.state: dict[str, Any] = {
            "type": "game_update", "active": False, "mode": "ALL_MODES", "period": "total", "players": []
        }
        self._selected_settings = {"mode": "ALL_MODES", "period": "total"}
        self._pending_settings: dict[str, str] = dict(self._selected_settings)
        self._settings_retry_used = False
        self._last_message: dict[str, Any] | None = None
        self.selected_player_name: str | None = None
        self._detail_request_counter = 0
        self._active_detail_request = ""
        self._detail_data: dict[str, Any] | None = None
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.timeout.connect(self._on_detail_timeout)
        self._shown_once = False
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMinimumSize(270, 250)
        self.resize(
            max(270, int(config["overlay"].get("width", 326))),
            max(250, int(config["overlay"].get("height", 420))),
        )
        self.move(int(config["overlay"].get("x", 24)), int(config["overlay"].get("y", 180)))
        self._build_ui()
        self.toggle_requested.connect(self.toggle_visible)
        socket_client.message_received.connect(self._on_message)
        socket_client.connection_changed.connect(self._on_connection_changed)
        self._apply_opacity()
        self._render()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 8)
        root.setSpacing(8)

        self.header = DragHeader(self)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(4, 1, 4, 1)
        header_layout.setSpacing(7)
        self.brand_mark = QLabel("✦")
        self.brand_mark.setObjectName("brandMark")
        self.brand = QLabel("BEDWARS")
        self.brand.setObjectName("brand")
        self.mode_label = QLabel("Waiting")
        self.mode_label.setObjectName("mode")
        self.connection_label = QLabel("●")
        self.connection_label.setObjectName("connection")
        for label in (self.brand_mark, self.brand, self.mode_label, self.connection_label):
            self._enable_copying(label)
        self.connection_label.setToolTip("Waiting for ViaProxy")
        self.close_button = QToolButton()
        self.close_button.setText("×")
        self.close_button.setObjectName("closeButton")
        self.close_button.setToolTip("Exit overlay")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(QApplication.instance().quit)
        header_layout.addWidget(self.brand_mark)
        header_layout.addWidget(self.brand)
        header_layout.addStretch(1)
        header_layout.addWidget(self.mode_label)
        header_layout.addWidget(self.connection_label)
        header_layout.addWidget(self.close_button)
        root.addWidget(self.header)

        selector_row = QHBoxLayout()
        selector_row.setContentsMargins(2, 0, 2, 0)
        selector_row.setSpacing(7)
        self.mode_button = QToolButton()
        self.mode_button.setObjectName("selector")
        self.mode_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mode_button.setToolTip("Click to switch BedWars mode")
        self.mode_button.clicked.connect(self._cycle_mode)
        self.period_button = QToolButton()
        self.period_button.setObjectName("selector")
        self.period_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.period_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.period_button.setToolTip("Click to switch stats time period")
        self.period_button.clicked.connect(self._cycle_period)
        self.reload_button = QToolButton()
        self.reload_button.setObjectName("reloadButton")
        self.reload_button.setText("↻")
        self.reload_button.setAccessibleName("Reload player list and all player stats")
        self.reload_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reload_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.reload_button.setToolTip("Refresh the player list and reload stats for everyone")
        self.reload_button.clicked.connect(self._reload_players)
        self.reload_button.setFixedWidth(38)
        selector_row.addWidget(self.mode_button, 1)
        selector_row.addWidget(self.period_button, 1)
        selector_row.addWidget(self.reload_button)
        root.addLayout(selector_row)

        heading_row = QHBoxLayout()
        heading_row.setContentsMargins(2, 0, 2, 0)
        self.subheading = QLabel("Players in your game")
        self.subheading.setObjectName("subheading")
        self._enable_copying(self.subheading)
        heading_row.addWidget(self.subheading, 1)
        self.back_button = QToolButton()
        self.back_button.setObjectName("selector")
        self.back_button.setText("‹ PLAYERS")
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.back_button.clicked.connect(self._close_player_details)
        self.back_button.hide()
        heading_row.addWidget(self.back_button)
        root.addLayout(heading_row)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rows_widget = QWidget()
        self.rows_widget.setObjectName("rowsWidget")
        self.rows_layout = QVBoxLayout(self.rows_widget)
        self.rows_layout.setContentsMargins(0, 0, 3, 0)
        self.rows_layout.setSpacing(6)
        self.scroll.setWidget(self.rows_widget)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.scroll.viewport().setStyleSheet("background: transparent;")
        root.addWidget(self.scroll, 1)

        footer = QHBoxLayout()
        hotkey_label = str(self.config["overlay"].get("hotkey", "F8")).upper().replace("+", " + ")
        self.footer_label = QLabel(f"{hotkey_label}  HIDE  ·  LOCAL STATS")
        self.footer_label.setObjectName("footer")
        self._enable_copying(self.footer_label)
        footer.addWidget(self.footer_label)
        footer.addStretch(1)
        grip = QSizeGrip(self)
        grip.setFixedSize(14, 14)
        grip.setStyleSheet("QSizeGrip { background: transparent; }")
        footer.addWidget(grip)
        root.addLayout(footer)

    def _apply_opacity(self) -> None:
        overlay = self.config["overlay"]
        opacity = min(1.0, max(0.25, finite_number(overlay.get("opacity"), 0.84)))
        alpha = round(opacity * 255)
        self._background_alpha = alpha
        font_size = max(9, min(24, int(overlay.get("font_size", 13))))
        self.setStyleSheet(
            f"""
            QWidget {{ color: #edf2fa; font-family: "Segoe UI", Arial, sans-serif; font-size: {font_size}px; }}
            #overlayWindow {{ background-color: rgba(20, 29, 42, {alpha});
                border: 1px solid rgba(198, 219, 245, 74); border-radius: 17px; }}
            #rowsWidget {{ background: transparent; }}
            #header {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba(255, 255, 255, 12), stop:1 rgba(255, 255, 255, 0));
                border-top-left-radius: 15px; border-top-right-radius: 15px; min-height: 26px; }}
            #brandMark {{ color: #8de6c2; font-size: 18px; font-weight: 700; }}
            #brand {{ color: #f4f7fb; font-size: {font_size + 2}px; font-weight: 800; letter-spacing: 1px; }}
            #mode {{ color: #8de6c2; background: rgba(141, 230, 194, 28); border-radius: 8px;
                     padding: 4px 8px; font-size: {max(9, font_size - 2)}px; font-weight: 700; }}
            #connection {{ color: #efba61; padding: 0 2px; font-size: 11px; }}
            #closeButton {{ color: #8794a7; font-size: 19px; padding: 0 3px; }}
            #closeButton:hover {{ color: #fb6471; }}
            QToolButton#selector, QToolButton#reloadButton {{ color: #dce7f6; text-align: left; padding: 7px 9px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(139, 166, 199, 37), stop:1 rgba(85, 111, 145, 24));
                border: 1px solid rgba(192, 215, 244, 50); border-radius: 10px;
                font-size: {max(9, font_size - 2)}px; font-weight: 700; }}
            QToolButton#reloadButton {{ text-align: center; padding: 5px; font-size: {font_size + 3}px; }}
            QToolButton#selector:hover, QToolButton#reloadButton:hover {{ background-color: rgba(141, 230, 194, 38);
                border-color: rgba(141, 230, 194, 100); }}
            QToolButton#selector:pressed, QToolButton#reloadButton:pressed {{ background-color: rgba(141, 230, 194, 58); }}
            #subheading {{ color: #8995a7; padding-left: 4px; font-size: {max(9, font_size - 2)}px; }}
            #footer {{ color: #6f7c90; font-size: {max(8, font_size - 4)}px; font-weight: 700; letter-spacing: 0.5px; }}
            #empty {{ color: #99a5b6; background: rgba(255, 255, 255, 9); border-radius: 10px;
                      padding: 22px 12px; }}
            #playerCard {{ background: rgba(255, 255, 255, 12); border: 1px solid rgba(255, 255, 255, 15);
                           border-radius: 10px; }}
            #playerCard:hover {{ background: rgba(141, 230, 194, 22); border-color: rgba(141, 230, 194, 95); }}
            #playerName {{ color: #f1f4f8; font-size: {font_size}px; font-weight: 700; }}
            #playerMeta {{ color: #9aa6b7; font-size: {max(9, font_size - 2)}px; }}
            #detailTitle {{ color: #f1f4f8; font-size: {font_size + 3}px; font-weight: 800; }}
            #detailSection {{ color: #8de6c2; font-size: {max(9, font_size - 1)}px; font-weight: 800;
                              padding-top: 7px; }}
            #detailMetric {{ color: #aab7c9; font-size: {max(9, font_size - 2)}px; }}
            #detailValue {{ color: #f1f4f8; font-size: {font_size}px; font-weight: 700; }}
            #punishmentEntry {{ color: #d6dfec; font-size: {max(9, font_size - 1)}px; }}
            #fkdrValue {{ color: #ffffff; font-size: {font_size + 4}px; font-weight: 800; }}
            #fkdrCaption {{ color: #8491a4; font-size: {max(8, font_size - 4)}px; font-weight: 700; }}
            #category {{ font-size: {max(9, font_size - 2)}px; font-weight: 800; }}
            QScrollBar:vertical {{ width: 5px; background: transparent; margin: 1px; }}
            QScrollBar::handle:vertical {{ background: rgba(190, 205, 225, 80); border-radius: 2px; min-height: 18px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            """
        )

    def _on_message(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "player_details":
            if (str(message.get("requestId", "")) != self._active_detail_request
                    or self.selected_player_name is None):
                return
            self._detail_timer.stop()
            self._detail_data = copy.deepcopy(message)
            self._render()
        elif message_type in ("game_update", "player_update"):
            if message == self._last_message:
                return
            self._last_message = copy.deepcopy(message)
            server_mode = str(message.get("mode", self.state.get("mode", "QUADS")))
            server_period = str(message.get("period", self.state.get("period", "total")))
            if self._pending_settings.get("mode") == server_mode:
                self._pending_settings.pop("mode", None)
            if self._pending_settings.get("period") == server_period:
                self._pending_settings.pop("period", None)
            if not self._pending_settings:
                self._settings_retry_used = False

            expected_mode = self._selected_settings.get("mode", server_mode)
            expected_period = self._selected_settings.get("period", server_period)
            if (server_mode.upper() != expected_mode.upper()
                    or server_period.casefold() != expected_period.casefold()):
                # Old responses can arrive after a filter change. Keep the selected
                # filters and the current rows until matching stats come back.
                self.state = {
                    "active": bool(message.get("active", self.state.get("active", False))),
                    "mode": expected_mode,
                    "period": expected_period,
                    "players": self.state.get("players", []),
                }
                self._render()
                if self._pending_settings and not self._settings_retry_used:
                    self._settings_retry_used = True
                    self._send_settings_update(self._pending_settings)
                return

            self.state = {
                "active": bool(message.get("active", self.state.get("active", False))),
                "mode": expected_mode,
                "period": expected_period,
                "players": message.get("players", []),
            }
            self._render()
            if self._pending_settings and not self._settings_retry_used:
                self._settings_retry_used = True
                self._send_settings_update(self._pending_settings)
        elif message_type == "player_removed":
            removed_name = str(message.get("name", "")).casefold()
            self.state["players"] = [
                player for player in self.state.get("players", [])
                if str(player.get("name", "")).casefold() != removed_name
            ]
            self._render()

    def _send_settings_update(self, settings: dict[str, str]) -> None:
        message: dict[str, Any] = {"type": "settings_update", **settings}
        if self.state.get("active"):
            message["active"] = True
            message["players"] = [
                str(player.get("name", ""))
                for player in self.state.get("players", [])
                if isinstance(player, dict) and player.get("name")
            ]
        self.socket_client.send_json(message)

    def _on_connection_changed(self, connected: bool, text: str) -> None:
        self.connection_label.setStyleSheet(f"color: {'#71dfad' if connected else '#efba61'};")
        self.connection_label.setToolTip(text)
        if (not connected and self.selected_player_name is not None
                and self._detail_data is None):
            self._detail_timer.stop()
            self._detail_data = {
                "statsError": "ViaProxy connection lost",
                "punishmentsError": "ViaProxy connection lost",
            }
            self._render()
        if connected and self._selected_settings:
            self._pending_settings.update(self._selected_settings)
            self._settings_retry_used = False
            self._send_settings_update(self._selected_settings)

    def _render(self) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        mode = self.state.get("mode", "QUADS")
        mode_names = {
            "SOLO": "SOLO",
            "DOUBLES": "DOUBLES",
            "TRIPLES": "TRIPLES",
            "QUADS": "QUADS",
            "ALL_MODES": "ALL MODES",
        }
        self.mode_label.setText(mode_names.get(mode, str(mode).replace("_", " ")))
        self.mode_button.setText(f"MODE   {mode_names.get(mode, str(mode).replace('_', ' '))}   ›")
        periods = {
            "weekly": "WEEKLY",
            "monthly": "MONTHLY",
            "yearly": "YEARLY",
            "total": "ALL TIME",
            "a": "ALL TIME",
            "w": "WEEKLY",
            "m": "MONTHLY",
            "y": "YEARLY",
        }
        period = str(self.state.get("period", "total")).casefold()
        self.period_button.setText(f"TIME   {periods.get(period, period.upper())}   ›")
        details_open = self.selected_player_name is not None
        for widget in (self.mode_button, self.period_button, self.reload_button):
            widget.setVisible(not details_open)
        self.back_button.setVisible(details_open)
        if details_open:
            self.subheading.setText(f"Player details  ·  {self.selected_player_name}")
            self.rows_layout.addWidget(self._player_detail_view(self.selected_player_name or ""))
            self.rows_layout.addStretch(1)
            self.reload_button.setEnabled(False)
            return

        players = prepare_players(self.state.get("players", []), self.config)
        active = bool(self.state.get("active", False))
        self.reload_button.setEnabled(active and bool(players))
        self.subheading.setText(f"Players in your game  ·  {len(players)}")

        if not active:
            self.rows_layout.addWidget(self._empty_card("Waiting for a BedWars game", "Join or queue while ViaProxy is connected"))
        elif not players:
            self.rows_layout.addWidget(self._empty_card("Game detected", "Waiting for the player list"))
        else:
            for player in players:
                self.rows_layout.addWidget(self._player_card(player))
        self.rows_layout.addStretch(1)

    def _cycle_mode(self) -> None:
        modes = ("QUADS", "SOLO", "DOUBLES", "TRIPLES", "ALL_MODES")
        current = str(self.state.get("mode", "ALL_MODES")).upper()
        selected = modes[(modes.index(current) + 1) % len(modes)] if current in modes else modes[0]
        self._select_setting("mode", selected)

    def _cycle_period(self) -> None:
        periods = ("weekly", "monthly", "yearly", "total")
        current = str(self.state.get("period", "total")).casefold()
        selected = periods[(periods.index(current) + 1) % len(periods)] if current in periods else periods[0]
        self._select_setting("period", selected)

    def _reload_players(self) -> None:
        if not self.state.get("active"):
            return
        players = [
            player for player in self.state.get("players", [])
            if isinstance(player, dict) and player.get("name")
        ]
        if not players or not self.socket_client.send_json({"type": "reload_players"}):
            return
        for player in players:
            has_previous_stats = all(player.get(metric) is not None for metric in ("fkdr", "wlr", "kdr"))
            player["status"] = "refreshing" if has_previous_stats else "loading"
            player["retryCount"] = 0
        self._render()

    def _select_setting(self, key: str, selected: str) -> None:
        self.state[key] = selected
        self._selected_settings[key] = selected
        self._pending_settings[key] = selected
        self._settings_retry_used = False
        for player in self.state.get("players", []):
            if isinstance(player, dict):
                has_previous_stats = all(player.get(metric) is not None for metric in ("fkdr", "wlr", "kdr"))
                player["status"] = "refreshing" if has_previous_stats else "loading"
        self._render()
        self._send_settings_update({key: selected})

    def _selected_scope(self) -> str:
        return self._scope_label(self.state.get("mode"), self.state.get("period"))

    def _previous_scope(self, player: dict[str, Any]) -> str | None:
        mode = player.get("loadedMode")
        period = player.get("loadedPeriod")
        if mode is None or period is None:
            return None
        return self._scope_label(mode, period)

    @staticmethod
    def _scope_label(mode: Any, period: Any) -> str:
        modes = {
            "SOLO": "SOLO",
            "DOUBLES": "DOUBLES",
            "TRIPLES": "TRIPLES",
            "QUADS": "QUADS",
            "QUAD": "QUADS",
            "ALL_MODES": "ALL MODES",
            "ALL MODES": "ALL MODES",
        }
        periods = {
            "weekly": "WEEKLY",
            "monthly": "MONTHLY",
            "yearly": "YEARLY",
            "total": "ALL TIME",
            "a": "ALL TIME",
            "w": "WEEKLY",
            "m": "MONTHLY",
            "y": "YEARLY",
        }
        mode_text = str(mode or "").upper()
        period_text = str(period or "").casefold()
        mode_label = modes.get(mode_text, mode_text.replace("_", " "))
        period_label = periods.get(period_text, period_text.upper())
        return f"{period_label} {mode_label}".strip()

    def _empty_card(self, title: str, subtitle: str) -> QWidget:
        card = QFrame()
        card.setObjectName("empty")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 14, 12, 14)
        title_label = QLabel(title)
        title_label.setStyleSheet("font-weight: 700; color: #dfe7f1;")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setWordWrap(True)
        subtitle_label.setStyleSheet("color: #8794a7; font-size: 11px;")
        self._enable_copying(title_label)
        self._enable_copying(subtitle_label)
        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        return card

    def _player_card(self, player: dict[str, Any]) -> QWidget:
        card = ClickableFrame()
        card.setObjectName("playerCard")
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setToolTip("Click to view detailed stats and punishments")
        card.clicked.connect(lambda name=player["name"]: self._open_player_details(name))
        row = QHBoxLayout(card)
        row.setContentsMargins(10, 8, 11, 8)
        row.setSpacing(9)

        category = player.get("category", "unknown")
        color = CATEGORY_COLORS.get(category, CATEGORY_COLORS["unknown"])
        dot = QLabel("●")
        dot.setObjectName("category")
        dot.setStyleSheet(f"color: {color};")
        dot.setFixedWidth(13)
        dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        row.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)

        details = QVBoxLayout()
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(2)
        name = CopyableLabel(player["name"])
        name.setObjectName("playerName")
        self._enable_copying(name)
        name.clicked.connect(card.clicked.emit)
        details.addWidget(name)

        status = player.get("status")
        has_stats = player.get("score") is not None
        if has_stats:
            wlr = finite_number(player.get("wlr"))
            kdr = finite_number(player.get("kdr"))
            score = finite_number(player.get("score"))
            metrics = f"{category.upper()}  ·  Score {score:.2f}  ·  WLR {wlr:.2f}  KDR {kdr:.2f}"
            previous_scope = self._previous_scope(player)
            stale_scope = previous_scope is not None and previous_scope != self._selected_scope()
            if status == "retrying":
                retries = int(finite_number(player.get("retryCount")))
                prefix = f"RETRY {retries}/3"
                if stale_scope:
                    prefix += f" · LAST {previous_scope}"
                meta_text = f"{prefix}  ·  {metrics}"
            elif status == "refreshing":
                prefix = "REFRESHING"
                if stale_scope:
                    prefix += f" · LAST {previous_scope}"
                meta_text = f"{prefix}  ·  {metrics}"
            elif status == "nicked":
                prefix = "STALE · REFRESH FAILED"
                if previous_scope is not None:
                    prefix += f" · LAST {previous_scope}"
                meta_text = f"{prefix}  ·  {metrics}"
            else:
                meta_text = metrics
        elif status == "nicked":
            meta_text = "Nicked or stats disabled"
        elif status == "retrying":
            retries = int(finite_number(player.get("retryCount")))
            meta_text = f"Retrying stats ({retries}/3)..."
        elif status == "loading":
            meta_text = "Loading stats..."
        else:
            meta_text = "Stats unavailable"
        meta = CopyableLabel(meta_text)
        meta.setObjectName("playerMeta")
        self._enable_copying(meta)
        meta.clicked.connect(card.clicked.emit)
        details.addWidget(meta)
        row.addLayout(details, 1)

        fkdr_column = QVBoxLayout()
        fkdr_column.setContentsMargins(0, 0, 0, 0)
        fkdr_column.setSpacing(0)
        if has_stats:
            fkdr = CopyableLabel(f"{finite_number(player.get('fkdr')):.2f}")
            caption = CopyableLabel("FKDR")
        else:
            fkdr = CopyableLabel("…" if status in {"loading", "retrying"} else "—")
            caption = CopyableLabel("NICKED" if status == "nicked" else "STATS")
        fkdr.setObjectName("fkdrValue")
        caption.setObjectName("fkdrCaption")
        for label in (fkdr, caption):
            self._enable_copying(label)
            label.clicked.connect(card.clicked.emit)
        fkdr_column.addWidget(fkdr, 0, Qt.AlignmentFlag.AlignRight)
        fkdr_column.addWidget(caption, 0, Qt.AlignmentFlag.AlignRight)
        row.addLayout(fkdr_column)
        return card

    def _open_player_details(self, name: str) -> None:
        self.selected_player_name = name
        self._detail_request_counter += 1
        self._active_detail_request = str(self._detail_request_counter)
        self._detail_data = None
        self._detail_timer.stop()
        request = {
            "type": "player_details_request",
            "name": name,
            "requestId": self._active_detail_request,
            "mode": self.state.get("mode", "ALL_MODES"),
            "period": self.state.get("period", "total"),
        }
        if not self.socket_client.send_json(request):
            self._detail_data = {
                "name": name,
                "statsError": "ViaProxy is not connected",
                "punishmentsError": "ViaProxy is not connected",
            }
        else:
            self._detail_timer.start(15000)
        self._render()

    def _on_detail_timeout(self) -> None:
        if self.selected_player_name is None or self._detail_data is not None:
            return
        self._detail_data = {
            "statsError": "No response from ViaProxy. Restart ViaProxy and try again.",
            "punishmentsError": "No response from ViaProxy. Restart ViaProxy and try again.",
        }
        self._render()

    def _close_player_details(self) -> None:
        self._detail_timer.stop()
        self.selected_player_name = None
        self._active_detail_request = ""
        self._detail_data = None
        self._render()

    def _player_detail_view(self, name: str) -> QWidget:
        card = QFrame()
        card.setObjectName("playerCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        title = QLabel(name)
        title.setObjectName("detailTitle")
        self._enable_copying(title)
        layout.addWidget(title)

        data = self._detail_data
        if data is None:
            self._add_detail_text(layout, "Loading detailed stats and punishments...", "playerMeta")
            return card

        scope = self._scope_label(data.get("mode", self.state.get("mode")),
                                  data.get("period", self.state.get("period")))
        self._add_detail_text(layout, scope, "playerMeta")

        stats = data.get("stats")
        layout.addWidget(self._detail_heading("BEDWARS STATS"))
        if isinstance(stats, dict):
            stat_rows = (
                ("FKDR", stats.get("fkdr", "—")), ("WLR", stats.get("wlr", "—")),
                ("KDR", stats.get("kdr", "—")), ("Wins", stats.get("wins", "—")),
                ("Losses", stats.get("losses", "—")), ("Games", stats.get("gamesPlayed", "—")),
                ("Final kills", stats.get("finalKills", "—")),
                ("Final deaths", stats.get("finalDeaths", "—")),
                ("Kills", stats.get("kills", "—")), ("Deaths", stats.get("deaths", "—")),
                ("Beds destroyed", stats.get("bedsDestroyed", "—")),
                ("Best winstreak", stats.get("highestWinStreakReached", "—")),
                ("Melee kills", stats.get("meleeKills", "—")),
                ("Bow kills", stats.get("bowKills", "—")),
                ("Void kills", stats.get("voidKills", "—")),
                ("Arrows shot", stats.get("arrowsShot", "—")),
                ("Arrows hit", stats.get("arrowsHit", "—")),
            )
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(5)
            for index, (label_text, value) in enumerate(stat_rows):
                column = (index % 2) * 2
                row = index // 2
                label = QLabel(label_text)
                label.setObjectName("detailMetric")
                value_label = QLabel(str(value))
                value_label.setObjectName("detailValue")
                for widget in (label, value_label):
                    widget.setTextFormat(Qt.TextFormat.PlainText)
                    self._enable_copying(widget)
                grid.addWidget(label, row, column)
                grid.addWidget(value_label, row, column + 1, Qt.AlignmentFlag.AlignRight)
            layout.addLayout(grid)
        else:
            self._add_detail_text(layout, str(data.get("statsError", "Stats unavailable")), "playerMeta")

        layout.addWidget(self._detail_heading("PUNISHMENTS"))
        punishments = data.get("punishments")
        if isinstance(punishments, dict):
            counts = (f"Bans {punishments.get('bans', 0)}  ·  Mutes {punishments.get('mutes', 0)}  ·  "
                      f"Warns {punishments.get('warns', 0)}  ·  Kicks {punishments.get('kicks', 0)}")
            self._add_detail_text(layout, counts, "playerMeta")
            entries = punishments.get("entries", [])
            if isinstance(entries, list) and entries:
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    type_names = {"B": "Ban", "K": "Kick", "M": "Mute", "W": "Warn"}
                    type_name = type_names.get(str(entry.get("type", "")), "Punishment")
                    date = str(entry.get("date", "")).strip()
                    reason = str(entry.get("reason", "")).strip() or "No reason listed"
                    text = f"{type_name} {date}: {reason}" if date else f"{type_name}: {reason}"
                    self._add_detail_text(layout, text, "punishmentEntry", wrap=True)
            else:
                self._add_detail_text(layout, "No recent punishment entries found.", "playerMeta")
        else:
            self._add_detail_text(layout, str(data.get("punishmentsError", "Punishments unavailable")),
                                  "playerMeta", wrap=True)
        return card

    @staticmethod
    def _detail_heading(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("detailSection")
        label.setTextFormat(Qt.TextFormat.PlainText)
        OverlayWindow._enable_copying(label)
        return label

    @staticmethod
    def _enable_copying(label: QLabel, *, links: bool = False) -> None:
        flags = Qt.TextInteractionFlag.TextSelectableByMouse
        if links:
            flags |= Qt.TextInteractionFlag.LinksAccessibleByMouse
        label.setTextInteractionFlags(flags)
        label.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        label.setOpenExternalLinks(links)

    @staticmethod
    def _linkified_html(text: str) -> str:
        parts: list[str] = []
        cursor = 0
        for match in URL_PATTERN.finditer(text):
            url = match.group(0)
            trimmed = url.rstrip(".,;:!?")
            # Keep balanced parentheses and brackets used inside URLs.
            while trimmed.endswith(")") and trimmed.count("(") < trimmed.count(")"):
                trimmed = trimmed[:-1]
            while trimmed.endswith("]") and trimmed.count("[") < trimmed.count("]"):
                trimmed = trimmed[:-1]
            suffix = url[len(trimmed):]
            parts.append(html.escape(text[cursor:match.start()]).replace("\n", "<br>"))
            safe_url = html.escape(trimmed, quote=True)
            parts.append(f'<a href="{safe_url}" style="color:#8fc7ff;">{html.escape(trimmed)}</a>')
            parts.append(html.escape(suffix))
            cursor = match.end()
        parts.append(html.escape(text[cursor:]).replace("\n", "<br>"))
        return "".join(parts)

    @staticmethod
    def _add_detail_text(layout: QVBoxLayout, text: str, object_name: str, wrap: bool = False) -> None:
        label = QLabel(text)
        label.setObjectName(object_name)
        has_links = URL_PATTERN.search(text) is not None
        if has_links:
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setText(OverlayWindow._linkified_html(text))
        else:
            label.setTextFormat(Qt.TextFormat.PlainText)
        OverlayWindow._enable_copying(label, links=has_links)
        label.setWordWrap(wrap)
        layout.addWidget(label)

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(bounds, 17, 17)
        gradient = QLinearGradient(bounds.topLeft(), bounds.bottomRight())
        gradient.setColorAt(0.0, QColor(35, 47, 66, self._background_alpha))
        gradient.setColorAt(0.55, QColor(19, 26, 39, self._background_alpha))
        gradient.setColorAt(1.0, QColor(12, 18, 29, self._background_alpha))
        painter.fillPath(path, gradient)
        painter.setPen(QPen(QColor(198, 219, 245, 74), 1))
        painter.drawPath(path)
        painter.end()

    def moveEvent(self, event) -> None:
        self.config["overlay"]["x"] = self.x()
        self.config["overlay"]["y"] = self.y()
        super().moveEvent(event)

    def resizeEvent(self, event) -> None:
        self.config["overlay"]["width"] = self.width()
        self.config["overlay"]["height"] = self.height()
        super().resizeEvent(event)

    def closeEvent(self, event) -> None:
        save_config(self.config)
        self.socket_client.stop()
        event.accept()


def main() -> int:
    config = load_config()
    app = QApplication([])
    app.setApplicationName("PikaStats Overlay")
    host = str(config["websocket"].get("host", "127.0.0.1"))
    if host not in ("127.0.0.1", "localhost"):
        print("[PikaStats Overlay] WebSocket host must stay local (127.0.0.1). Using 127.0.0.1.")
        host = "127.0.0.1"
    port = int(config["websocket"].get("port", 8765))
    socket_client = SocketClient(f"ws://{host}:{port}/pikastats")
    window = OverlayWindow(config, socket_client)
    hotkey = GlobalHotkey(str(config["overlay"].get("hotkey", "F8")), window.toggle_requested.emit)
    hotkey.start()
    socket_client.start()

    if config["overlay"].get("enabled", True):
        window.show()
    else:
        window.hide()
    app.aboutToQuit.connect(hotkey.stop)
    app.aboutToQuit.connect(socket_client.stop)
    app.aboutToQuit.connect(lambda: save_config(config))
    try:
        return app.exec()
    finally:
        hotkey.stop()
        socket_client.stop()
        save_config(config)


if __name__ == "__main__":
    raise SystemExit(main())
