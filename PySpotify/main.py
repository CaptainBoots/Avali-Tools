#!/usr/bin/env python3
"""
Transparent Spotify wrapper using PyQt6 + QtWebEngine.

Loads open.spotify.com inside a native Qt window with:
  - A translucent/transparent window background (real OS-level alpha,
    not a CSS trick), so your desktop compositor's blur shows through.
  - Injected CSS that strips Spotify's own solid backgrounds so the
    transparency is actually visible through the page content too.
  - Persistent login (cookies/local storage saved to disk), so you
    only log in once.
  - Frameless window with a simple custom drag-to-move + close button,
    since removing the OS window frame removes the normal titlebar too.

Requires (install via pacman/AUR on CachyOS) -- or just run via run.sh,
which installs these automatically:
    sudo pacman -S python-pyqt6 python-pyqt6-webengine

Run:
    ./run.sh          # auto-installs missing deps, then launches
    python3 main.py   # if deps are already installed
"""

import sys
import os

try:
    from PyQt6.QtCore import Qt, QUrl
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QPushButton, QLabel, QSizeGrip
    )
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import (
        QWebEngineProfile, QWebEngineScript, QWebEnginePage,
        QWebEngineUrlRequestInterceptor,
    )
except ImportError:
    sys.stderr.write(
        "\nMissing PyQt6 / PyQt6-WebEngine.\n"
        "Run this instead, which installs dependencies automatically:\n\n"
        "    ./run.sh\n\n"
        "Or install manually:\n\n"
        "    sudo pacman -S python-pyqt6 python-pyqt6-webengine\n\n"
    )
    sys.exit(1)

APP_NAME = "SpotifyTransparent"
SPOTIFY_URL = "https://open.spotify.com"

# Persistent profile storage location, so login survives restarts.
PROFILE_DIR = os.path.expanduser(f"~/.local/share/{APP_NAME}")

# CSS injected into every page load. This targets Spotify's known
# top-level background containers. Spotify's internal class names can
# change over time (same issue as the Spicetify/Electron problems),
# so if a future Spotify update stops showing transparency, the fix
# is almost always: open real DevTools (see note below) and update
# this selector list.
INJECTED_CSS = """
html, body,
#main,
.Root__top-bar,
.Root__nav-bar,
.Root__now-playing-bar,
.Root__main-view,
[data-testid="root"],
[data-testid="topbar"],
[data-testid="now-playing-bar"],
[data-testid="left-sidebar"] {
    background: transparent !important;
    background-color: transparent !important;
    background-image: none !important;
}

/* Visual fallback: hide leftover ad banner containers even if a
   request slips past the network-level ad blocker (e.g. an ad served
   from a domain not yet in AD_HOST_FRAGMENTS). Network blocking above
   is the primary defense; this just hides the empty/broken slot. */
[data-testid="ad-banner"],
[data-testid="leaderboard-ad"],
.ad-banner,
.adsbygoogle {
    display: none !important;
}
"""


# --- Ad blocking ---------------------------------------------------------
#
# Network-level blocking (refusing the request outright) is more reliable
# than CSS-hiding an ad after it loads, since it also stops the ad audio
# stream itself, not just the visual banner.
#
# This list is a curated set of domains/path fragments known to serve ads
# or tracking for Spotify's web player. Like the CSS selectors above, this
# can go stale if Spotify changes ad infrastructure -- if ads start
# slipping through, check community adblock filter lists (e.g. uBlock
# Origin's default lists already block most of these) and add any new
# domains you find to AD_HOST_FRAGMENTS below.
AD_HOST_FRAGMENTS = [
    # Ad-serving / ad-request APIs
    "ads-fa.spotify.com",
    "adeng-pa.spotify.com",
    "adeng-sec.spotify.com",
    "spclient.wg.spotify.com/ads",
    "spclient.wg.spotify.com/melody",
    "gabo-receiver-service",
    "pagead2.googlesyndication.com",
    "googlesyndication.com",
    "googleadservices.com",
    "adservice.google.com",
    "doubleclick.net",
    "adclick.g.doubleclick.net",
    "pubads.g.doubleclick.net",
    "ads.pubmatic.com",
    "criteo.com",
    "outbrain.com",
    "taboola.com",
    # Tracking/analytics that piggybacks on ad delivery
    "scorecardresearch.com",
    "google-analytics.com",
    "googletagmanager.com",
    "analytics.twitter.com",
    "static.ads-twitter.com",
    "contentsquare.net",
]


# Spotify's login page checks the browser's user agent and shows a
# stripped-down login (often just a QR code) if it doesn't look like a
# normal desktop browser -- which QtWebEngine's default UA doesn't.
# Spoofing a real desktop Chrome UA gets the full login page back
# (password field + "Continue with Google/Facebook/Apple" buttons).
DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class PopupWindow(QMainWindow):
    """A plain, normal (non-transparent, has titlebar) window used to host
    OAuth login popups -- e.g. "Continue with Google" opens a real separate
    window in a browser, and QWebEngineView needs somewhere to put that."""

    def __init__(self, profile):
        super().__init__()
        self.setWindowTitle("Sign in")
        self.resize(500, 650)

        self.view = QWebEngineView()
        page = QWebEnginePage(profile, self)
        page.profile().setHttpUserAgent(DESKTOP_CHROME_UA)
        self.view.setPage(page)
        self.setCentralWidget(self.view)


