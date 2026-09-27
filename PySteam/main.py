#!/usr/bin/env python3
"""
Transparent Steam library launcher.

This does NOT reimplement Steam -- it can't (see README for why). Instead:
  - It reads your real library/friends via Steam's official Web API.
  - It launches games by making the real, locally-installed Steam client
    do it via steam:// URIs (steam://run/<appid>), the same mechanism
    Steam's own shortcuts and other launchers use.
  - The window itself is a fully custom, transparent PyQt6 UI, so it can
    look however you want and sit on top of your blurred desktop --
    unlike theming the real Steam client (Millennium), which depends on
    reverse-engineering Steam's internal UI and breaks on updates.

Requires:
    sudo pacman -S python-pyqt6
(no QtWebEngine needed here -- this is a native widget UI, not a browser)

First run: you'll be asked for a Steam Web API key and your SteamID64.
See README.md for how to get both.

Run:
    ./run.sh
    python3 main.py
"""

import sys
import os
import json
import time

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QPixmap, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QDialog, QFormLayout, QMessageBox, QTabWidget, QSizeGrip,
    QMenu, QRadioButton,
)

import steam_api

APP_NAME = "SteamTransparent"
CONFIG_DIR = os.path.expanduser(f"~/.config/{APP_NAME}")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


# ---------------------------------------------------------------------------
# Steam client look: the real client's palette (dark navy + blue accent),
# adapted for a translucent window.
# ---------------------------------------------------------------------------
STEAM_BG = "rgba(27, 40, 56, 215)"        # #1b2838 library background
STEAM_DARK = "#171a21"                     # top bar / tab strip
STEAM_ACCENT = "#66c0f4"                   # links, highlights
STEAM_TEXT = "#c7d5e0"                     # primary text
STEAM_DIM = "#8f98a0"                      # secondary text
STEAM_SELECTED = "#2a475e"                 # selected rows / active tab
STEAM_GREEN = "#5c7e10"                    # PLAY button
STEAM_GREEN_HOVER = "#7a9e14"
STEAM_BLUE = "#1a9fff"                     # install / action button
STEAM_BLUE_HOVER = "#47bfff"

BTN_DARK = (
    "QPushButton { background: " + STEAM_SELECTED + "; color: white; "
    "padding: 6px; border-radius: 4px; }"
    "QPushButton:hover { background: #3d6a8f; }"
)
SEARCH_STYLE = (
    "QLineEdit { background: rgba(14, 20, 27, 200); color: " + STEAM_TEXT + "; "
    "border: 1px solid " + STEAM_SELECTED + "; border-radius: 4px; padding: 6px; }"
)


# ---------------------------------------------------------------------------
# Config (API key + SteamID64), stored locally, never sent anywhere but
# Steam's own API.
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return {}


def save_config(cfg: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)


class SetupDialog(QDialog):
    """First-run dialog asking for the Steam Web API key + SteamID64."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Steam Transparent -- Setup")
        self.resize(420, 200)

        layout = QVBoxLayout(self)

        info = QLabel(
            "Get an API key at:\n"
            "  https://steamcommunity.com/dev/apikey\n\n"
            "Find your SteamID64 at:\n"
            "  https://steamid.io  (paste your profile URL)"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        self.api_key_input = QLineEdit()
        self.steam_id_input = QLineEdit()
        form.addRow("API key:", self.api_key_input)
        form.addRow("SteamID64:", self.steam_id_input)
        layout.addLayout(form)

        save_btn = QPushButton("Save && Continue")
        save_btn.clicked.connect(self.accept)
        layout.addWidget(save_btn)

    def values(self):
        return self.api_key_input.text().strip(), self.steam_id_input.text().strip()


# ---------------------------------------------------------------------------
# Background workers so network calls never freeze the UI
# ---------------------------------------------------------------------------

class LibraryLoader(QThread):
    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, api_key, steam_id):
        super().__init__()
        self.api_key = api_key
        self.steam_id = steam_id

    def run(self):
        try:
            games = steam_api.get_owned_games(self.api_key, self.steam_id)
            games.sort(key=lambda g: g.get("name", "").lower())
            self.loaded.emit(games)
        except steam_api.SteamAPIError as e:
            self.failed.emit(str(e))


class FriendsLoader(QThread):
    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, api_key, steam_id):
        super().__init__()
        self.api_key = api_key
        self.steam_id = steam_id

    def run(self):
        try:
            friends = steam_api.get_friend_list(self.api_key, self.steam_id)
            ids = [f["steamid"] for f in friends]
            summaries = []
            # Batch in groups of 100 (API limit).
            for i in range(0, len(ids), 100):
                summaries.extend(steam_api.get_player_summaries(self.api_key, ids[i:i + 100]))
            summaries.sort(key=lambda p: (-p.get("personastate", 0), p.get("personaname", "").lower()))
            self.loaded.emit(summaries)
        except steam_api.SteamAPIError as e:
            self.failed.emit(str(e))


class IconLoader(QThread):
    """Loads one game/friend icon image in the background and hands back
    the raw bytes -- keeps scrolling/searching smooth even with a big
    library."""
    loaded = pyqtSignal(object, bytes)  # (target_item, image_bytes)

    def __init__(self, target_item, url):
        super().__init__()
        self.target_item = target_item
        self.url = url

    def run(self):
        try:
            data = steam_api.fetch_image_bytes(self.url)
            self.loaded.emit(self.target_item, data)
        except Exception:
            pass  # Missing icon is not worth interrupting anything for.


class SteamOpWatcher(QThread):
    """Watches a client-side operation (install/uninstall) via on-disk
    state so the UI knows when Steam is no longer needed.

    Install: waits for the download to appear (user still has to confirm
    Steam's own install dialog -- it can't be suppressed), reports MB
    progress, and finishes when the appmanifest lands.
    Uninstall: finishes once the appmanifest is gone.
    """
    update = pyqtSignal(str)          # status-line text
    done = pyqtSignal(bool, str)      # (success, message)

    def __init__(self, appid: int, mode: str, game_name: str = ""):
        super().__init__()
        self.appid = appid
        self.mode = mode
        self.game_name = game_name or f"App {appid}"

    def run(self):
        try:
            if self.mode == "install":
                self._watch_install()
            else:
                self._watch_uninstall()
        except Exception as e:
            self.done.emit(False, str(e))

    def _watch_install(self):
        # Phase 1: user confirms Steam's install dialog (up to 5 min).
        for _ in range(75):
            in_prog, _mb = steam_api.download_progress(self.appid)
            if in_prog or steam_api.is_game_installed(self.appid):
                break
            time.sleep(4)
        else:
            self.done.emit(False, "Install never started (dialog dismissed?).")
            return
        # Phase 2: download in flight -- report progress.
        while True:
            in_prog, size = steam_api.download_progress(self.appid)
            if not in_prog:
                break
            self.update.emit(
                f"Downloading {self.game_name} — {size / 1e9:.2f} GB so far…")
            time.sleep(4)
        if steam_api.is_game_installed(self.appid):
            self.done.emit(True, f"{self.game_name} installed.")
        else:
            self.done.emit(False, "Download stopped before finishing.")

    def _watch_uninstall(self):
        for _ in range(120):  # up to ~10 min for the removal to land
            if not steam_api.is_game_installed(self.appid):
                self.done.emit(True, f"{self.game_name} uninstalled.")
                return
            time.sleep(5)
        self.done.emit(False, "Still installed — Steam may need attention.")


class StoreSearchLoader(QThread):
    loaded = pyqtSignal(str, list)  # (term searched, results)
    failed = pyqtSignal(str)

    def __init__(self, term):
        super().__init__()
        self.term = term

    def run(self):
        try:
            results = steam_api.store_search(self.term)
            self.loaded.emit(self.term, results)
        except steam_api.SteamAPIError as e:
            self.failed.emit(str(e))


ONLINE_STATE_LABELS = {
    0: "Offline", 1: "Online", 2: "Busy", 3: "Away",
    4: "Snooze", 5: "Looking to trade", 6: "Looking to play",
}


class TitleBar(QWidget):
    def __init__(self, parent_window, title="STEAM"):
        super().__init__(parent_window)
        self._parent_window = parent_window
        self.setFixedHeight(34)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 8, 0)

        label = QLabel(title)
        label.setStyleSheet(
            "color: " + STEAM_ACCENT + "; font-weight: bold; font-size: 13px; "
            "background: transparent; letter-spacing: 2px;")
        layout.addWidget(label)
        layout.addStretch()

        min_btn = QPushButton("–")
        close_btn = QPushButton("✕")
        for b in (min_btn, close_btn):
            b.setFixedSize(28, 24)
            b.setStyleSheet(
                "QPushButton { color: " + STEAM_DIM + "; background: transparent; "
                "border: none; border-radius: 3px; font-size: 12px; }"
                "QPushButton:hover { background: rgba(255,255,255,40); color: white; }"
            )
            layout.addWidget(b)
        min_btn.clicked.connect(parent_window.showMinimized)
        close_btn.clicked.connect(parent_window.close)

        self.setStyleSheet("background: " + STEAM_DARK + ";")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._parent_window.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()


class DetailsLoader(QThread):
    """Store details (developer, release, blurb, capsule) for Properties."""
    loaded = pyqtSignal(dict)

    def __init__(self, appid):
        super().__init__()
        self.appid = appid

    def run(self):
        self.loaded.emit(steam_api.get_store_details(self.appid))


class PropertiesDialog(QDialog):
    """Right-click → Properties, Steam-style tabs.

    All data comes from on-disk/API sources (appmanifest, localconfig,
    store details) -- no client internals. Writes (launch options,
    update mode) only happen while Steam is fully closed; if our hidden
    client is running we offer to shut it down first, and if it's the
    user's own client we refuse rather than risk clobbering its files.
    """

    def __init__(self, parent, appid: int, name: str, playtime_min: int = 0):
        super().__init__(parent)
        self._main = parent
        self.appid = appid
        self.game_name = name
        self.setWindowTitle(f"{name} — Properties")
        self.resize(540, 600)
        self.setStyleSheet(
            "QDialog { background: " + STEAM_DARK + "; color: " + STEAM_TEXT + "; } "
            "QLabel { color: " + STEAM_TEXT + "; } "
            "QTabWidget::pane { border: none; border-top: 2px solid " + STEAM_SELECTED + "; } "
            "QTabBar::tab { background: #0e141b; color: " + STEAM_DIM + "; "
            "padding: 7px 16px; margin-right: 2px; } "
            "QTabBar::tab:selected { background: " + STEAM_SELECTED + "; color: white; } "
            "QLineEdit { background: #0e141b; color: " + STEAM_TEXT + "; "
            "border: 1px solid " + STEAM_SELECTED + "; border-radius: 4px; padding: 6px; } "
            "QRadioButton { color: " + STEAM_TEXT + "; spacing: 8px; padding: 4px; } "
        )

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        tabs.addTab(self._general_tab(playtime_min), "GENERAL")
        tabs.addTab(self._launch_tab(), "LAUNCH OPTIONS")
        tabs.addTab(self._updates_tab(), "UPDATES")
        tabs.addTab(self._files_tab(), "INSTALLED FILES")

        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(BTN_DARK)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignRight)

        self._details_loader = DetailsLoader(appid)
        self._details_loader.loaded.connect(self._on_details)
        self._details_loader.start()

    # -- shared ------------------------------------------------------
    def _note(self, text):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: " + STEAM_DIM + "; font-size: 11px;")
        return lbl

    def _require_stopped(self):
        """Ensure Steam is fully closed before a file write. Returns True
        if writes may proceed."""
        if not steam_api.is_steam_running():
            return True
        if not self._main._steam_we_started:
            QMessageBox.warning(
                self, "Steam is running",
                "Your own Steam client is open, so settings files can't be "
                "edited safely right now (Steam rewrites them on exit). "
                "Close Steam and reopen Properties to edit.")
            return False
        ok = QMessageBox.question(
            self, "Close hidden Steam?",
            "The hidden Steam client must exit to save this setting "
            "(it rewrites its files on exit). Close it now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ok != QMessageBox.StandardButton.Yes:
            return False
        steam_api.shutdown()
        for _ in range(20):
            time.sleep(0.5)
            if not steam_api.is_steam_running():
                self._main._set_status("Steam closed for settings edit.")
                return True
        QMessageBox.warning(self, "Still running",
                            "Steam didn't exit in time — try again.")
        return False

    # -- General -----------------------------------------------------
    def _general_tab(self, playtime_min):
        w = QWidget()
        layout = QVBoxLayout(w)
        self._capsule = QLabel("loading artwork…")
        self._capsule.setFixedHeight(150)
        self._capsule.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._capsule.setStyleSheet("background: #0e141b; border-radius: 4px;")
        layout.addWidget(self._capsule)

        title = QLabel(self.game_name)
        title.setStyleSheet("color: white; font-size: 16px; font-weight: bold;")
        title.setWordWrap(True)
        layout.addWidget(title)

        form = QFormLayout()
        form.addRow("App ID:", QLabel(str(self.appid)))
        self._dev_label = QLabel("…")
        self._rel_label = QLabel("…")
        form.addRow("Developer:", self._dev_label)
        form.addRow("Released:", self._rel_label)
        hrs = playtime_min / 60
        form.addRow("Playtime:", QLabel(
            f"{hrs:.1f} hours" if hrs >= 0.1 else "Never played"))
        man = steam_api.read_manifest(self.appid)
        if man:
            size = int(man.get("SizeOnDisk", 0) or 0)
            form.addRow("Status:", QLabel(
                f"✓ Installed ({size / 1e9:.2f} GB on disk)"))
        else:
            form.addRow("Status:", QLabel("Not installed"))
        layout.addLayout(form)

        self._blurb = QLabel("")
        self._blurb.setWordWrap(True)
        self._blurb.setStyleSheet("color: " + STEAM_DIM + "; font-size: 11px;")
        layout.addWidget(self._blurb)
        layout.addStretch()
        return w

    def _on_details(self, d):
        if not d:
            self._dev_label.setText("unknown")
            self._rel_label.setText("unknown")
            return
        self._dev_label.setText(d.get("developers") or "unknown")
        self._rel_label.setText(d.get("release") or "unknown")
        if d.get("blurb"):
            self._blurb.setText(d["blurb"])
        if d.get("header"):
            loader = IconLoader(None, d["header"])
            loader.loaded.connect(self._on_capsule)
            self._main._icon_loaders.append(loader)
            loader.start()

    def _on_capsule(self, _item, data):
        pix = QPixmap()
        if pix.loadFromData(data):
            self._capsule.setPixmap(pix.scaledToWidth(
                500, Qt.TransformationMode.SmoothTransformation))

    # -- Launch options ----------------------------------------------
    def _launch_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("Launch options (e.g. gamemoderun %command%):"))
        self._launch_edit = QLineEdit()
        try:
            self._launch_edit.setText(steam_api.get_launch_options(self.appid))
        except Exception:
            pass
        layout.addWidget(self._launch_edit)
        row = QHBoxLayout()
        save = QPushButton("Save")
        save.setStyleSheet(BTN_DARK)
        save.clicked.connect(self._save_launch)
        clear = QPushButton("Clear")
        clear.setStyleSheet(BTN_DARK)
        clear.clicked.connect(lambda: self._launch_edit.setText(""))
        row.addWidget(save)
        row.addWidget(clear)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(self._note(
            "Stored in Steam's localconfig.vdf (backed up as .bak). "
            "Steam must be closed to save — you'll be asked if needed."))
        layout.addStretch()
        return w

    def _save_launch(self):
        if not self._require_stopped():
            return
        if steam_api.set_launch_options(self.appid, self._launch_edit.text()):
            QMessageBox.information(self, "Saved", "Launch options saved.")
        else:
            QMessageBox.warning(self, "Failed",
                                "Could not write launch options.")

    # -- Updates ------------------------------------------------------
    def _updates_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("Automatic updates:"))
        man = steam_api.read_manifest(self.appid)
        current = man.get("AutoUpdateBehavior", "0") if man else "0"
        self._update_radios = {}
        for key, label in steam_api.AUTO_UPDATE_LABELS.items():
            r = QRadioButton(label)
            r.setChecked(key == current)
            r.setEnabled(bool(man))
            self._update_radios[key] = r
            layout.addWidget(r)
        save = QPushButton("Save")
        save.setStyleSheet(BTN_DARK)
        save.setEnabled(bool(man))
        save.clicked.connect(self._save_updates)
        layout.addWidget(save, 0, Qt.AlignmentFlag.AlignLeft)
        if not man:
            layout.addWidget(self._note(
                "Update mode lives in the appmanifest, so it only applies "
                "to installed games."))
        layout.addStretch()
        return w

    def _save_updates(self):
        if not self._require_stopped():
            return
        chosen = next((k for k, r in self._update_radios.items()
                       if r.isChecked()), "0")
        if steam_api.set_manifest_value(self.appid, "AutoUpdateBehavior", chosen):
            QMessageBox.information(self, "Saved", "Update mode saved.")
        else:
            QMessageBox.warning(self, "Failed", "Could not write manifest.")

    # -- Installed files ----------------------------------------------
    def _files_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        path = steam_api.game_install_path(self.appid)
        man = steam_api.read_manifest(self.appid)
        if not man:
            layout.addWidget(QLabel("Not installed — nothing on disk."))
            layout.addStretch()
            return w
        size = int(man.get("SizeOnDisk", 0) or 0)
        layout.addWidget(QLabel(f"Size on disk: {size / 1e9:.2f} GB"))
        p = QLabel(path or "unknown location")
        p.setWordWrap(True)
        p.setStyleSheet("color: " + STEAM_ACCENT + ";")
        layout.addWidget(p)
        row = QHBoxLayout()
        for label, fn in (("Verify integrity", self._verify),
                          ("Browse files", self._browse),
                          ("Uninstall…", self._uninstall)):
            b = QPushButton(label)
            b.setStyleSheet(BTN_DARK)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        return w

    def _verify(self):
        steam_api.validate_game(self.appid)
        self._main._set_status(f"Verifying {self.game_name} in Steam…")

    def _browse(self):
        if not steam_api.open_game_folder(self.appid):
            QMessageBox.warning(self, "Failed", "Install folder not found.")

    def _uninstall(self):
        self.accept()
        self._main.on_uninstall_clicked()


class MainWindow(QMainWindow):
    def __init__(self, api_key, steam_id):
        super().__init__()
        self.api_key = api_key
        self.steam_id = steam_id
        self._icon_loaders = []  # keep references alive while running

        self.setWindowTitle("Steam (Transparent)")
        self.resize(900, 650)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)

        central = QWidget()
        central.setStyleSheet(
            "background: " + STEAM_BG + "; border-radius: 8px;")
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(TitleBar(self))

        # Make sure the real Steam client is alive so steam:// launches work.
        # Hidden (tray-only via -silent): no main window, downloads and
        # Proton/DRM still fully work. Remember whether WE started it --
        # auto-shutdown after background ops only touches our own instance,
        # never a client the user already had open.
        self._steam_we_started = not steam_api.is_steam_running()
        try:
            steam_api.ensure_steam_running()
        except steam_api.SteamAPIError as e:
            QMessageBox.warning(self, "Steam not found", str(e))

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        body_layout = QVBoxLayout(body)
        outer.addWidget(body)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search your library...")
        self.search_box.textChanged.connect(self.filter_library)
        self.search_box.setStyleSheet(SEARCH_STYLE)
        body_layout.addWidget(self.search_box)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            "QTabWidget::pane { border: none; background: transparent; "
            "border-top: 2px solid " + STEAM_SELECTED + "; } "
            "QTabBar::tab { background: " + STEAM_DARK + "; color: " + STEAM_DIM + "; "
            "padding: 8px 18px; margin-right: 2px; "
            "border-top-left-radius: 4px; border-top-right-radius: 4px; } "
            "QTabBar::tab:selected { background: " + STEAM_SELECTED + "; color: white; } "
            "QTabBar::tab:hover:!selected { color: " + STEAM_TEXT + "; }"
        )
        body_layout.addWidget(self.tabs)

        self.library_list = QListWidget()
        self.library_list.setIconSize(QSize(48, 48))
        self.library_list.itemDoubleClicked.connect(self.on_launch_clicked)
        self.library_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.library_list.customContextMenuRequested.connect(
            self.on_library_context_menu)
        self._style_list(self.library_list)
        self.tabs.addTab(self.library_list, "LIBRARY")

        self.friends_list = QListWidget()
        self.friends_list.setIconSize(QSize(40, 40))
        self.friends_list.itemDoubleClicked.connect(self.on_friend_open_profile)
        self._style_list(self.friends_list)
        self.tabs.addTab(self.friends_list, "FRIENDS")

        # Store tab: in-app search (public search endpoint), results open
        # in the real client's store UI.
        store_tab = QWidget()
        store_tab.setStyleSheet("background: transparent;")
        store_layout = QVBoxLayout(store_tab)
        store_layout.setContentsMargins(0, 0, 0, 0)
        self.store_box = QLineEdit()
        self.store_box.setPlaceholderText("Search the Steam store… (Enter to search)")
        self.store_box.returnPressed.connect(self.search_store)
        self.store_box.setStyleSheet(SEARCH_STYLE)
        store_layout.addWidget(self.store_box)
        self.store_list = QListWidget()
        self.store_list.setIconSize(QSize(120, 45))
        self.store_list.itemDoubleClicked.connect(self.on_store_open_page)
        self._style_list(self.store_list)
        store_layout.addWidget(self.store_list)
        self.tabs.addTab(store_tab, "STORE")

        launch_row = QHBoxLayout()
        launch_btn = QPushButton("▶  PLAY")
        launch_btn.clicked.connect(self.on_launch_clicked)
        launch_btn.setStyleSheet(
            "QPushButton { background: " + STEAM_GREEN + "; color: white; "
            "font-weight: bold; padding: 9px; border-radius: 4px; }"
            "QPushButton:hover { background: " + STEAM_GREEN_HOVER + "; }"
        )
        launch_row.addWidget(launch_btn)

        install_btn = QPushButton("INSTALL")
        install_btn.clicked.connect(self.on_install_clicked)
        install_btn.setStyleSheet(
            "QPushButton { background: " + STEAM_BLUE + "; color: white; "
            "font-weight: bold; padding: 9px; border-radius: 4px; }"
            "QPushButton:hover { background: " + STEAM_BLUE_HOVER + "; }"
        )
        launch_row.addWidget(install_btn)

        uninstall_btn = QPushButton("UNINSTALL")
        uninstall_btn.clicked.connect(self.on_uninstall_clicked)
        uninstall_btn.setStyleSheet(BTN_DARK)
        launch_row.addWidget(uninstall_btn)
        body_layout.addLayout(launch_row)

        # Community/store links for the selected library game. These live
        # in the real client's UI (no API exists), so they open Steam
        # windows via steam://openurl/.
        links_row = QHBoxLayout()
        for label, handler in (
            ("Store Page", self.on_store_page_clicked),
            ("Hub", self.on_hub_clicked),
            ("Workshop", self.on_workshop_clicked),
            ("Guides", self.on_guides_clicked),
        ):
            b = QPushButton(label)
            b.clicked.connect(handler)
            b.setStyleSheet(BTN_DARK)
            links_row.addWidget(b)
        chat_btn = QPushButton("Steam Chat")
        chat_btn.clicked.connect(lambda: steam_api.open_friends())
        chat_btn.setStyleSheet(BTN_DARK)
        links_row.addWidget(chat_btn)
        front_btn = QPushButton("Store Front")
        front_btn.clicked.connect(lambda: steam_api.open_store_front())
        front_btn.setStyleSheet(BTN_DARK)
        links_row.addWidget(front_btn)
        body_layout.addLayout(links_row)

        # Status line: Steam client state + background-op progress.
        self.status_label = QLabel("Steam: hidden (tray only)")
        self.status_label.setStyleSheet("color: " + STEAM_DIM + "; padding: 4px 2px;")
        body_layout.addWidget(self.status_label)

        grip = QSizeGrip(self)
        outer.addWidget(grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)

        self._all_games = []
        self.reload_library()
        self.reload_friends()

    def _style_list(self, widget: QListWidget):
        widget.setStyleSheet(
            "QListWidget { background: rgba(0,0,0,70); color: " + STEAM_TEXT + "; "
            "border: none; outline: none; }"
            "QListWidget::item { padding: 8px; border: none; }"
            "QListWidget::item:hover { background: rgba(102,192,244,25); }"
            "QListWidget::item:selected { background: " + STEAM_SELECTED + "; color: white; }"
        )

    # --- Library -----------------------------------------------------

    def reload_library(self):
        self.loader = LibraryLoader(self.api_key, self.steam_id)
        self.loader.loaded.connect(self.on_library_loaded)
        self.loader.failed.connect(lambda msg: QMessageBox.warning(self, "Failed to load library", msg))
        self.loader.start()

    def on_library_loaded(self, games):
        self._all_games = games
        self.populate_library(games)

    def populate_library(self, games):
        self.library_list.clear()
        for game in games:
            label = game.get("name", f"App {game['appid']}")
            try:
                if steam_api.is_game_installed(game["appid"]):
                    label += "  ✓ installed"
            except Exception:
                pass
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, game["appid"])
            self.library_list.addItem(item)
            icon_hash = game.get("img_icon_url")
            if icon_hash:
                url = steam_api.game_icon_url(game["appid"], icon_hash)
                loader = IconLoader(item, url)
                loader.loaded.connect(self.on_icon_loaded)
                self._icon_loaders.append(loader)
                loader.start()

    def on_icon_loaded(self, item, data):
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            item.setIcon(QIcon(pixmap))

    def filter_library(self, text):
        text = text.lower()
        filtered = [g for g in self._all_games if text in g.get("name", "").lower()]
        self.populate_library(filtered)

    def on_launch_clicked(self):
        item = self.library_list.currentItem()
        if item is None:
            return
        appid = item.data(Qt.ItemDataRole.UserRole)
        steam_api.launch_game(appid)
        # A launched game needs the client alive -- no auto-shutdown here.

    def _set_status(self, text):
        self.status_label.setText(text)

    def _maybe_shutdown_steam(self, reason):
        """Close the hidden client after a background op completes -- but
        only if we started it. Never touches a client the user had open
        themselves, and never called from the launch path."""
        if self._steam_we_started and steam_api.is_steam_running():
            steam_api.shutdown()
            self._set_status(f"Steam closed ({reason}) — not using compute.")
        else:
            self._set_status(f"{reason} — Steam left running.")

    def _selected_game(self):
        item = self.library_list.currentItem()
        if item is None:
            return None, None
        name = item.text().removesuffix("  ✓ installed")
        return item.data(Qt.ItemDataRole.UserRole), name

    def on_library_context_menu(self, pos):
        item = self.library_list.itemAt(pos)
        if item is None:
            return
        self.library_list.setCurrentItem(item)
        appid = item.data(Qt.ItemDataRole.UserRole)
        name = item.text().removesuffix("  ✓ installed")
        installed = steam_api.is_game_installed(appid)

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: " + STEAM_DARK + "; color: " + STEAM_TEXT + "; "
            "border: 1px solid " + STEAM_SELECTED + "; } "
            "QMenu::item:selected { background: " + STEAM_SELECTED + "; color: white; } "
            "QMenu::separator { height: 1px; background: " + STEAM_SELECTED + "; }"
        )
        act_play = menu.addAction("▶  Play")
        act_get = menu.addAction("Uninstall…" if installed else "Install…")
        menu.addSeparator()
        act_store = menu.addAction("Store Page")
        act_hub = menu.addAction("Community Hub")
        act_work = menu.addAction("Workshop")
        act_guides = menu.addAction("Guides")
        menu.addSeparator()
        act_props = menu.addAction("Properties…")

        chosen = menu.exec(self.library_list.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_play:
            self.on_launch_clicked()
        elif chosen == act_get:
            self.on_uninstall_clicked() if installed else self.on_install_clicked()
        elif chosen == act_store:
            steam_api.open_store_page(appid)
        elif chosen == act_hub:
            steam_api.open_game_hub(appid)
        elif chosen == act_work:
            steam_api.open_workshop(appid)
        elif chosen == act_guides:
            steam_api.open_guides(appid)
        elif chosen == act_props:
            playtime = 0
            for g in self._all_games:
                if g.get("appid") == appid:
                    playtime = g.get("playtime_forever", 0) or 0
                    break
            dlg = PropertiesDialog(self, appid, name, playtime)
            dlg.exec()

    def on_install_clicked(self):
        appid, name = self._selected_game()
        if appid is None:
            return
        if steam_api.is_game_installed(appid):
            QMessageBox.information(self, "Already installed",
                                    f"{name} is already downloaded.")
            return
        try:
            steam_api.install_game(appid)
        except steam_api.SteamAPIError as e:
            QMessageBox.warning(self, "Install failed", str(e))
            return
        # Steam pops its own location-picker dialog for this (can't be
        # suppressed); the watcher picks up once the download starts.
        self._set_status(f"Waiting for install confirm: {name}…")
        self._watcher = SteamOpWatcher(appid, "install", name)
        self._watcher.update.connect(self._set_status)
        self._watcher.done.connect(self._on_op_done)
        self._watcher.start()

    def on_uninstall_clicked(self):
        appid, name = self._selected_game()
        if appid is None:
            return
        if not steam_api.is_game_installed(appid):
            QMessageBox.information(self, "Not installed",
                                    f"{name} isn't downloaded.")
            return
        ok = QMessageBox.question(
            self, "Uninstall?",
            f"Uninstall {name}? Saves are usually kept, but double-check.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ok != QMessageBox.StandardButton.Yes:
            return
        try:
            steam_api.uninstall_game(appid)
        except steam_api.SteamAPIError as e:
            QMessageBox.warning(self, "Uninstall failed", str(e))
            return
        self._set_status(f"Uninstalling {name}…")
        self._watcher = SteamOpWatcher(appid, "uninstall", name)
        self._watcher.update.connect(self._set_status)
        self._watcher.done.connect(self._on_op_done)
        self._watcher.start()

    def _on_op_done(self, success, message):
        if success:
            self.reload_library()
            self._maybe_shutdown_steam(message)
        else:
            self._set_status(message)
            QMessageBox.warning(self, "Steam operation", message)

    # --- Store / Community (client UI via steam://openurl/) ------------

    def on_store_page_clicked(self):
        appid, _name = self._selected_game()
        if appid is None:
            return
        steam_api.open_store_page(appid)

    def on_hub_clicked(self):
        appid, _name = self._selected_game()
        if appid is None:
            return
        steam_api.open_game_hub(appid)

    def on_workshop_clicked(self):
        appid, _name = self._selected_game()
        if appid is None:
            return
        steam_api.open_workshop(appid)

    def on_guides_clicked(self):
        appid, _name = self._selected_game()
        if appid is None:
            return
        steam_api.open_guides(appid)

    def on_friend_open_profile(self):
        item = self.friends_list.currentItem()
        if item is None:
            return
        steam_id = item.data(Qt.ItemDataRole.UserRole)
        if steam_id:
            steam_api.open_profile(steam_id)

    # --- In-app store search --------------------------------------------

    def search_store(self):
        term = self.store_box.text().strip()
        if not term:
            return
        self._set_status(f"Searching store for “{term}”…")
        self.store_loader = StoreSearchLoader(term)
        self.store_loader.loaded.connect(self.on_store_loaded)
        self.store_loader.failed.connect(
            lambda msg: QMessageBox.warning(self, "Store search failed", msg))
        self.store_loader.start()

    @staticmethod
    def _format_price(item):
        price = item.get("price") or {}
        if not price:
            return ""
        try:
            final = price.get("final", 0) / 100
            cur = price.get("currency", "")
            if price.get("discount_percent"):
                return f"{final:.2f} {cur} (-{price['discount_percent']}%)"
            if final == 0:
                return "Free"
            return f"{final:.2f} {cur}"
        except (TypeError, ValueError):
            return ""

    def on_store_loaded(self, term, results):
        self._set_status(f"Store: {len(results)} results for “{term}”. "
                         "Double-click to open in Steam.")
        self.store_list.clear()
        for r in results:
            label = r.get("name", f"App {r.get('id')}")
            price = self._format_price(r)
            if price:
                label += f"  —  {price}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, r.get("id"))
            self.store_list.addItem(item)
            thumb = r.get("tiny_image")
            if thumb:
                loader = IconLoader(item, thumb)
                loader.loaded.connect(self.on_icon_loaded)
                self._icon_loaders.append(loader)
                loader.start()

    def on_store_open_page(self):
        item = self.store_list.currentItem()
        if item is None:
            return
        appid = item.data(Qt.ItemDataRole.UserRole)
        if appid:
            steam_api.open_store_page(appid)

    # --- Friends -------------------------------------------------------

    def reload_friends(self):
        self.friends_loader = FriendsLoader(self.api_key, self.steam_id)
        self.friends_loader.loaded.connect(self.on_friends_loaded)
        self.friends_loader.failed.connect(lambda msg: QMessageBox.warning(self, "Failed to load friends", msg))
        self.friends_loader.start()

    def on_friends_loaded(self, players):
        self.friends_list.clear()
        self._friend_ids = {}
        for p in players:
            state = ONLINE_STATE_LABELS.get(p.get("personastate", 0), "Unknown")
            item = QListWidgetItem(f"{p.get('personaname', 'Unknown')} — {state}")
            item.setData(Qt.ItemDataRole.UserRole, p.get("steamid", ""))
            self.friends_list.addItem(item)
            avatar = steam_api.avatar_url(p)
            if avatar:
                loader = IconLoader(item, avatar)
                loader.loaded.connect(self.on_icon_loaded)
                self._icon_loaders.append(loader)
                loader.start()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    cfg = load_config()
    if "api_key" not in cfg or "steam_id" not in cfg:
        dialog = SetupDialog()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)
        api_key, steam_id = dialog.values()
        if not api_key or not steam_id:
            QMessageBox.critical(None, "Missing info", "Both fields are required.")
            sys.exit(1)
        cfg = {"api_key": api_key, "steam_id": steam_id}
        save_config(cfg)

    window = MainWindow(cfg["api_key"], cfg["steam_id"])
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