class MainPage(QWebEnginePage):
    """QWebEnginePage subclass that knows how to open the popup windows
    OAuth logins (Google/Facebook/Apple) require. Without this,
    window.open() calls from the login page silently fail and the
    OAuth flow can never complete."""

    def __init__(self, profile, parent_window):
        super().__init__(profile, parent_window)
        self._parent_window = parent_window
        self._popups = []  # keep references so they aren't garbage collected

    def createWindow(self, _window_type):
        popup = PopupWindow(self.profile())
        popup.show()
        self._popups.append(popup)
        return popup.view.page()


class AdBlockInterceptor(QWebEngineUrlRequestInterceptor):
    """Blocks network requests to known ad/tracking hosts before they're sent."""

    def interceptRequest(self, info):
        url = info.requestUrl().toString().lower()
        for fragment in AD_HOST_FRAGMENTS:
            if fragment in url:
                info.block(True)
                return


def build_inject_script(css: str) -> QWebEngineScript:
    """Wrap the CSS string in a small JS snippet that injects a <style> tag,
    and set it to run at document creation *and* re-run on navigation so it
    survives Spotify's internal client-side page changes (it's a single-page app)."""
    js = f"""
    (function() {{
        function injectCSS() {{
            var id = 'transparent-spotify-style';
            if (document.getElementById(id)) return;
            var style = document.createElement('style');
            style.id = id;
            style.textContent = `{css}`;
            document.head.appendChild(style);
        }}
        injectCSS();
        // Spotify is a single-page app; re-check periodically in case
        // it replaces <head> content or the style gets stripped.
        setInterval(injectCSS, 2000);
    }})();
    """
    script = QWebEngineScript()
    script.setName("inject-transparency-css")
    script.setSourceCode(js)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
    script.setRunsOnSubFrames(False)
    script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
    return script


class TitleBar(QWidget):
    """Minimal custom titlebar: drag-to-move + close/minimize, since a
    frameless window has no native one."""

    def __init__(self, parent_window):
        super().__init__(parent_window)
        self._parent_window = parent_window
        self._drag_pos = None
        self.setFixedHeight(28)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)

        label = QLabel("Spotify")
        label.setStyleSheet("color: white; font-weight: bold; background: transparent;")
        layout.addWidget(label)
        layout.addStretch()

        min_btn = QPushButton("_")
        min_btn.setFixedSize(24, 24)
        min_btn.clicked.connect(parent_window.showMinimized)

        close_btn = QPushButton("x")
        close_btn.setFixedSize(24, 24)
        close_btn.clicked.connect(parent_window.close)

        for b in (min_btn, close_btn):
            b.setStyleSheet(
                "QPushButton { color: white; background: rgba(255,255,255,30); border: none; border-radius: 4px; }"
                "QPushButton:hover { background: rgba(255,255,255,70); }"
            )
            layout.addWidget(b)

        self.setStyleSheet("background: rgba(0,0,0,120);")
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            window_handle = self._parent_window.windowHandle()
            # startSystemMove() delegates the drag to the compositor itself,
            # which works correctly on both X11 and Wayland. Plain
            # self.move(...) silently does nothing on Wayland, since
            # clients aren't allowed to reposition their own windows there.
            if window_handle is not None:
                started = window_handle.startSystemMove()
                if started:
                    event.accept()
                    self._drag_pos = None
                    return
            # Fallback for setups where startSystemMove() isn't available.
            self._drag_pos = event.globalPosition().toPoint() - self._parent_window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._parent_window.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None


class SpotifyWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Spotify (Transparent)")
        self.resize(1000, 700)

        # --- Window-level transparency (real, OS-composited alpha) ---
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window
        )

        central = QWidget()
        central.setStyleSheet("background: transparent;")
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.titlebar = TitleBar(self)
        outer.addWidget(self.titlebar)

        # --- Persistent profile so login survives restarts ---
        os.makedirs(PROFILE_DIR, exist_ok=True)
        self.profile = QWebEngineProfile(APP_NAME, self)
        self.profile.setPersistentStoragePath(PROFILE_DIR)
        self.profile.setCachePath(PROFILE_DIR)
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        # See DESKTOP_CHROME_UA above: without this, Spotify's login page
        # shows a stripped-down QR-code-only login instead of the full
        # password / Google / Facebook / Apple options.
        self.profile.setHttpUserAgent(DESKTOP_CHROME_UA)

        self.profile.scripts().insert(build_inject_script(INJECTED_CSS))

        # --- Ad blocker ---
        self.ad_interceptor = AdBlockInterceptor(self)
        self.profile.setUrlRequestInterceptor(self.ad_interceptor)

        # MainPage (not plain QWebEnginePage) so "Continue with Google/
        # Facebook/Apple" login popups actually open instead of failing
        # silently.
        self.page = MainPage(self.profile, self)
        # Make the web page itself render with a transparent base color,
        # not just the Qt window around it -- this is what lets the
        # injected "transparent" CSS actually show your desktop through,
        # rather than showing Chromium's default white/black canvas.
        self.page.setBackgroundColor(QColor(0, 0, 0, 0))

        self.view = QWebEngineView()
        self.view.setPage(self.page)
        self.view.setStyleSheet("background: transparent;")
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        outer.addWidget(self.view)

        self.page.load(QUrl(SPOTIFY_URL))

        # Simple resize grip in the corner since frameless windows lose
        # the normal OS resize handles too.
        grip = QSizeGrip(self)
        grip.setStyleSheet("background: transparent;")
        outer.addWidget(grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    window = SpotifyWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
