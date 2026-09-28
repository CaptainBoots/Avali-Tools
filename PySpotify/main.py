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
import argparse
import json
import re

DEBUG = os.environ.get("SPOTIFY_DEBUG", "0") == "1"

try:
    from PyQt6.QtCore import Qt, QUrl, QTimer, QThread, pyqtSignal
    from PyQt6.QtGui import QColor, QPainter
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QPushButton, QLabel, QSlider
    )
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import (
        QWebEngineProfile, QWebEngineScript, QWebEnginePage,
        QWebEngineUrlRequestInterceptor, QWebEngineSettings,
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

# Lyrics provider keys (Musixmatch/Vagalume/Genius), saved from the
# panel's settings view. Env vars remain as fallback defaults.
LYRICS_CONFIG_DIR = os.path.expanduser("~/.config/SpotifyTransparent")
LYRICS_KEYS_PATH = os.path.join(LYRICS_CONFIG_DIR, "lyrics_keys.json")

# 0.0 = fully clear, 1.0 = solid black. Override per-run with:
#   SPOTIFY_BG_ALPHA=0.85 ./run.sh
try:
    _BG_ALPHA = max(0.0, min(1.0, float(os.environ.get("SPOTIFY_BG_ALPHA", "0.2"))))
except ValueError:
    _BG_ALPHA = 0.2
# Titlebar has its own knob so it can stay readable while panels are glassy:
#   SPOTIFY_TITLEBAR_ALPHA=0.8 ./run.sh
try:
    _TB_ALPHA = max(0.0, min(1.0, float(os.environ.get("SPOTIFY_TITLEBAR_ALPHA", "0.65"))))
except ValueError:
    _TB_ALPHA = 0.65
_PANEL_BG = f"rgba(12, 12, 14, {_BG_ALPHA})"
_CARD_BG = "rgba(255, 255, 255, 0.08)"
# Native Qt bottom player bar (legacy experiment, off by default):
#   SPOTIFY_NATIVE_BAR=1 ./run.sh
_NATIVE_BAR = os.environ.get("SPOTIFY_NATIVE_BAR", "0") == "1"
# Spicetify ports: AI-artist blocklist (comma list and/or file, one per line):
#   SPOTIFY_BLOCKED_ARTISTS="artist one,artist two"
#   SPOTIFY_BLOCKED_ARTISTS_FILE=~/.config/spotify-blocked-artists.txt
# Video-track auto-skip (Auto Skip Videos port):
#   SPOTIFY_SKIP_VIDEOS=0 ./run.sh  (default on)


def _load_blocked_artists():
    items = []
    for a in os.environ.get("SPOTIFY_BLOCKED_ARTISTS", "").replace("\n", ",").split(","):
        a = a.strip().lower()
        if a:
            items.append(a)
    path = os.environ.get("SPOTIFY_BLOCKED_ARTISTS_FILE", "")
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip().lower()
                    if line and not line.startswith("#"):
                        items.append(line)
        except OSError as exc:
            print(f"[skip] blocked-artists file unreadable: {exc}", flush=True)
    return sorted(set(items))


_BLOCKED_ARTISTS = _load_blocked_artists()
_SKIP_VIDEOS = os.environ.get("SPOTIFY_SKIP_VIDEOS", "1") == "1"

INJECTED_CSS = f"""
:root, html, body {{
    --background-base: transparent !important;
    --background-tinted-base: transparent !important;
    --background-tinted-highlight: transparent !important;
    --background-tinted-press: transparent !important;
    --background-highlight: transparent !important;
    --background-press: transparent !important;
    --background-elevated-base: transparent !important;
    --background-elevated-highlight: transparent !important;
    --background-elevated-press: transparent !important;
    --background-tinted-elevated-base: transparent !important;
    --background-unsafe: transparent !important;
    --background-canvas: transparent !important;
    --backdrop-opacity: 0 !important;
}}
/* Window canvas itself stays clear so compositor blur shows at edges. */
html, body,
body::before, body::after,
#main, #main::before, #main::after,
.Root {{
    background: transparent !important;
    background-color: transparent !important;
    background-image: none !important;
    box-shadow: none !important;
}}
/* Layout panels get the readable translucent fill. */
div[class*="Root__"],
div[class*="main-view"],
div[class*="main-view-container"],
div[class*="scroll-node"],
div[class*="nav-bar"],
div[class*="top-bar"],
div[class*="YourLibrary"],
main, aside, header,
[data-testid="root"],
[data-testid="topbar"],
[data-testid="left-sidebar"],
[data-testid="main-view"],
.encore-layout,
.encore-dark-theme,
.os-host, .os-viewport {{
    background-color: {_PANEL_BG} !important;
    background-image: none !important;
    box-shadow: none !important;
}}
/* Bottom player bar: translucent like the titlebar (same alpha), so it
   blends instead of sitting as an opaque strip. Bumped specifity-free:
   single rule, !important wins ties. */
footer,
[data-testid="now-playing-bar"],
[data-testid*="now-playing"],
[data-testid*="player"],
div[class*="now-playing"],
div[class*="player"],
div[class*="Player"],
div[class*="playbar"],
div[class*="Playback"] {{
    background-color: rgba(10, 10, 12, {_TB_ALPHA:.2f}) !important;
    background-image: none !important;
    box-shadow: none !important;
    flex-shrink: 0 !important;
}}
[data-testid="now-playing-bar"] {{
    position: fixed !important;
    bottom: 0 !important;
    left: 0 !important;
    right: 0 !important;
    z-index: 100 !important;
    margin-top: 0 !important;
}}
/* Marketing footer: a huge slab that serves no purpose in the app --
   hide it, unless it ever wraps the player bar itself (:has guard
   keeps the player safe). */
footer:not(:has([data-testid="now-playing-bar"])) {{
    display: none !important;
}}
/* Inner player cells (track info, controls group) keep stock
   near-black fills through the exclusions above, rendering as darker
   patches against the translucent bar. Clear them all so the bar's own
   fill is the single backdrop. Doubled testid outbids Encore's
   two-class rules; slider track/fills are repainted inline by JS
   (inline !important wins over everything). */
[data-testid="now-playing-bar"][data-testid="now-playing-bar"] div {{
    background-color: transparent !important;
    background-image: none !important;
    box-shadow: none !important;
}}
/* Room for the fixed bar so page content isn't hidden behind it. */
[data-testid="root"] {{
    padding-bottom: 78px !important;
    box-sizing: border-box !important;
}}
/* Player track artwork: force exact stock geometry. An inline <img>
   sits on the text baseline, which reads as "shifted down" in its box;
   block display + fixed 56px (Spotify's player thumb size) fixes it. */
footer a img,
[data-testid="now-playing-bar"] a img,
[data-testid="now-playing-bar"] img {{
    width: 56px !important;
    height: 56px !important;
    min-width: 56px !important;
    min-height: 56px !important;
    aspect-ratio: auto !important;
    object-fit: cover !important;
    display: block !important;
    margin: 0 !important;
    border-radius: 4px !important;
    flex-shrink: 0 !important;
}}
/* Outbid two-class Spotify rules (e.g. cover-art margins): doubled
   attribute counts twice, winning without touching anything else. */
[data-testid="now-playing-bar"][data-testid="now-playing-bar"] img {{
    margin: 0 !important;
}}
/* NOTE: slider fills are painted from paintSliders() in JS, not here:
   the broad transparency rule above carries ~30 :not()s, so any
   stylesheet rule loses the !important specificity war. Inline
   !important via JS outranks everything. */
/* Stock lyrics buttons: hidden since our own LYRICS panel replaces
   them (multi-provider, synced). Our button has no aria-label and a
   py- id, so it survives these rules. */
button[aria-label*="lyric" i]:not(#py-lyrics-btn),
a[aria-label*="lyric" i],
[data-testid*="lyric-button"],
div[role="menuitem"][aria-label*="lyric" i] {{
    display: none !important;
}}
/* Now Playing sidebar showcase: big centered artwork, larger track text.
   Multiple candidate selectors since Spotify renames this panel often. */
aside[aria-label*="Now playing" i],
[data-testid*="now-playing-view"],
[data-testid*="right-sidebar"],
div[class*="now-playing-view"],
div[class*="right-sidebar"] {{
    text-align: center !important;
}}
aside[aria-label*="Now playing" i] img,
[data-testid*="now-playing-view"] img,
[data-testid*="right-sidebar"] img,
div[class*="now-playing-view"] img,
div[class*="right-sidebar"] img {{
    width: min(100%, 320px) !important;
    height: auto !important;
    aspect-ratio: 1 / 1 !important;
    object-fit: cover !important;
    border-radius: 12px !important;
    margin: 12px auto !important;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5) !important;
}}
aside[aria-label*="Now playing" i] h1,
aside[aria-label*="Now playing" i] h2,
[data-testid*="now-playing-view"] h1,
[data-testid*="now-playing-view"] h2,
[data-testid*="right-sidebar"] h1,
[data-testid*="right-sidebar"] h2,
div[class*="now-playing-view"] h1,
div[class*="now-playing-view"] h2,
div[class*="right-sidebar"] h1,
div[class*="right-sidebar"] h2 {{
    font-size: 22px !important;
    text-align: center !important;
}}
/* Let main content shrink (grid AND flex) so the footer survives small
   windows: grid/flex children default to min-height:auto and refuse to
   shrink, pushing the footer out. Only the footer keeps priority. */
main,
[data-testid="main-view"],
div[class*="main-view"],
div[class*="main-view-container"],
div[class*="scroll-node"],
.os-host, .os-viewport,
.encore-layout {{
    min-height: 0 !important;
}}
/* Inner nested containers stay CLEAR -- the single translucent panel
   behind them provides the fill. Giving every nested div its own alpha
   layer stacks (0.65 over 0.65 over 0.65 = near-black), which is why it
   looked solid dark. Panel/slider elements are excluded below. */
.Root div:not([data-testid="root"]):not([data-testid="now-playing-bar"]):not([data-testid*="now-playing"]):not([data-testid*="player"]):not([data-testid="left-sidebar"]):not([data-testid="main-view"]):not([data-testid*="right-sidebar"]):not([data-testid*="progress"]):not([data-testid*="volume"]):not([class*="Root__"]):not([class*="main-view"]):not([class*="nav-bar"]):not([class*="now-playing"]):not([class*="player"]):not([class*="Player"]):not([class*="playbar"]):not([class*="Playback"]):not([class*="progress" i]):not([class*="slider" i]):not([class*="volume" i]):not([class*="right-sidebar"]):not([class*="RightSidebar"]):not([class*="top-bar"]):not([class*="YourLibrary"]):not([class*="card"]):not([class*="Card"]):not([role="slider"]),
.Root section:not([data-testid="now-playing-bar"]):not([data-testid*="now-playing"]):not([data-testid*="player"]):not([class*="now-playing"]):not([class*="player"]):not([class*="Player"]),
.Root article:not([data-testid="now-playing-bar"]):not([data-testid*="player"]),
.Root ul, .Root li {{
    background-color: transparent !important;
    background-image: none !important;
    box-shadow: none !important;
}}
/* Kill gradient ::before/::after backdrops Spotify uses for album-art tint. */
*::before, *::after {{
    background-image: none !important;
}}
/* Keep album art / avatars / icons visible. Cards stay slightly
   translucent for readability. */
img, video, canvas, svg {{
    background: transparent !important;
}}
div[class*="card"],
div[class*="Card"] {{
    background-color: {_CARD_BG} !important;
    background-image: none !important;
}}

/* Visual fallback: hide leftover ad banner containers even if a
   request slips past the network-level ad blocker (e.g. an ad served
   from a domain not yet in AD_HOST_FRAGMENTS). Network blocking above
   is the primary defense; this just hides the empty/broken slot. */
[data-testid="ad-banner"],
[data-testid="leaderboard-ad"],
.ad-banner,
.adsbygoogle {{
    display: none !important;
}}
/* Hide Spotify's "Install App" / "Download Spotify" upsell (top-right
   button + "Download Spotify for Linux" promo card) and Premium begging
   (upgrade buttons, trial banners, Premium badges). Text-based JS
   below catches whatever selectors Spotify renames next. */
[data-testid*="install"],
[data-testid*="download"],
[data-testid*="premium"],
[data-testid*="upgrade"],
[data-testid*="trial"],
a[href*="/download"],
a[href*="/premium"],
a[href*="/upgrade"],
div[class*="install" i],
div[class*="upsell" i],
div[class*="promo" i],
/* Spicetify snippets port: hide Browse / Full screen / Mini player buttons
   (attribute selectors work regardless of renames). */
button[aria-label="Browse" i], a[aria-label="Browse" i],
button[aria-label*="Full screen" i],
button[aria-label*="Miniplayer" i], button[aria-label*="Mini player" i],
a[aria-label*="Miniplayer" i] {{
    display: none !important;
}}
"""

# (Web now-playing footer is shown with a stock solid fill -- see CSS above.)


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
    # Third-party ad-tech / server-side-ad-insertion vendors. These are
    # safe to hard-block (the player never depends on them); first-party
    # Spotify ad endpoints above are kept as-is because blocking more of
    # those trips Spotify's "Playback Paused" anti-adblock loop.
    "springserve.com",
    "freewheel.tv",
    "magnite.com",
    "rubiconproject.com",
    "adsrvr.org",
    "moatads.com",
    "moat.com",
    "doubleverify.com",
    "iasds01.com",
    "mathtag.com",
    "openx.net",
    "spotxchange.com",
]


# Spotify's login page checks the browser's user agent and shows a
# stripped-down login (often just a QR code) if it doesn't look like a
# normal desktop browser -- which QtWebEngine's default UA doesn't.
# Spoofing a real desktop Chrome UA gets the full login page back
# (password field + "Continue with Google/Facebook/Apple" buttons).
#
# NOTE: Chrome/128 is from Aug 2024. accounts.spotify.com does bot/
# fingerprint checks, and a 2-year-old UA mismatched with the real
# QtWebEngine Chromium version is a common cause of generic
# "login failed" / "something went wrong". Override with:
#   SPOTIFY_UA="Mozilla/5.0 ..." ./run.sh --debug
DESKTOP_CHROME_UA = os.environ.get(
    "SPOTIFY_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
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

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        if DEBUG:
            print(
                f"[js:{level}] {source_id}:{line_number}: {message}",
                flush=True,
            )


class AdBlockInterceptor(QWebEngineUrlRequestInterceptor):
    """Blocks network requests to known ad/tracking hosts before they're sent."""

    # Never block auth/login flows even if a fragment matches --
    # a false positive here shows up as a generic "login failed".
    LOGIN_SAFE_SUBSTRS = (
        "accounts.spotify.com",
        "accounts.google.com",
        "appleid.apple.com",
        "facebook.com",
        "challenge",
        "recaptcha",
        "open.spotify.com/login",
    )

    def interceptRequest(self, info):
        url = info.requestUrl().toString().lower()
        for fragment in AD_HOST_FRAGMENTS:
            if fragment in url:
                if any(s in url for s in self.LOGIN_SAFE_SUBSTRS):
                    if DEBUG:
                        print(f"[adblock] ALLOW (login-safe): {url}", flush=True)
                    return
                if DEBUG:
                    print(f"[adblock] BLOCK: {url}", flush=True)
                info.block(True)
                return


def build_inject_script(css: str) -> QWebEngineScript:
    """Wrap the CSS string in a small JS snippet that injects a <style> tag,
    and set it to run at document creation *and* re-run on navigation so it
    survives Spotify's internal client-side page changes (it's a single-page app)."""
    # NOTE: document.head may not exist at DocumentCreation, and Spotify
    # (SPA) replaces <head> / body classes on navigation, so: try early,
    # retry on DOMContentLoaded, re-apply via MutationObserver + interval.
    js = f"""
    (function() {{
        var id = 'transparent-spotify-style';
        var cssText = `{css}`;
        var DEBUGJS = {'true' if DEBUG else 'false'};
        // Spicetify ports (baked at launch).
        var SKIP_VIDEOS = {'true' if _SKIP_VIDEOS else 'false'};
        var BLOCKED_ARTISTS = {json.dumps(_BLOCKED_ARTISTS)};
        var _skipCool = 0;
        function injectCSS() {{
            try {{
                var el = document.getElementById(id);
                if (!el) {{
                    el = document.createElement('style');
                    el.id = id;
                    (document.head || document.documentElement).appendChild(el);
                }}
                if (el.textContent !== cssText) el.textContent = cssText;
            }} catch (e) {{}}
        }}
        // Hide upsell / premium-begging by visible text. Selector CSS
        // above catches known containers; this catches renames (runs on
        // the same timers as injectCSS, cheap scan).
        // Also ports the Spicetify snippets: home-section kill list
        // (Made For You, Popular shelves) hides the whole shelf section.
        var snippetSections = ['made for you', 'popular radio',
            'popular albums', 'popular new releases', '#spotifywrapped',
            'getting started'];
        function hideUpsells() {{
            try {{
                var nodes = document.querySelectorAll('button, a, h1, h2, h3, h4, span, p');
                for (var i = 0; i < nodes.length; i++) {{
                    if (nodes[i].id === 'py-lyrics-btn') continue;  // ours
                    var t = (nodes[i].innerText || '').trim();
                    if (!t || t.length > 60) continue;
                    var low = t.toLowerCase();
                    if (t === 'Install App' || t === 'Download the free app' ||
                        t === 'Browse' ||
                        low === 'go premium' || low === 'get premium' ||
                        low === 'try premium free' || low === 'start a free trial' ||
                        low === 'get 3 months free' || low.indexOf('premium free') !== -1 ||
                        low === 'lyrics' || low === 'show lyrics') {{
                        var holder = nodes[i].closest('button, a');
                        if (holder) holder.style.display = 'none';
                    }} else if (t.indexOf('Download Spotify for Linux') !== -1) {{
                        var card = nodes[i].closest('section, aside, div');
                        if (card) card.style.display = 'none';
                        else nodes[i].style.display = 'none';
                    }} else if ((nodes[i].tagName === 'H1' || nodes[i].tagName === 'H2') &&
                               snippetSections.indexOf(low) !== -1) {{
                        var shelf = nodes[i].closest('section') ||
                                    nodes[i].closest('div[class]') || nodes[i];
                        shelf.style.display = 'none';
                    }}
                }}
            }} catch (e) {{}}
        }}
                // Free-tier unlock attempt: shuffle/repeat/loop buttons get
        // disabled/aria-disabled by Spotify without Premium. Strip the
        // flags so clicks go through. If Spotify enforces server-side the
        // state still won't stick -- the dump shows which case it is.
        var _ctlSig = '';
        function unblockControls() {{
            try {{
                var btns = document.querySelectorAll('[data-testid*="shuffle"],[data-testid*="repeat"],[data-testid*="loop"],button[aria-label*="huffle" i],button[aria-label*="epeat" i],button[aria-label*="oop" i]');
                var sig = '';
                btns.forEach(b => {{
                    sig += (b.getAttribute('data-testid') || b.getAttribute('aria-label') || '?') +
                           ':' + !!b.disabled + ':' + (b.getAttribute('aria-disabled') || '') + ';';
                    if (b.disabled) b.disabled = false;
                    if (b.getAttribute('aria-disabled') === 'true') b.setAttribute('aria-disabled', 'false');
                    b.style.pointerEvents = '';
                }});
                if (sig !== _ctlSig) {{ _ctlSig = sig;
                    if (DEBUGJS) console.log('[controls] ' + (sig || 'none found')); }}
            }} catch (e) {{}}
        }}
        // Spicetify ports: AI-artist + video-track auto-skip.
        // Canvas loops are muted/short, so the guards only catch real video.
        function applyAll() {{ injectCSS(); hideUpsells(); maybeSkip(); unblockControls(); paintSliders(); }}
        applyAll();
        function skipTrack(reason) {{
            try {{
                var b = document.querySelector('[data-testid="control-button-skip-forward"]') ||
                        document.querySelector('[data-testid*="skip-forward"]');
                if (b) {{ b.click(); _skipCool = Date.now() + 5000;
                    if (DEBUGJS) console.log('[skip] ' + reason); }}
            }} catch (e) {{}}
        }}
        function maybeSkip() {{
            try {{
                if (Date.now() < _skipCool) return;
                if (BLOCKED_ARTISTS.length) {{
                    var m = navigator.mediaSession && navigator.mediaSession.metadata;
                    var ar = ((m && m.artist) || '').toLowerCase();
                    if (ar) for (var i = 0; i < BLOCKED_ARTISTS.length; i++) {{
                        if (BLOCKED_ARTISTS[i] && ar.indexOf(BLOCKED_ARTISTS[i]) !== -1) {{
                            skipTrack('blocked artist: ' + ar); return; }}
                    }}
                }}
                if (SKIP_VIDEOS) {{
                    var vs = document.querySelectorAll('video');
                    for (var j = 0; j < vs.length; j++) {{ var v = vs[j];
                        if (!v.paused && !v.muted && isFinite(v.duration) &&
                            v.duration > 60 && v.currentTime > 1) {{
                            skipTrack('video track'); return; }} }}
                }}
            }} catch (e) {{}}
        }}
        // Slider fills: the broad transparency rule above clears
        // backgrounds on plain inner divs, wiping the white elapsed
        // fills and tracks of the playback + volume bars. Repaint them
        // via inline !important, which outranks even the ~30-:not()
        // broad rule (no stylesheet rule can win that specificity war).
        // Layout wrappers are FULL-WIDTH boxes -- painting them would
        // turn the whole bar white -- so only the width-carrying
        // descendants get paint: direct children with their own style
        // or testid (tooltip follower, handle) plus everything nested
        // deeper than the wrappers. Geometry untouched, colors only.
        var _sldLog = 0;
        function paintSliders() {{
            try {{
                var painted = false;
                var pb = document.querySelector(
                    '[data-testid="progress-bar-background"]');
                if (pb) {{
                    pb.style.setProperty('background-color',
                        'rgba(255,255,255,0.28)', 'important');
                    painted = paintFillTree(pb) || painted;
                }}
                var vb = document.querySelector('[data-testid="volume-bar"]');
                if (vb) {{
                    // Outer div is a 12px-tall invisible hit area, NOT the
                    // visible track: keep it transparent or it renders as
                    // a fat extra bar. The real 4px track is the nested
                    // background; paintNestedBar handles it.
                    var track = null;
                    var kids = vb.children;
                    for (var k = 0; k < kids.length; k++)
                        if (!track && kids[k].tagName === 'DIV') track = kids[k];
                    if (track) {{
                        track.style.setProperty('background-color',
                            'transparent', 'important');
                        var nested = track.querySelector(
                            '[data-testid="progress-bar"]');
                        if (nested) {{ paintNestedBar(nested); painted = true; }}
                    }}
                }}
                if (painted) {{
                    // Spotify re-renders bar markup on track change,
                    // wiping inline styles -- the 500ms tick restores them.
                    if (DEBUGJS && Date.now() - _sldLog > 30000) {{
                        _sldLog = Date.now();
                        console.log('[sliders] fill painted');
                    }}
                }}
            }} catch (e) {{}}
        }}
        function paintFillTree(track) {{
            try {{
                track.style.setProperty('background-color',
                    'rgba(255,255,255,0.28)', 'important');
                paintFillChildren(track);
            }} catch (e) {{}}
            return true;
        }}
        function paintFillChildren(container) {{
            var any = false;
            try {{
                var kids = container.children;
                for (var i = 0; i < kids.length; i++) {{
                    var c = kids[i];
                    if (c.tagName !== 'DIV') continue;
                    var st = c.getAttribute('style') || '';
                    var tid = c.getAttribute('data-testid') || '';
                    if (tid || st.indexOf('left') !== -1) {{
                        c.style.setProperty(
                            'background-color', '#ffffff', 'important');
                        any = true;
                    }} else if (c.matches('[data-testid="progress-bar"]')) {{
                        paintNestedBar(c); any = true;
                    }} else {{
                        var nested = c.querySelector(
                            '[data-testid="progress-bar"]');
                        if (nested) {{ paintNestedBar(nested); any = true; continue; }}
                        var inner = c.querySelectorAll('div');
                        for (var j = 0; j < inner.length; j++)
                            inner[j].style.setProperty(
                                'background-color', '#ffffff', 'important');
                        if (inner.length) any = true;
                    }}
                }}
            }} catch (e) {{}}
            return any;
        }}
        // Volume is a progress-bar nested inside the volume hit area:
        // nested shell stays transparent, the nested 4px background is
        // the visible grey track, and its children get the same
        // wrapper-aware treatment as the main bar.
        function paintNestedBar(nested) {{
            try {{
                nested.style.setProperty(
                    'background-color', 'transparent', 'important');
                var nbg = nested.querySelector(
                    '[data-testid="progress-bar-background"]');
                if (!nbg) return;
                nbg.style.setProperty('background-color',
                    'rgba(255,255,255,0.28)', 'important');
                paintFillChildren(nbg);
            }} catch (e) {{}}
        }}
        // One-shot slider diagnostic: dumps the real progress/volume
        // markup so fills can be targeted exactly (log via --debug).
        function probeSliders() {{
            try {{
                var r = document.querySelector('[data-testid="progress-bar"]');
                console.log('[sliderprobe] progress found=' + !!r);
                if (r) {{
                    console.log('[sliderprobe] html=' + r.outerHTML.slice(0, 2200));
                    var divs = r.querySelectorAll('div');
                    var rootW = r.getBoundingClientRect().width;
                    for (var i = 0; i < divs.length && i < 10; i++) {{
                        var cs = getComputedStyle(divs[i]);
                        var dr = divs[i].getBoundingClientRect();
                        console.log('[sliderprobe] div' + i + ' bg=' + cs.backgroundColor +
                            ' w=' + Math.round(dr.width) + '/' + Math.round(rootW) +
                            ' h=' + Math.round(dr.height) + ' ov=' + cs.overflow +
                            ' op=' + cs.opacity +
                            ' style=' + (divs[i].getAttribute('style') || '(none)').slice(0, 220));
                    }}
                }}
                var v = document.querySelector('[data-testid*="volume"]');
                console.log('[sliderprobe] volume found=' + !!v +
                    (v ? ' html=' + v.outerHTML.slice(0, 3200) : ''));
                if (v) {{
                    var vv = v.querySelectorAll('div');
                    var vw = v.getBoundingClientRect().width;
                    for (var k = 0; k < vv.length && k < 8; k++) {{
                        var vc = getComputedStyle(vv[k]);
                        var vr = vv[k].getBoundingClientRect();
                        console.log('[sliderprobe] voldiv' + k + ' bg=' + vc.backgroundColor +
                            ' w=' + Math.round(vr.width) + '/' + Math.round(vw) +
                            ' h=' + Math.round(vr.height) + ' ov=' + vc.overflow +
                            ' op=' + vc.opacity +
                            ' style=' + (vv[k].getAttribute('style') || '(none)').slice(0, 200));
                    }}
                }}
            }} catch (e) {{ console.log('[sliderprobe] err ' + e); }}
        }}
        if (DEBUGJS) setTimeout(probeSliders, 10000);
        // Strip identifier: what's actually painted below the player
        // bar (button rect + bar flex + elementFromPoint rows).
        function probeStrip() {{
            try {{
                var b = document.getElementById('py-lyrics-btn');
                if (b) {{
                    var r = b.getBoundingClientRect();
                    console.log('[stripprobe] lyrics-btn rect x=' + Math.round(r.x) +
                        ' y=' + Math.round(r.y) + ' w=' + Math.round(r.width) +
                        ' h=' + Math.round(r.height));
                }} else console.log('[stripprobe] lyrics-btn missing');
                var np = document.querySelector('[data-testid="now-playing-bar"]');
                if (np) {{
                    var cs = getComputedStyle(np);
                    console.log('[stripprobe] bar flexwrap=' + cs.flexWrap +
                        ' align=' + cs.alignItems + ' overflow=' + cs.overflow);
                }}
                var vh = window.innerHeight;
                // Identify every BUTTON inside the player bar by id/rect,
                // plus the full ancestor chain of the bottom-most one.
                try {{
                    var np2 = document.querySelector(
                        '[data-testid="now-playing-bar"]');
                    // Direct children of the bar: finds the mystery row.
                    var ch = np2 ? np2.children : [];
                    for (var ci = 0; ci < ch.length; ci++) {{
                        var cr = ch[ci].getBoundingClientRect();
                        var cbg = '';
                        try {{ cbg = getComputedStyle(ch[ci]).backgroundColor; }}
                        catch (e) {{}}
                        console.log('[stripprobe] row' + ci + ' tag=' +
                            ch[ci].tagName + ' tid=' +
                            (ch[ci].getAttribute('data-testid') || '') +
                            ' cls=' + String(ch[ci].className || '').slice(0, 50) +
                            ' rect=' + Math.round(cr.x) + ',' +
                            Math.round(cr.y) + ',' + Math.round(cr.width) +
                            'x' + Math.round(cr.height) + ' bg=' + cbg);
                    }}
                    var btns = np2 ? np2.querySelectorAll('button') : [];
                    for (var bi = 0; bi < btns.length; bi++) {{
                        var br = btns[bi].getBoundingClientRect();
                        console.log('[stripprobe] btn' + bi + ' id=' +
                            (btns[bi].id || '(none)') + ' label=' +
                            ((btns[bi].getAttribute('aria-label') || '') +
                             '|' + (btns[bi].innerText || '').trim()).slice(0, 40) +
                            ' rect=' + Math.round(br.x) + ',' +
                            Math.round(br.y) + ',' + Math.round(br.width) +
                            'x' + Math.round(br.height));
                    }}
                    var yb = vh - 8;
                    var deep = document.elementFromPoint(
                        Math.round(window.innerWidth / 2), yb);
                    var chain = [];
                    var p = deep;
                    while (p && chain.length < 6) {{
                        chain.push(p.tagName +
                            (p.id ? '#' + p.id : '') +
                            (p.getAttribute('data-testid') ?
                                '[tid=' + p.getAttribute('data-testid') + ']' : ''));
                        p = p.parentElement;
                    }}
                    console.log('[stripprobe] bottom chain: ' +
                                chain.join(' < '));
                }} catch (e) {{ console.log('[stripprobe] btnerr ' + e); }}
            }} catch (e) {{ console.log('[stripprobe] err ' + e); }}
        }}
        if (DEBUGJS) setTimeout(probeStrip, 11000);
        // SpicyTracker port: strip ?si= tracking from Spotify share links,
        // both copy-event and clipboard-API paths.
        var _copyHooked = false;
        function hookCopyCleaner() {{
            if (_copyHooked) return; _copyHooked = true;
            function cleanLink(link) {{
                try {{ var u = new URL(link);
                    if (u.hostname.indexOf('spotify.com') !== -1) {{
                        u.searchParams.delete('si'); return u.toString(); }} }}
                catch (x) {{}}
                return link;
            }}
            try {{
                document.addEventListener('copy', function(e) {{
                    try {{
                        if (!e.clipboardData) return;
                        var t = (window.getSelection() || '').toString();
                        if (!t || t.indexOf('open.spotify.com') === -1 ||
                            t.indexOf('si=') === -1) return;
                        var out = t.replace(/https?:\\/\\/open\\.spotify\\.com[^\\s]*/g, cleanLink);
                        if (out !== t) {{ e.clipboardData.setData('text/plain', out);
                            e.preventDefault();
                            if (DEBUGJS) console.log('[tracker] cleaned share link'); }}
                    }} catch (x) {{}}
                }});
            }} catch (e) {{}}
            try {{
                if (navigator.clipboard && navigator.clipboard.writeText) {{
                    var _write = navigator.clipboard.writeText.bind(navigator.clipboard);
                    navigator.clipboard.writeText = function(txt) {{
                        try {{
                            if (typeof txt === 'string' &&
                                txt.indexOf('open.spotify.com') !== -1 &&
                                txt.indexOf('si=') !== -1)
                                txt = txt.replace(/https?:\\/\\/open\\.spotify\\.com[^\\s]*/g, cleanLink);
                        }} catch (x) {{}}
                        return _write(txt);
                    }};
                }}
            }} catch (e) {{}}
        }}
        hookCopyCleaner();
        document.addEventListener('DOMContentLoaded', applyAll);
        window.addEventListener('load', applyAll);
        // Spotify is a single-page app; re-apply in case it replaces
        // <head> content or the style gets stripped.
        setInterval(applyAll, 2000);
        // Sliders move continuously; repaint fills on their own faster
        // tick so progress motion looks smooth.
        setInterval(paintSliders, 500);
        // documentElement may not exist yet at DocumentCreation --
        // retry until we can observe it.
        function armObserver() {{
            try {{
                if (document.documentElement) {{
                    new MutationObserver(applyAll).observe(
                        document.documentElement,
                        {{childList: true, subtree: true}}
                    );
                    if (DEBUGJS) setTimeout(probePlayerBar, 6000);
                    return;
                }}
            }} catch (e) {{}}
            setTimeout(armObserver, 500);
        }}
        // One-shot diagnostic: report what the bottom player bar actually
        // is (selector + computed background), surfaces via --debug logs.
        function probePlayerBar() {{
            try {{
                console.log('[probe] styleTag=' + !!document.getElementById(id));
                var sels = ['footer', '[data-testid="now-playing-bar"]',
                    '[data-testid*="player"]', 'div[class*="PlayerBar"]',
                    'div[class*="now-playing"]', 'div[id*="player"]'];
                for (var i = 0; i < sels.length; i++) {{
                    var n = document.querySelectorAll(sels[i]).length;
                    console.log('[probe] ' + sels[i] + ' count=' + n);
                }}
                var f = document.querySelector('footer');
                if (f) console.log('[probe] footer bg=' +
                    getComputedStyle(f).backgroundColor +
                    ' h=' + f.getBoundingClientRect().height);
                var np = document.querySelector('[data-testid="now-playing-bar"]');
                if (np) console.log('[probe] now-playing-bar bg=' +
                    getComputedStyle(np).backgroundColor +
                    ' h=' + np.getBoundingClientRect().height);
                var med = document.querySelectorAll('audio,video');
                console.log('[probe] media count=' + med.length);
                for (var j = 0; j < med.length; j++) {{
                    var m = med[j];
                    var seekLen = (m.seekable && m.seekable.length) || 0;
                    console.log('[probe] media[' + j + '] tag=' + m.tagName +
                        ' paused=' + m.paused + ' cur=' + m.currentTime +
                        ' dur=' + m.duration + ' seekable=' + seekLen +
                        ' vol=' + m.volume + ' muted=' + m.muted);
                }}
            }} catch (e) {{ console.log('[probe] err ' + e); }}
        }}
        armObserver();
    }})();
    """
    script = QWebEngineScript()
    script.setName("inject-transparency-css")
    script.setSourceCode(js)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setRunsOnSubFrames(False)
    script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
    return script


def build_adskip_script() -> QWebEngineScript:
    """Page-level ad skipper, runs in MainWorld (page context).

    Why this exists: Spotify moved to server-side ad insertion, so audio
    ads are stitched into the same stream as music -- no domain blocklist
    can catch them (and hard-blocking first-party endpoints just triggers
    "Playback Paused" anti-adblock loops). Instead this does what
    maintained web-player ad-skippers do:

    1. Find the player's ListPlayer class through the webpack chunk queue
       (window.webpackChunkclient_web) and hook it to capture the live
       player instance. When the current track's contentType is 'ad',
       skip it via next('trackdone') -- the ad handshake completes, so
       no freeze loop.
    2. Fallback: mute every <audio>/<video> element while an ad is
       suspected (now-playing link contains ':ad:' or an
       'Advertisement' label is visible), and click the skip-forward
       button if one is enabled.

    Must run in MainWorld: the webpack globals live in page context and
    are invisible from ApplicationWorld's isolated world.
    """
    js = f"""
    (function() {{
        if (window.__pyAdskip) return; window.__pyAdskip = true;
        var DEBUGJS = {'true' if DEBUG else 'false'};
        var _lp = null;          // captured ListPlayer instance
        var _lpClass = null;     // ListPlayer class once resolved
        var _tries = 0;
        var _mutedByUs = false;
        var _lastLog = 0;
        function log(m) {{
            if (!DEBUGJS) return;
            var n = Date.now();
            if (n - _lastLog > 5000) {{ _lastLog = n; console.log('[adskip] ' + m); }}
        }}
        function queue() {{
            try {{
                return window.webpackChunkclient_web || window.rspackChunkclient_web ||
                       window.rspackChunk || null;
            }} catch (e) {{ return null; }}
        }}
        function getReq() {{
            try {{
                var q = queue();
                if (!q || !q.push) return null;
                var r = q.push([[Symbol()], {{}}, function(x) {{ return x; }}]);
                if (r && r.m) return r;
                // Some builds: push returns length, require fn hangs off queue.
                if (q.require && q.require.m) return q.require;
                return null;
            }} catch (e) {{ return null; }}
        }}
        function findListPlayer(req) {{
            try {{
                var ids = Object.keys(req.m || {{}});
                for (var i = 0; i < ids.length; i++) {{
                    var fn;
                    try {{ fn = req.m[ids[i]]; }} catch (x) {{ continue; }}
                    var src = '';
                    try {{ src = Function.prototype.toString.call(fn); }} catch (x) {{ continue; }}
                    if (src.indexOf('allowSeeking') === -1 ||
                        src.indexOf('_loadedList') === -1 ||
                        src.indexOf('LIST_PLAYER_NO_LIST') === -1) continue;
                    var mod;
                    try {{ mod = req(ids[i]); }} catch (x) {{ continue; }}
                    var vals = [];
                    try {{ vals = Object.values(mod); }} catch (x) {{ continue; }}
                    for (var j = 0; j < vals.length; j++) {{
                        var v = vals[j];
                        if (typeof v === 'function' && v.prototype &&
                            typeof v.prototype.seek === 'function' &&
                            typeof v.prototype.load === 'function' &&
                            typeof v.prototype._getTrackPlayer === 'function')
                            return v;
                    }}
                }}
            }} catch (e) {{}}
            return null;
        }}
        function hookPlayer() {{
            if (_lpClass) return true;
            // No give-up: the tick throttles calls to ~every 2s, so this
            // just keeps trying across player-bundle reloads.
            _tries++;
            if (_tries % 30 === 1) log('ListPlayer hook attempt ' + _tries);
            var req = getReq();
            if (!req) return false;
            var cls = findListPlayer(req);
            if (!cls) return false;
            _lpClass = cls;
            try {{
                var origLoad = cls.prototype.load;
                cls.prototype.load = function(list) {{
                    try {{ _lp = this; }} catch (e) {{}}
                    // Pre-mute EVERY load at full strength (elements +
                    // volume slider to zero): element muting alone is a
                    // proven no-op in this player, so the slider is the
                    // only mute that bites. The tick restores volume
                    // within ~100ms on positive music ID -- a dip no
                    // longer than a crossfade.
                    try {{ setMediaMuted(true, true); }} catch (e) {{}}
                    return origLoad.apply(this, arguments);
                }};
                // Player may already exist (late hook): grab via load hook
                // won't fire, so also accept an already-playing instance
                // discovered through _loadedList polling below.
                log('ListPlayer hooked');
            }} catch (e) {{ _lpClass = null; return false; }}
            return true;
        }}
        function currentTrack() {{
            try {{ return (_lp && _lp._currentTrack) || null; }} catch (e) {{ return null; }}
        }}
        function isAdTrack() {{
            var t = currentTrack();
            if (!t) return false;
            try {{
                if (t.contentType === 'ad') return true;
                var uri = t.uri || '';
                if (uri.indexOf('spotify:ad:') !== -1 || uri.indexOf('spotify:canvas:') === -1 &&
                    uri.indexOf(':ad:') !== -1) return true;
            }} catch (e) {{}}
            return false;
        }}
        function domAdSuspect() {{
            // Fallback signal when the webpack hook hasn't captured the
            // player yet: ad links carry ':ad:' URIs and the UI shows an
            // 'Advertisement' label in the now-playing area.
            try {{
                var np = document.querySelector('[data-testid="now-playing-bar"]');
                if (!np) return false;
                if (np.querySelector('a[href*=":ad:"]')) return true;
                var txt = (np.innerText || '').toLowerCase();
                if (txt.indexOf('advertisement') !== -1) return true;
            }} catch (e) {{}}
            return false;
        }}
        // Stitched/in-episode ads (podcast host-reads, BBC-style dynamic
        // inserts): baked into the episode stream, so no track change, no
        // contentType, no load event -- the track-level skipper is blind.
        // The player UI still badges them. MUTE ONLY here: skipping would
        // nuke the whole episode.
        function stitchedSignals() {{
            try {{
                var zones = document.querySelectorAll(
                    '[data-testid="now-playing-bar"],' +
                    '[data-testid*="player"],[data-testid*="episode"]');
                for (var i = 0; i < zones.length; i++) {{
                    if (zones[i].querySelector('a[href*=":ad:"]')) return true;
                    var txt = (zones[i].innerText || '').toLowerCase();
                    if (txt.indexOf('advertisement') !== -1) return true;
                }}
            }} catch (e) {{}}
            return false;
        }}
        function setMediaMuted(muted, full) {{
            // full=true also zeroes the volume slider (React-driven) and
            // restores it on unmute. Media-only mode just flags elements.
            try {{
                var els = allMedia();
                for (var i = 0; i < els.length; i++) {{
                    try {{
                        if (els[i].muted !== muted) els[i].muted = muted;
                    }} catch (x) {{}}
                }}
                var vi = document.querySelector(
                    '[data-testid="volume-bar"] input[type="range"]');
                if (vi && full) {{
                    if (muted) {{
                        if (_savedVol === null || _savedVol === undefined)
                            _savedVol = vi.value;
                        if (String(vi.value) !== '0') {{
                            vi.value = 0;
                            fireInput(vi);
                        }}
                    }} else if (_savedVol !== null &&
                               _savedVol !== undefined) {{
                        vi.value = _savedVol;
                        fireInput(vi);
                        _savedVol = null;
                    }}
                }}
                _mutedByUs = muted;
            }} catch (e) {{}}
        }}
        // Media elements incl. shadow DOM: the player keeps its <audio>
        // out of the light DOM (media count=0 there), which is why the
        // old light-DOM-only mute was a silent no-op.
        var _mediaCache = null, _mediaCacheT = 0;
        function allMedia() {{
            try {{
                if (_mediaCache && Date.now() - _mediaCacheT < 10000)
                    return _mediaCache;
                var out = [];
                function scan(root) {{
                    try {{
                        var els = root.querySelectorAll('audio,video');
                        for (var i = 0; i < els.length; i++) out.push(els[i]);
                        var all = root.querySelectorAll('*');
                        for (var j = 0; j < all.length; j++) {{
                            if (all[j].shadowRoot) scan(all[j].shadowRoot);
                            if (j > 4000) break;  // sanity cap
                        }}
                    }} catch (e) {{}}
                }}
                scan(document);
                _mediaCache = out; _mediaCacheT = Date.now();
                return out;
            }} catch (e) {{ return []; }}
        }}
        function fireInput(el) {{
            try {{ el.dispatchEvent(new Event('input', {{bubbles: true}})); }}
            catch (e) {{}}
            try {{ el.dispatchEvent(new Event('change', {{bubbles: true}})); }}
            catch (e) {{}}
        }}
        var _savedVol = null;
        function clickSkip() {{
            try {{
                var b = document.querySelector('[data-testid="control-button-skip-forward"]') ||
                        document.querySelector('[data-testid*="skip-forward"]');
                if (b && !b.disabled && b.getAttribute('aria-disabled') !== 'true') {{
                    b.click(); log('clicked skip-forward for ad'); return true;
                }}
            }} catch (e) {{}}
            return false;
        }}
        function setUiLock(on) {{
            try {{
                var ov = document.getElementById('py-ui-lock');
                if (on) {{
                    if (!ov) {{
                        ov = document.createElement('div');
                        ov.id = 'py-ui-lock';
                        ov.style.cssText = 'position:fixed;left:0;right:0;' +
                            'bottom:0;height:120px;z-index:150;' +
                            'background:rgba(10,10,12,0.45);display:flex;' +
                            'align-items:center;justify-content:center;' +
                            'color:#fff;font-weight:bold;font-size:14px;' +
                            'cursor:not-allowed;';
                        ov.textContent = 'Recording lyrics — controls locked';
                        ov.addEventListener('click', function(e) {{
                            try {{ e.stopPropagation(); e.preventDefault(); }}
                            catch (x) {{}}
                        }}, true);
                        (document.body || document.documentElement)
                            .appendChild(ov);
                    }}
                    ov.style.display = 'flex';
                }} else if (ov) {{
                    ov.style.display = 'none';
                }}
            }} catch (e) {{}}
        }}
        var _skipCool = 0;
        var _hookTick = 0;
        setInterval(function() {{
            try {{
                // Retry the hook forever (throttled: full webpack scan
                // every ~2s, not every tick) so a late-loading player
                // bundle still gets captured instead of giving up.
                if (!_lpClass && (++_hookTick % 20 === 1)) hookPlayer();
                // Seek bridge (live-record transcription): Python asks
                // for a restart-to-0 via window.__pySeekReq. Verified
                // loop, not fire-and-forget: position is read back every
                // tick and methods retried until it lands < 3% (8s cap).
                // Method: skip-back button when far in (stock Spotify
                // restarts the track past ~3s), player API + Home key
                // otherwise. Plain clicks only -- synthetic pointer
                // events caused drag-capture following the real cursor.
                try {{
                    var sq = window.__pySeekReq;
                    if (sq) {{
                        window.__pySeekReq = null;
                        window.__pySeekWant = {{ms: sq.ms || 0,
                            until: Date.now() + 8000, cool: 0}};
                    }}
                }} catch (e) {{}}
                try {{
                    var sw = window.__pySeekWant;
                    if (sw) {{
                        if (Date.now() > sw.until) {{
                            window.__pySeekWant = null;
                            log('seek gave up');
                        }} else {{
                            var pos = readPosFrac();
                            if (pos !== null && pos < 3) {{
                                window.__pySeekWant = null;
                                log('seek ok at ' + pos + '%');
                            }} else if (Date.now() > sw.cool) {{
                                var hasSeek = false;
                                try {{ hasSeek = !!(_lp && _lp.seek); }}
                                catch (e) {{}}
                                var sbFound = false, barFound = false;
                                try {{
                                    sbFound = !!(document.querySelector(
                                        '[data-testid="control-button-skip-back"]') ||
                                                 document.querySelector(
                                        '[data-testid*="skip-back"]'));
                                    barFound = !!(document.querySelector(
                                        '[data-testid="progress-bar-background"]'));
                                }} catch (e) {{}}
                                if (!sw.dbgT || Date.now() - sw.dbgT > 2000) {{
                                    sw.dbgT = Date.now();
                                    log('seek try pos=' + pos + '% player=' +
                                        (!!_lp) + ' seekFn=' + hasSeek +
                                        ' skipback=' + sbFound +
                                        ' bar=' + barFound);
                                }}
                                    try {{
                                        var pr = _lp.seek(sw.ms || 0);
                                        if (pr && pr.catch)
                                            pr.catch(function() {{}});
                                    }} catch (e) {{}}
                                if (pos !== null && pos > 5) {{
                                    try {{
                                        var sb = document.querySelector(
                                            '[data-testid="control-button-skip-back"]') ||
                                                 document.querySelector(
                                            '[data-testid*="skip-back"]');
                                        if (sb) {{ sb.click(); sw.cool = Date.now() + 2500; }}
                                    }} catch (e) {{}}
                                }} else {{
                                    try {{
                                        var bar = document.querySelector(
                                            '[data-testid="progress-bar-background"]');
                                        if (bar) {{
                                            try {{ bar.focus(); }} catch (e) {{}}
                                            bar.dispatchEvent(new KeyboardEvent(
                                                'keydown', {{key: 'Home',
                                                            code: 'Home',
                                                            bubbles: true,
                                                            cancelable: true}}));
                                        }}
                                    }} catch (e) {{}}
                                }}
                            }}
                        }}
                    }}
                }} catch (e) {{}}
                function readPosFrac() {{
                    try {{
                        var b = document.querySelector(
                            '[data-testid="progress-bar"]');
                        var st = b ? (b.getAttribute('style') || '') : '';
                        var m = st.match(/--progress-bar-transform:\\s*([\\d.]+)%/);
                        return m ? parseFloat(m[1]) : null;
                    }} catch (e) {{ return null; }}
                }}
                // Play bridge: start playback if paused (recording or
                // anything else that needs audio actually moving).
                try {{
                    if (window.__pyPlayReq) {{
                        window.__pyPlayReq = null;
                        var pb = document.querySelector(
                            '[data-testid="play-button"]') ||
                                 document.querySelector(
                            '[data-testid*="playpause"]') ||
                                 document.querySelector(
                            '[data-testid="control-button-play"]');
                        if (pb) {{
                            var pl = (pb.getAttribute('aria-label') || '')
                                     .toLowerCase();
                            if (pl.indexOf('pause') === -1) {{
                                pb.click(); log('play bridge: resumed');
                            }}
                        }}
                    }}
                }} catch (e) {{}}
                // UI lock overlay for live recording: covers the player
                // bar so seeks/skips can't ruin the take.
                try {{
                    if (window.__pyUiLock !== undefined)
                        setUiLock(!!window.__pyUiLock);
                }} catch (e) {{}}
                var ad = isAdTrack() || (!_lp && domAdSuspect());
                var stitched = !ad && stitchedSignals();
                if (ad) {{
                    // Primary: player-level skip, completes the ad handshake.
                    if (_lp) {{
                        try {{
                            if (Date.now() > _skipCool) {{
                                _skipCool = Date.now() + 4000;
                                var p = _lp.next('trackdone');
                                if (p && p.catch) p.catch(function() {{}});
                                log('skipped ad via player.next()');
                            }}
                        }} catch (e) {{ clickSkip(); }}
                    }} else {{
                        clickSkip();
                    }}
                    if (!_mutedByUs) {{ log('muted ad media'); }}
                    setMediaMuted(true, true);
                }} else if (stitched) {{
                    // In-episode ad (podcast dynamic insert): the UI is
                    // badged but the track is the episode itself -- mute
                    // fully, NEVER skip (that would kill the episode).
                    // Unmute is handled by the branch below once badges
                    // clear (allowed without a track object when the
                    // hook is absent, so mute can't stick).
                    if (!_mutedByUs) {{ log('muted stitched ad'); }}
                    setMediaMuted(true, true);
                }} else if (_mutedByUs && !isAdTrack() &&
                           (currentTrack() || !_lp)) {{
                    // Unmute ONLY on positive music ID (a track object
                    // that isn't an ad) -- never on "unknown", so a
                    // pre-muted load can't unmute before the track
                    // resolves. When nothing plays, staying muted is
                    // harmless (no audio to mute).
                    setMediaMuted(false, true); log('unmuted (music back)');
                }}
            }} catch (e) {{}}
        }}, 100);
        log('adskip armed (MainWorld)');
        // Clipboard bridge: QtWebEngine's async clipboard can silently
        // swallow programmatic copies (Share -> Copy link). Stash every
        // copy where Python can see it; the Python side mirrors it onto
        // the real system clipboard (this also keeps ?si= stripping even
        // when Chromium's own clipboard path is the broken half).
        try {{
            if (navigator.clipboard && navigator.clipboard.writeText) {{
                var _cwt = navigator.clipboard.writeText.bind(navigator.clipboard);
                navigator.clipboard.writeText = function(txt) {{
                    try {{ window.__pyClipboardPending = String(txt); }} catch (e) {{}}
                    try {{ return _cwt(txt); }}
                    catch (e) {{ return Promise.resolve(); }}
                }};
            }}
            document.addEventListener('copy', function() {{
                try {{
                    var t = (window.getSelection() || '').toString();
                    if (t) window.__pyClipboardPending = t;
                }} catch (e) {{}}
            }});
        }} catch (e) {{}}
    }})();
    """
    script = QWebEngineScript()
    script.setName("adskip-mainworld")
    script.setSourceCode(js)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
    script.setRunsOnSubFrames(False)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    return script


def build_lyrics_script() -> QWebEngineScript:
    """In-page lyrics panel (MainWorld): a Lyrics toggle button in the
    player bar opens a synced-lyrics sidebar for the current track.

    Providers, tried in order (keyless always run; key-based are baked
    at launch from env and skipped silently when unset):
      1. lrclib.net exact -- synced LRC, then plain (keyless).
      2. lrclib.net search -- loose match the strict endpoint misses.
      3. Netease -- synced LRC via CORS proxy (ported from lyrics-plus).
      4. Musixmatch -- plain, via JSONP (SPOTIFY_MUSIXMATCH_KEY).
      5. Vagalume -- plain (SPOTIFY_VAGALUME_KEY).
      6. Genius -- plain, search API + page scrape (SPOTIFY_GENIUS_TOKEN).
      7. lyrics.ovh -- plain (keyless).
      8. ChartLyrics -- plain XML API (keyless).
    Track identity comes from MediaSession metadata + the duration
    label; position from --progress-bar-transform x duration. No
    separate window, no crash surface: plain DOM + fetch.
    """
    js = f"""
    (function() {{
        if (window.__pyLyrics) return; window.__pyLyrics = true;
        var DEBUGJS = {'true' if DEBUG else 'false'};
        var panel = null, listEl = null, headEl = null;
        var findBar = null, findInput = null, findHits = [], findIdx = -1;
        // Follow mode (default): highlight + autoscroll to the sung
        // line. FREE mode: static text, no jumping, no sync to worry
        // about. Persisted with the lyrics config.
        var follow = true;
        var curKey = '', lines = [], synced = false;
        function log(m) {{ if (DEBUGJS) console.log('[lyrics] ' + m); }}
        function css() {{
            if (document.getElementById('py-lyrics-style')) return;
            var s = document.createElement('style');
            s.id = 'py-lyrics-style';
            s.textContent = '#py-lyrics-panel{{position:fixed;top:64px;right:12px;bottom:92px;width:340px;z-index:200;background:rgba(10,10,12,0.94);border:1px solid rgba(255,255,255,0.12);border-radius:12px;display:flex;flex-direction:column;overflow:hidden;font-family:inherit}}'
                + '#py-lyrics-head{{padding:12px 14px;border-bottom:1px solid rgba(255,255,255,0.1);color:#fff;font-weight:bold;font-size:14px;display:flex;align-items:center}}'
                + '#py-lyrics-title{{flex:1}}'
                + '#py-lyrics-gear{{background:transparent;border:none;color:#9a9a9a;font-size:15px;cursor:pointer;padding:2px 6px;border-radius:4px}}'
                + '#py-lyrics-gear:hover{{color:#fff}}'
                + '#py-lyrics-follow{{background:transparent;border:none;color:#9a9a9a;font-size:10px;font-weight:bold;cursor:pointer;padding:2px 6px;border-radius:4px}}'
                + '#py-lyrics-follow:hover{{color:#fff}}'
                + '#py-lyrics-follow.off{{color:#555}}'
                + '#py-lyrics-sub{{padding:6px 14px;color:#9a9a9a;font-size:11px;border-bottom:1px solid rgba(255,255,255,0.08)}}'
                + '#py-lyrics-list{{flex:1;overflow-y:auto;padding:10px 14px;color:#b3b3b3;font-size:14px;line-height:2}}'
                + '#py-lyrics-list .w.onw{{color:#fff;font-weight:bold}}'
                + '#py-lyrics-find{{display:none;padding:8px 14px;border-bottom:1px solid rgba(255,255,255,0.08)}}'
                + '#py-lyrics-find input{{width:100%;box-sizing:border-box;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.16);border-radius:6px;color:#fff;padding:7px;font-size:13px}}'
                + '#py-lyrics-list .hit{{color:#fff;text-decoration:underline}}'
                + '#py-lyrics-list .cur-hit{{background:rgba(255,255,255,0.16);border-radius:4px}}'
                + '#py-lyrics-list label{{display:block;color:#9a9a9a;font-size:11px;margin:10px 0 4px}}'
                + '#py-lyrics-list input{{width:100%;box-sizing:border-box;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.16);border-radius:6px;color:#fff;padding:8px;font-size:13px}}'
                + '#py-lyrics-save{{margin-top:14px;width:100%;background:rgba(255,255,255,0.16);border:none;border-radius:6px;color:#fff;font-weight:bold;padding:9px;cursor:pointer}}'
                + '#py-lyrics-save:hover{{background:rgba(255,255,255,0.28)}}'
                + '#py-lyrics-transcribe{{margin-top:12px;width:100%;background:rgba(255,255,255,0.12);border:1px solid rgba(255,255,255,0.16);border-radius:6px;color:#fff;padding:9px;cursor:pointer;font-size:13px}}'
                + '#py-lyrics-transcribe:hover{{background:rgba(255,255,255,0.24)}}'
                + '#py-lyrics-record,#py-lyrics-retry{{margin-top:8px;width:100%;background:transparent;border:1px solid rgba(255,255,255,0.16);border-radius:6px;color:#b3b3b3;padding:8px;cursor:pointer;font-size:12px}}'
                + '#py-lyrics-record:hover,#py-lyrics-retry:hover{{color:#fff;background:rgba(255,255,255,0.1)}}'
                + '#py-lyrics-btn{{flex:0 0 auto;align-self:center;white-space:nowrap;width:auto;max-width:max-content;background:transparent !important;border:none;color:#b3b3b3;font-size:11px;font-weight:bold;cursor:pointer;padding:6px 8px;border-radius:4px}}'
                + '#py-lyrics-btn:hover{{color:#fff}}'
                + '#py-lyrics-btn svg{{width:16px;height:16px;display:block;fill:currentColor}}';
            document.head.appendChild(s);
        }}
        function trackInfo() {{
            var title = '', artist = '', dur = 0;
            try {{
                var m = navigator.mediaSession && navigator.mediaSession.metadata;
                if (m) {{ title = m.title || ''; artist = m.artist || ''; }}
            }} catch (e) {{}}
            try {{
                var np = document.querySelector('[data-testid="now-playing-bar"]');
                var times = np ? np.innerText.match(/\\d+:\\d+/g) : null;
                if (times && times.length) dur = toSec(times[times.length - 1]);
            }} catch (e) {{}}
            return {{title: title, artist: artist, dur: dur,
                     key: (title + '|' + artist).toLowerCase()}};
        }}
        function toSec(t) {{
            var p = t.split(':');
            return p.length === 2 ? (+p[0]) * 60 + (+p[1]) : 0;
        }}
        function position() {{
            try {{
                var bar = document.querySelector('[data-testid="progress-bar"]');
                var st = bar ? (bar.getAttribute('style') || '') : '';
                var m = st.match(/--progress-bar-transform:\\s*([\\d.]+)%/);
                var frac = m ? parseFloat(m[1]) / 100 : 0;
                var info = trackInfo();
                return frac * (info.dur || 0);
            }} catch (e) {{ return 0; }}
        }}
        function parseLRC(text) {{
            var out = [];
            var rows = String(text || '').split('\\n');
            for (var i = 0; i < rows.length; i++) {{
                var m = rows[i].match(/\\[(\\d+):(\\d+(?:\\.\\d+)?)\\](.*)/);
                if (m) out.push({{t: (+m[1]) * 60 + (+m[2]), x: m[3].trim()}});
            }}
            out.sort(function(a, b) {{ return a.t - b.t; }});
            return out;
        }}
        // Netease karaoke lines: [lineStart,lineDur](off,dur)word...
        // offsets in ms relative to line start.
        function parseKaraoke(text) {{
            var out = [];
            var rows = String(text || '').split('\\n');
            for (var i = 0; i < rows.length; i++) {{
                var lm = rows[i].match(/^\\[(\\d+),(\\d+)\\]/);
                if (!lm) continue;
                var start = (+lm[1]) / 1000;
                var rest = rows[i].slice(lm[0].length);
                var words = [], re = /\\((\\d+),(\\d+)\\)([^()]*)/g, m;
                var anyWord = false, txt = '';
                while ((m = re.exec(rest))) {{
                    if (m[3] === '') continue;
                    txt += m[3];
                    words.push({{w: m[3], t: start + (+m[1]) / 1000}});
                    if (m[3].trim() !== '') anyWord = true;
                }}
                if (anyWord) out.push({{t: start, x: txt, words: words}});
            }}
            return out;
        }}
        function render(statusLine) {{
            if (!listEl) return;
            listEl.innerHTML = '';
            findHits = []; findIdx = -1;
            if (!lines.length) {{
                var d = document.createElement('div');
                d.textContent = statusLine || 'No lyrics found for this track.';
                listEl.appendChild(d); return;
            }}
            for (var i = 0; i < lines.length; i++) {{
                var d = document.createElement('div');
                d.dataset.i = i;
                if (lines[i].words) {{
                    // Karaoke line: one span per word for word-timing.
                    for (var w = 0; w < lines[i].words.length; w++) {{
                        var sp = document.createElement('span');
                        sp.className = 'w';
                        sp.textContent = lines[i].words[w].w;
                        sp.dataset.t = lines[i].words[w].t;
                        d.appendChild(sp);
                    }}
                }} else {{
                    d.textContent = lines[i].x || '…';
                }}
                listEl.appendChild(d);
            }}
        }}
        function sync() {{
            if (!panel || !synced || !lines.length) return;
            if (!follow) return;  // FREE mode: static text, no marks
            var pos = position(), cur = 0;
            for (var i = 0; i < lines.length; i++)
                if (lines[i].t <= pos + 0.15) cur = i;
            var kids = listEl.children;
            for (var j = 0; j < kids.length; j++) {{
                // classList (not className): preserve find-hit marks.
                if (j === cur) kids[j].classList.add('on');
                else kids[j].classList.remove('on');
            }}
            var line = lines[cur], el = kids[cur];
            if (line && line.words && el) {{
                var spans = el.querySelectorAll('span');
                for (var s = 0; s < spans.length; s++) {{
                    var wt = parseFloat(spans[s].dataset.t || '0');
                    spans[s].className = 'w' + (wt <= pos + 0.05 ? ' onw' : '');
                }}
            }}
            if (el && !(findBar && findBar.style.display === 'block' &&
                        findInput && findInput.value))
                el.scrollIntoView({{block: 'center', behavior: 'smooth'}});
        }}
        function clearSyncMarks() {{
            if (!listEl) return;
            try {{
                var kids = listEl.children;
                for (var i = 0; i < kids.length; i++) {{
                    kids[i].classList.remove('on');
                    var spans = kids[i].querySelectorAll('span.w');
                    for (var j = 0; j < spans.length; j++)
                        spans[j].classList.remove('onw');
                }}
            }} catch (e) {{}}
        }}
        function sub(t) {{ if (headEl) headEl.nextSibling.textContent = t; }}
        // Provider chain. Keyless providers always run; key-based ones
        // are baked at launch from env (empty = skipped silently):
        //   SPOTIFY_MUSIXMATCH_KEY, SPOTIFY_VAGALUME_KEY
        var MM_KEY = {json.dumps(os.environ.get("SPOTIFY_MUSIXMATCH_KEY", ""))};
        var VAG_KEY = {json.dumps(os.environ.get("SPOTIFY_VAGALUME_KEY", ""))};
        var GEN_KEY = {json.dumps(os.environ.get("SPOTIFY_GENIUS_TOKEN", ""))};
        // Key resolution: settings UI (localStorage) wins, env-baked
        // value is the fallback. Python mirrors localStorage to a JSON
        // config file and re-seeds it on startup (see __pyLyricsKeys).
        function K(kind) {{
            var cfg = loadCfg();
            var v = cfg.keys[kind];
            if (v === undefined || v === null) {{
                var env = {{mm: MM_KEY, vag: VAG_KEY, gen: GEN_KEY}};
                return env[kind] || '';
            }}
            return v;
        }}
        function currentKeys() {{
            var cfg = loadCfg();
            return {{mm: cfg.keys.mm || '', vag: cfg.keys.vag || '',
                     gen: cfg.keys.gen || ''}};
        }}
        // Config: {{keys:{{mm,vag,gen}}, order:[ids], off:{{id:true}}}}.
        // Env-baked keys are defaults; settings UI overrides persist in
        // localStorage and mirror to the Python JSON config file.
        var PROV_ORDER = ['lrclib', 'lrclib-search', 'netease', 'mm',
                          'vag', 'gen', 'ovh', 'chart'];
        var PROV_NAMES = {{lrclib: 'lrclib exact', 'lrclib-search': 'lrclib search',
                           netease: 'Netease', mm: 'Musixmatch', vag: 'Vagalume',
                           gen: 'Genius', ovh: 'lyrics.ovh', chart: 'ChartLyrics'}};
        function loadCfg() {{
            var cfg = {{keys: {{mm: MM_KEY, vag: VAG_KEY, gen: GEN_KEY}},
                        order: PROV_ORDER.slice(), off: {{}}, follow: true}};
            try {{
                var raw = localStorage.getItem('py-lyr-cfg');
                if (raw) {{
                    var s = JSON.parse(raw);
                    if (s.keys) for (var k in s.keys) cfg.keys[k] = s.keys[k];
                    if (s.order && s.order.length) {{
                        // Stored order wins, but pick up providers added
                        // after it was saved so they still run.
                        cfg.order = s.order.filter(function(id) {{
                            return PROV_ORDER.indexOf(id) !== -1;
                        }});
                        for (var p = 0; p < PROV_ORDER.length; p++)
                            if (cfg.order.indexOf(PROV_ORDER[p]) === -1)
                                cfg.order.push(PROV_ORDER[p]);
                    }}
                    if (s.off) cfg.off = s.off;
                    if (s.follow !== undefined) cfg.follow = !!s.follow;
                }} else {{
                    // Migrate legacy per-slot keys.
                    var l = {{mm: localStorage.getItem('py-mm'),
                             vag: localStorage.getItem('py-vag'),
                             gen: localStorage.getItem('py-gen')}};
                    for (var k2 in l)
                        if (l[k2] !== null) cfg.keys[k2] = l[k2];
                }}
            }} catch (e) {{}}
            return cfg;
        }}
        function saveCfg(cfg) {{
            try {{
                localStorage.setItem('py-lyr-cfg', JSON.stringify(cfg));
                // Python pump picks this up and writes the JSON config.
                window.__pyLyricsKeysPending = JSON.stringify(cfg);
                log('config saved');
            }} catch (e) {{}}
        }}
        function plainRows(text) {{
            return String(text || '').split('\\n').map(
                function(x) {{ return {{t: 0, x: x}}; }}).filter(
                function(r) {{ return r.x.trim() !== ''; }});
        }}
        // fetch() with a hard timeout: a stalled provider must fail
        // over to the next, never freeze the chain (this was the 1/5
        // hang -- lrclib stalling with no resolve/reject).
        function fetchT(url, ms, opts) {{
            return new Promise(function(res, rej) {{
                var to = setTimeout(function() {{ rej(0); }}, ms || 9000);
                fetch(url, opts).then(function(r) {{
                    clearTimeout(to); res(r);
                }}, function(e) {{ clearTimeout(to); rej(e || 0); }});
            }});
        }}
        function jsonp(url, param) {{
            // Musixmatch blocks CORS but allows JSONP script callbacks.
            return new Promise(function(res, rej) {{
                var cb = '__pymm' + Date.now() + Math.floor(Math.random() * 999);
                var done = false;
                function cleanup() {{
                    if (done) return; done = true;
                    try {{ delete window[cb]; }} catch (e) {{}}
                    try {{ s.remove(); }} catch (e) {{}}
                }}
                window[cb] = function(d) {{ cleanup(); res(d); }};
                var s = document.createElement('script');
                s.onerror = function() {{ cleanup(); rej(0); }};
                s.src = url + (url.indexOf('?') === -1 ? '?' : '&') +
                        (param || 'callback') + '=' + cb;
                document.head.appendChild(s);
                setTimeout(function() {{ cleanup(); rej(0); }}, 12000);
            }});
        }}
        function provLrclibSynced(info) {{
            var q = 'artist_name=' + encodeURIComponent(info.artist) +
                    '&track_name=' + encodeURIComponent(info.title) +
                    '&duration=' + Math.round(info.dur || 0);
            return fetchT('https://lrclib.net/api/get?' + q).then(function(r) {{
                if (!r.ok) throw 0;
                return r.json();
            }}).then(function(d) {{
                if (d && d.syncedLyrics) {{
                    var rows = parseLRC(d.syncedLyrics);
                    if (rows.length) return {{rows: rows, synced: true,
                        label: 'Synced lyrics · via lrclib'}};
                }}
                if (d && d.plainLyrics) {{
                    var rows = plainRows(d.plainLyrics);
                    if (rows.length) return {{rows: rows, synced: false,
                        label: 'Plain lyrics · via lrclib'}};
                }}
                throw 0;
            }});
        }}
        // lrclib loose search: /api/get is strict and 404s on songs
        // the database actually has (known lrclib quirk); /api/search
        // finds them. First hit with synced LRC wins, else plain.
        function provLrclibSearch(info) {{
            var q = 'https://lrclib.net/api/search?q=' +
                    encodeURIComponent(info.title + ' ' + info.artist);
            return fetchT(q).then(function(r) {{
                if (!r.ok) throw 0;
                return r.json();
            }}).then(function(arr) {{
                if (!arr || !arr.length) throw 0;
                for (var i = 0; i < Math.min(arr.length, 5); i++) {{
                    var d = arr[i];
                    if (d && d.syncedLyrics) {{
                        var rows = parseLRC(d.syncedLyrics);
                        if (rows.length) return {{rows: rows, synced: true,
                            label: 'Synced lyrics · via lrclib search'}};
                    }}
                }}
                for (var j = 0; j < Math.min(arr.length, 5); j++) {{
                    var p = arr[j] && arr[j].plainLyrics;
                    var rows = plainRows(p);
                    if (rows.length) return {{rows: rows, synced: false,
                        label: 'Plain lyrics · via lrclib search'}};
                }}
                throw 0;
            }});
        }}
        // Netease (ported from Spicetify lyrics-plus, which reaches it
        // through the xianqiao CORS proxy): huge catalog, synced LRC.
        function provNetease(info) {{
            var q = 'https://music.xianqiao.wang/neteaseapiv2/search' +
                    '?limit=10&type=1&keywords=' +
                    encodeURIComponent(info.title + ' ' + info.artist);
            return fetchT(q).then(function(r) {{
                if (!r.ok) throw 0;
                return r.json();
            }}).then(function(d) {{
                var songs = d && d.result && d.result.songs;
                if (!songs || !songs.length) throw 0;
                var tw = words(info.title);
                var pick = null;
                for (var i = 0; i < songs.length && !pick; i++) {{
                    var nw = words(songs[i].name);
                    if (tw.some(function(w) {{ return nw.indexOf(w) !== -1; }}))
                        pick = songs[i];
                }}
                if (!pick) pick = songs[0];
                return fetchT('https://music.xianqiao.wang/neteaseapiv2/lyric?id=' +
                              pick.id);
            }}).then(function(r) {{
                if (!r.ok) throw 0;
                return r.json();
            }}).then(function(d) {{
                var lyric = d && d.lrc && d.lrc.lyric;
                if (!lyric || lyric.indexOf('纯音乐') !== -1) throw 0;
                var kara = null;
                try {{
                    var kl = d && d.klyric && d.klyric.lyric;
                    if (kl) {{
                        var kr = parseKaraoke(kl);
                        if (kr.length > 3) kara = kr;
                    }}
                }} catch (e) {{ kara = null; }}
                var rows = parseLRC(lyric);
                if (!rows.length) {{
                    rows = plainRows(lyric.replace(/\\[.*?\\]/g, ''));
                    if (!rows.length) throw 0;
                    return {{rows: rows, synced: false,
                             label: 'Plain lyrics · via Netease'}};
                }}
                if (kara) return {{rows: kara, synced: true,
                                   label: 'Karaoke · via Netease'}};
                return {{rows: rows, synced: true,
                         label: 'Synced lyrics · via Netease'}};
            }});
        }}
        function provMusixmatch(info) {{
            var key = K('mm');
            if (!key) return Promise.reject(0);
            var q = 'https://api.musixmatch.com/ws/1.1/matcher.lyrics.get' +
                    '?format=jsonp&q_track=' + encodeURIComponent(info.title) +
                    '&q_artist=' + encodeURIComponent(info.artist) +
                    '&apikey=' + encodeURIComponent(key);
            return jsonp(q).then(function(d) {{
                var l = d && d.message && d.message.body &&
                        d.message.body.lyrics;
                var body = l && l.lyrics_body;
                if (!body || (l.restricted && l.instrumental)) throw 0;
                var clean = String(body).split('\\n').filter(function(x) {{
                    return x.trim() !== '' &&
                           x.indexOf('*******') !== 0 &&
                           x.toLowerCase().indexOf('commercial use') === -1;
                }}).join('\\n');
                var rows = plainRows(clean);
                if (!rows.length) throw 0;
                return {{rows: rows, synced: false,
                         label: 'Plain lyrics · via Musixmatch'}};
            }});
        }}
        function provVagalume(info) {{
            var key = K('vag');
            if (!key) return Promise.reject(0);
            var q = 'https://api.vagalume.com.br/search.php?art=' +
                    encodeURIComponent(info.artist) + '&mus=' +
                    encodeURIComponent(info.title) + '&apikey=' +
                    encodeURIComponent(key);
            return fetchT(q).then(function(r) {{
                if (!r.ok) throw 0;
                return r.json();
            }}).then(function(d) {{
                var mus = d && d.mus;
                var txt = mus && mus[0] && (mus[0].text || mus[0].translate &&
                         mus[0].translate[0] && mus[0].translate[0].text);
                var rows = plainRows(txt);
                if (!rows.length) throw 0;
                return {{rows: rows, synced: false,
                         label: 'Plain lyrics · via Vagalume'}};
            }});
        }}
        function words(s) {{
            return String(s || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ')
                .split(' ').filter(function(w) {{ return w.length > 2; }});
        }}
        function provGenius(info) {{
            // Genius API finds the song URL (free token); the lyric text
            // itself is scraped from the song page through a CORS proxy
            // and read out of the [data-lyrics-container] divs.
            var key = K('gen');
            if (!key) return Promise.reject(0);
            var q = 'https://api.genius.com/search?q=' +
                    encodeURIComponent(info.title + ' ' + info.artist);
            return fetchT(q, 9000,
                {{headers: {{'Authorization': 'Bearer ' + key}}}}).then(function(r) {{
                if (!r.ok) throw 0;
                return r.json();
            }}).then(function(d) {{
                var hits = d && d.response && d.response.hits;
                var top = hits && hits[0] && hits[0].result;
                if (!top || !top.url) throw 0;
                // Loose sanity check: top hit must share a significant
                // word with the track or artist (wrong song is worse
                // than no song).
                var tw = words(info.title), aw = words(info.artist);
                var hw = words(top.title).concat(words(top.primary_artist &&
                                                       top.primary_artist.name));
                var ok = tw.concat(aw).some(function(w) {{
                    return hw.indexOf(w) !== -1;
                }});
                if (!ok) throw 0;
                return fetchT('https://api.allorigins.win/raw?url=' +
                              encodeURIComponent(top.url), 12000);
            }}).then(function(r) {{
                if (!r.ok) throw 0;
                return r.text();
            }}).then(function(html) {{
                var doc = null;
                try {{
                    doc = new DOMParser().parseFromString(html, 'text/html');
                }} catch (e) {{ throw 0; }}
                var boxes = doc.querySelectorAll('[data-lyrics-container="true"]');
                if (!boxes.length) throw 0;
                var parts = [];
                for (var i = 0; i < boxes.length; i++) {{
                    var el = boxes[i].cloneNode(true);
                    el.querySelectorAll('br').forEach(function(br) {{
                        br.replaceWith(doc.createTextNode('\\n'));
                    }});
                    var t = (el.textContent || '').trim();
                    if (t) parts.push(t);
                }}
                var rows = plainRows(parts.join('\\n'));
                if (!rows.length) throw 0;
                return {{rows: rows, synced: false,
                         label: 'Plain lyrics · via Genius'}};
            }});
        }}
        function provLyricsOvh(info) {{
            return fetchT('https://api.lyrics.ovh/v1/' +
                         encodeURIComponent(info.artist) + '/' +
                         encodeURIComponent(info.title)).then(function(r) {{
                if (!r.ok) throw 0;
                return r.json();
            }}).then(function(d) {{
                var rows = plainRows(d && d.lyrics);
                if (!rows.length) throw 0;
                return {{rows: rows, synced: false,
                         label: 'Plain lyrics · via lyrics.ovh'}};
            }});
        }}
        function provChartLyrics(info) {{
            var q = 'https://api.chartlyrics.com/apiv2.asmx/SearchLyricDirect' +
                    '?artist=' + encodeURIComponent(info.artist) +
                    '&song=' + encodeURIComponent(info.title);
            return fetchT(q).then(function(r) {{
                if (!r.ok) throw 0;
                return r.text();
            }}).then(function(t) {{
                var doc = null;
                try {{
                    doc = new DOMParser().parseFromString(t, 'text/xml');
                }} catch (e) {{ throw 0; }}
                var el = doc && doc.getElementsByTagName('Lyric')[0];
                var rows = plainRows(el && el.textContent);
                if (!rows.length) throw 0;
                return {{rows: rows, synced: false,
                         label: 'Plain lyrics · via ChartLyrics'}};
            }});
        }}
        // Auto-transcribe fallback: when no provider has the track,
        // offer local Whisper AI transcription (Python side downloads
        // YouTube audio + transcribes; result returns timestamped).
        var transKey = '';
        function offerTranscribe(info) {{
            if (!listEl) return;
            var b = document.createElement('button');
            b.id = 'py-lyrics-transcribe';
            b.textContent = 'Transcribe with local AI (slow)';
            b.title = 'Downloads audio + runs Whisper locally. Takes minutes.';
            b.onclick = function() {{ requestTranscribe(info, 'youtube'); }};
            listEl.appendChild(b);
            var r = document.createElement('button');
            r.id = 'py-lyrics-record';
            r.textContent = 'Or record the playing song (exact)';
            r.title = 'Restarts the song and records it live. Guaranteed the right song; takes one full play plus transcription. Keep volume up.';
            r.onclick = function() {{ requestTranscribe(info, 'record'); }};
            listEl.appendChild(r);
        }}
        // video IDs already tried per track (wrong-song retries).
        var transTried = {{}};
        function requestTranscribe(info, mode) {{
            try {{
                transKey = info.key;
                window.__pyTranscribeResult = null;
                window.__pyTranscribeStatus = 'Requesting…';
                window.__pyTranscribeReq = JSON.stringify({{
                    title: info.title, artist: info.artist,
                    dur: Math.round(info.dur || 0), key: info.key,
                    mode: mode || 'youtube',
                    exclude: transTried[info.key] || []}});
                sub(mode === 'record' ? 'Recording requested — song restarts…'
                                      : 'Transcription requested…');
                log('transcribe requested (' + (mode || 'youtube') + ')');
            }} catch (e) {{}}
        }}
        function pollTranscribe() {{
            try {{
                var st = window.__pyTranscribeStatus;
                if (st && transKey && transKey === curKey) sub(String(st));
                var tr = window.__pyTranscribeResult;
                if (!tr || transKey !== curKey) return;
                window.__pyTranscribeResult = null;
                if (tr.error) {{
                    render('Transcription failed: ' + tr.error);
                    sub(''); offerTranscribe(trackInfo()); transKey = ''; return;
                }}
                if (tr.lines && tr.lines.length) {{
                    lines = tr.lines; synced = true;
                    render();
                    var via = tr.video_title ? ' · ' + tr.video_title : '';
                    sub((tr.label || 'Transcribed') + via);
                    log('transcribed ' + lines.length + ' lines');
                    if (tr.video_id && tr.video_id !== 'live-record') {{
                        var arr = transTried[transKey] || [];
                        if (arr.indexOf(tr.video_id) === -1)
                            arr.push(tr.video_id);
                        transTried[transKey] = arr;
                        var rb = document.createElement('button');
                        rb.id = 'py-lyrics-retry';
                        rb.textContent = 'Wrong song? Try next match';
                        rb.onclick = function() {{
                            lines = []; render('Searching again…');
                            requestTranscribe(trackInfo(), 'youtube');
                        }};
                        if (listEl) listEl.appendChild(rb);
                    }}
                    transKey = '';
                }}
            }} catch (e) {{}}
        }}
        function loadFor(info) {{
            curKey = info.key; lines = []; synced = false;
            render('Searching lyrics…');
            sub(info.title + ' — ' + info.artist);
            // All enabled providers fire at once in configured order;
            // first usable result shows immediately, and a synced
            // result later UPGRADES a plain one. Track change aborts
            // everything via curKey.
            var FNS = {{lrclib: provLrclibSynced,
                        'lrclib-search': provLrclibSearch,
                        netease: provNetease, mm: provMusixmatch,
                        vag: provVagalume, gen: provGenius,
                        ovh: provLyricsOvh, chart: provChartLyrics}};
            var cfg = loadCfg();
            var chain = [];
            for (var i = 0; i < cfg.order.length; i++) {{
                var id = cfg.order[i];
                if (!cfg.off[id] && FNS[id]) chain.push(FNS[id]);
            }}
            var pending = chain.length, served = false;
            sub('Searching lyrics (' + chain.length + ' providers)…');
            chain.forEach(function(prov) {{
                prov(info).then(function(hit) {{
                    if (curKey !== info.key) return;
                    pending--;
                    if (hit.synced && !synced) {{
                        lines = hit.rows; synced = true; served = true;
                        render(); sub(hit.label);
                        log('served: ' + hit.label);
                    }} else if (!served) {{
                        lines = hit.rows; served = true;
                        render(); sub(hit.label);
                        log('served: ' + hit.label);
                    }}
                    if (!served && pending === 0) {{
                        lines = []; render(); sub('');
                        offerTranscribe(info);
                    }}
                }}, function() {{
                    if (curKey !== info.key) return;
                    pending--;
                    if (!served && pending === 0) {{
                        lines = []; render(); sub('');
                        offerTranscribe(info);
                    }}
                }});
            }});
        }}
        function ensureButton() {{
            try {{
                if (document.getElementById('py-lyrics-btn')) return;
                var np = document.querySelector('[data-testid="now-playing-bar"]');
                if (!np) return;
                css();  // styles must exist before first panel open,
                        // or the button renders as a default grey strip
                var b = document.createElement('button');
                b.id = 'py-lyrics-btn';
                b.title = 'Show lyrics';
                // Wear Spotify's own mic icon: clone the stock lyrics
                // button's SVG (that button stays hidden; text fallback).
                try {{
                    var lbs = document.querySelectorAll(
                        'button[aria-label*="lyric" i]');
                    for (var li = 0; li < lbs.length; li++) {{
                        var svg = lbs[li].querySelector('svg');
                        if (svg) {{
                            b.appendChild(svg.cloneNode(true));
                            break;
                        }}
                    }}
                    if (!b.firstChild) b.textContent = 'LYRICS';
                }} catch (e) {{ b.textContent = 'LYRICS'; }}
                b.onclick = toggle;
                // Inline with the controls row (next to volume), NOT as a
                // new full-width flex row: the aside stacks vertically, so
                // appending here created a second band under the bar.
                var anchor = np.querySelector('[data-testid="volume-bar"]') ||
                             np.querySelector('[data-testid="control-button-queue"]');
                if (anchor && anchor.parentElement)
                    anchor.parentElement.insertBefore(b, anchor);
                else
                    np.appendChild(b);
            }} catch (e) {{}}
        }}
        function toggle() {{
            css();
            if (panel) {{
                panel.remove(); panel = null; listEl = null; headEl = null;
                findBar = null; findInput = null; findHits = []; findIdx = -1;
                curKey = ''; settingsOpen = false; return;
            }}
            settingsOpen = false;
            try {{ follow = loadCfg().follow !== false; }}
            catch (x) {{ follow = true; }}
            panel = document.createElement('div');
            panel.id = 'py-lyrics-panel';
            headEl = document.createElement('div');
            headEl.id = 'py-lyrics-head';
            var titleSpan = document.createElement('span');
            titleSpan.id = 'py-lyrics-title';
            titleSpan.textContent = 'Lyrics';
            var followBtn = document.createElement('button');
            followBtn.id = 'py-lyrics-follow';
            followBtn.title = 'Toggle follow (highlight + autoscroll)';
            function paintFollow() {{
                followBtn.textContent = follow ? 'FOLLOW' : 'FREE';
                followBtn.className = follow ? '' : 'off';
            }}
            paintFollow();
            followBtn.onclick = function(e) {{
                try {{ e.stopPropagation(); }} catch (x) {{}}
                follow = !follow;
                paintFollow();
                try {{
                    var cfg = loadCfg();
                    cfg.follow = follow;
                    saveCfg(cfg);
                }} catch (x) {{}}
                if (!follow && listEl) clearSyncMarks();
                else sync();
            }};
            var gear = document.createElement('button');
            gear.id = 'py-lyrics-gear';
            gear.textContent = '⚙';
            gear.title = 'Lyrics providers + API keys';
            gear.onclick = function(e) {{
                try {{ e.stopPropagation(); }} catch (x) {{}}
                openSettings();
            }};
            headEl.appendChild(titleSpan);
            headEl.appendChild(followBtn);
            headEl.appendChild(gear);
            var subEl = document.createElement('div');
            subEl.id = 'py-lyrics-sub';
            listEl = document.createElement('div');
            listEl.id = 'py-lyrics-list';
            panel.appendChild(headEl);
            panel.appendChild(subEl);
            findBar = document.createElement('div');
            findBar.id = 'py-lyrics-find';
            findInput = document.createElement('input');
            findInput.placeholder = 'Find in lyrics (Enter ↵ / Shift+Enter)';
            findInput.autocomplete = 'off';
            findInput.spellcheck = false;
            findInput.addEventListener('input', findMatches);
            findInput.addEventListener('keydown', function(e) {{
                if (e.key === 'Enter') {{
                    e.preventDefault(); jumpFind(!e.shiftKey);
                }} else if (e.key === 'Escape') {{
                    toggleFind(false);
                }}
                try {{ e.stopPropagation(); }} catch (x) {{}}
            }});
            findBar.appendChild(findInput);
            panel.appendChild(findBar);
            panel.appendChild(listEl);
            document.body.appendChild(panel);
            loadFor(trackInfo());
        }}
        // Search-in-lyrics (lyrics-plus parity): Ctrl+Shift+F toggles,
        // Enter / Shift+Enter jumps next/prev match.
        function toggleFind(force) {{
            if (!findBar) return;
            var show = (typeof force === 'boolean') ? force :
                       (findBar.style.display !== 'block');
            findBar.style.display = show ? 'block' : 'none';
            findHits = []; findIdx = -1;
            if (show && findInput) findInput.focus();
            else paintFind();
        }}
        function findMatches() {{
            findHits = []; findIdx = -1;
            var q = findInput ? findInput.value.toLowerCase() : '';
            if (q && listEl) {{
                var kids = listEl.children;
                for (var i = 0; i < kids.length; i++) {{
                    var t = (kids[i].innerText || '').toLowerCase();
                    if (t.indexOf(q) !== -1) findHits.push(i);
                }}
                if (findHits.length) findIdx = 0;
            }}
            paintFind(true);
        }}
        function paintFind(scroll) {{
            if (!listEl) return;
            var kids = listEl.children;
            for (var i = 0; i < kids.length; i++) {{
                kids[i].classList.remove('hit');
                kids[i].classList.remove('cur-hit');
            }}
            for (var h = 0; h < findHits.length; h++) {{
                var el = kids[findHits[h]];
                if (el) el.classList.add('hit');
            }}
            if (scroll && findIdx >= 0) {{
                var cur = kids[findHits[findIdx]];
                if (cur) {{
                    cur.classList.add('cur-hit');
                    cur.scrollIntoView({{block: 'center'}});
                }}
            }}
        }}
        function jumpFind(fwd) {{
            if (!findHits.length) return;
            findIdx = (findIdx + (fwd ? 1 : -1) + findHits.length) % findHits.length;
            paintFind(true);
        }}
        document.addEventListener('keydown', function(e) {{
            try {{
                if (!panel || settingsOpen) return;
                if (e.ctrlKey && e.shiftKey &&
                    (e.key === 'F' || e.key === 'f')) {{
                    e.preventDefault();
                    toggleFind();
                    try {{ e.stopPropagation(); }} catch (x) {{}}
                }}
            }} catch (x) {{}}
        }});
        // Settings view: API keys for the key-based providers, saved to
        // localStorage (live layer) + mirrored to the Python JSON config.
        var settingsOpen = false;
        function openSettings() {{
            if (!listEl) return;
            settingsOpen = true;
            var ks = currentKeys();
            var cfg = loadCfg();
            listEl.innerHTML = '';
            function field(label, id, val, hint) {{
                var l = document.createElement('label');
                l.textContent = label;
                var inp = document.createElement('input');
                inp.id = id; inp.type = 'text';
                inp.value = val || '';
                inp.placeholder = hint;
                inp.autocomplete = 'off';
                inp.spellcheck = false;
                listEl.appendChild(l);
                listEl.appendChild(inp);
            }}
            field('Musixmatch API key', 'py-k-mm', ks.mm, 'optional');
            field('Vagalume API key', 'py-k-vag', ks.vag, 'optional');
            field('Genius access token', 'py-k-gen', ks.gen, 'optional');
            var pl = document.createElement('label');
            pl.textContent = 'Providers (order = priority)';
            pl.style.marginTop = '14px';
            listEl.appendChild(pl);
            var order = cfg.order.slice();
            function drawOrder() {{
                var old = document.getElementById('py-prov-list');
                if (old) old.remove();
                var box = document.createElement('div');
                box.id = 'py-prov-list';
                order.forEach(function(id, idx) {{
                    var row = document.createElement('div');
                    row.style.cssText = 'display:flex;align-items:center;gap:6px;margin:4px 0;';
                    var cb = document.createElement('input');
                    cb.type = 'checkbox'; cb.dataset.id = id;
                    cb.checked = !cfg.off[id];
                    cb.style.width = 'auto';
                    var nm = document.createElement('span');
                    nm.textContent = PROV_NAMES[id] || id;
                    nm.style.cssText = 'flex:1;color:#e8e8e8;font-size:13px;';
                    var up = document.createElement('button');
                    up.textContent = '▲'; up.title = 'Move up';
                    var dn = document.createElement('button');
                    dn.textContent = '▼'; dn.title = 'Move down';
                    [up, dn].forEach(function(b) {{
                        b.style.cssText = 'background:rgba(255,255,255,0.1);border:none;border-radius:4px;color:#fff;padding:2px 8px;cursor:pointer;';
                    }});
                    up.onclick = function() {{
                        if (idx > 0) {{
                            order.splice(idx - 1, 0, order.splice(idx, 1)[0]);
                            drawOrder();
                        }}
                    }};
                    dn.onclick = function() {{
                        if (idx < order.length - 1) {{
                            order.splice(idx + 1, 0, order.splice(idx, 1)[0]);
                            drawOrder();
                        }}
                    }};
                    row.appendChild(cb);
                    row.appendChild(nm);
                    row.appendChild(up);
                    row.appendChild(dn);
                    box.appendChild(row);
                }});
                listEl.appendChild(box);
            }}
            drawOrder();
            var save = document.createElement('button');
            save.id = 'py-lyrics-save';
            save.textContent = 'SAVE';
            save.onclick = function() {{ saveSettings(order); }};
            listEl.appendChild(save);
            sub('Keys + provider order save to the lyrics config file.');
        }}
        function saveSettings(order) {{
            try {{
                var vals = {{
                    mm: document.getElementById('py-k-mm').value.trim(),
                    vag: document.getElementById('py-k-vag').value.trim(),
                    gen: document.getElementById('py-k-gen').value.trim()
                }};
                var off = {{}};
                var cbs = listEl.querySelectorAll(
                    '#py-prov-list input[type="checkbox"]');
                for (var i = 0; i < cbs.length; i++)
                    if (!cbs[i].checked) off[cbs[i].dataset.id] = true;
                saveCfg({{keys: vals, order: order.slice(), off: off}});
            }} catch (e) {{}}
            settingsOpen = false;
            curKey = '';
            loadFor(trackInfo());
        }}
        // Mic icon can only be cloned once Spotify's own lyrics button
        // exists (it renders later than ours) -- retry until it lands.
        function upgradeButtonIcon() {{
            try {{
                var b = document.getElementById('py-lyrics-btn');
                if (!b || b.querySelector('svg')) return;
                var lbs = document.querySelectorAll(
                    'button[aria-label*="lyric" i]');
                for (var li = 0; li < lbs.length; li++) {{
                    var svg = lbs[li].querySelector('svg');
                    if (svg) {{
                        b.textContent = '';
                        b.appendChild(svg.cloneNode(true));
                        log('mic icon cloned');
                        return;
                    }}
                }}
            }} catch (e) {{}}
        }}
        // sub() targets headEl.nextSibling == the sub div.
        setInterval(function() {{
            ensureButton();
            upgradeButtonIcon();
            if (!panel || settingsOpen) return;
            var info = trackInfo();
            if (info.key && info.key !== curKey &&
                info.title && info.title !== 'Spotify') {{
                transKey = '';
                window.__pyTranscribeStatus = '';
                loadFor(info);
            }}
            sync();
            pollTranscribe();
        }}, 1000);
        log('lyrics armed (MainWorld)');
    }})();
    """
    script = QWebEngineScript()
    script.setName("lyrics-mainworld")
    script.setSourceCode(js)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
    script.setRunsOnSubFrames(False)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    return script


# One-shot footer/art diagnostic for SPOTIFY_DUMP=1: geometry, layout
# chain and artwork box, printed as [dump] lines. No guessing.
_DUMP_JS = ("(() => { try {"
            " const out = {};"
            " const f = document.querySelector('footer');"
            " out.footerFound = !!f;"
            " if (f) {"
            "  const r = f.getBoundingClientRect();"
            "  out.footerRect = {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};"
            "  out.viewport = {w: window.innerWidth, h: window.innerHeight};"
            "  const cs = getComputedStyle(f);"
            "  out.footerCSS = {display: cs.display, position: cs.position, flexShrink: cs.flexShrink, marginTop: cs.marginTop};"
            "  out.parents = [];"
            "  let p = f.parentElement, i = 0;"
            "  while (p && i < 4) {"
            "   let cls = ''; try { cls = String(p.className || '').slice(0, 100); } catch (e) {}"
            "   out.parents.push(p.tagName + '|cls=' + cls + '|tid=' + (p.getAttribute('data-testid') || ''));"
            "   if (i === 0) { const pc = getComputedStyle(p);"
            "    out.parentCSS = {display: pc.display, flexDirection: pc.flexDirection, overflow: pc.overflow}; }"
            "   p = p.parentElement; i++; }"
            "  out.html = f.outerHTML.slice(0, 1200); }"
            " const img = document.querySelector('footer a img, [data-testid=\"now-playing-bar\"] a img, footer img');"
            " out.imgFound = !!img;"
            " if (img) {"
            "  const r = img.getBoundingClientRect();"
            "  const cs = getComputedStyle(img);"
            "  out.imgRect = {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};"
            "  out.imgCSS = {display: cs.display, width: cs.width, height: cs.height, margin: cs.margin, position: cs.position};"
            "  const pr = img.parentElement.getBoundingClientRect();"
            "  out.imgParentRect = {w: Math.round(pr.width), h: Math.round(pr.height)};"
            " out.imgHTML = img.outerHTML.slice(0, 300); }"
            " const np = document.querySelector('[data-testid=\"now-playing-bar\"]');"
            " out.npFound = !!np;"
            " if (np) {"
            "  const r = np.getBoundingClientRect();"
            "  out.npRect = {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};"
            "  const cs = getComputedStyle(np);"
            "  out.npCSS = {display: cs.display, position: cs.position, flexShrink: cs.flexShrink, marginTop: cs.marginTop};"
            "  out.npParents = [];"
            "  let p = np.parentElement, i = 0;"
            "  while (p && i < 3) {"
            "   let cls = ''; try { cls = String(p.className || '').slice(0, 100); } catch (e) {}"
            "   out.npParents.push(p.tagName + '|cls=' + cls + '|tid=' + (p.getAttribute('data-testid') || ''));"
            "   if (i === 0) { const pc = getComputedStyle(p);"
            "    out.npParentCSS = {display: pc.display, flexDirection: pc.flexDirection, overflow: pc.overflow}; }"
            "   p = p.parentElement; i++; }"
            "  const ni = np.querySelector('img');"
            "  out.npImgFound = !!ni;"
            "  if (ni) {"
            "   const r = ni.getBoundingClientRect();"
            "   const cs = getComputedStyle(ni);"
            "   out.npImgRect = {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};"
            "   out.npImgCSS = {display: cs.display, width: cs.width, height: cs.height, margin: cs.margin, position: cs.position};"
            "   const pr = ni.parentElement.getBoundingClientRect();"
            "   out.npImgParentRect = {w: Math.round(pr.width), h: Math.round(pr.height)};"
            " out.npImgHTML = ni.outerHTML.slice(0, 300); } }"
            " const ctrls = document.querySelectorAll('[data-testid*=\"control-button\"],[data-testid*=\"shuffle\"],[data-testid*=\"repeat\"],[data-testid*=\"loop\"]');"
            " out.controls = [];"
            " Array.from(ctrls).forEach(b => {"
            "  try { out.controls.push(b.tagName + ' tid=' + (b.getAttribute('data-testid') || '')"
            "   + ' aria=' + (b.getAttribute('aria-label') || '') + ' dis=' + !!b.disabled"
            "   + ' ariaDis=' + (b.getAttribute('aria-disabled') || '')"
            "   + ' disp=' + getComputedStyle(b).display); } catch (e) {} });"
            " return JSON.stringify(out);"
            " } catch (e) { return '{\"err\":\"' + e + '\"}'; } })()")


class TitleBar(QWidget):
    """Custom titlebar: Discord/ClearVision-style flat window controls
    (dim glyphs, subtle hover, red close) + drag-to-move, since a
    frameless window has no native one. Fill matches the body panels
    exactly, so the whole window reads as one transparency."""

    # Shared with PySteam's TitleBar: keep the two in sync.
    BTN_BASE = (
        "QPushButton { color: #b9bbbe; background: transparent; "
        "border: none; font-size: 16px; }"
        "QPushButton:hover { color: white; background: rgba(255,255,255,25); }"
    )
    BTN_MAX = (
        "QPushButton { color: #b9bbbe; background: transparent; "
        "border: none; font-size: 11px; }"
        "QPushButton:hover { color: white; background: rgba(255,255,255,25); }"
    )
    BTN_CLOSE = (
        "QPushButton { color: #b9bbbe; background: transparent; "
        "border: none; font-size: 18px; }"
        "QPushButton:hover { color: white; background: #ed4245; }"
    )

    def __init__(self, parent_window):
        super().__init__(parent_window)
        self._parent_window = parent_window
        self._drag_pos = None
        self.setFixedHeight(30)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(0)

        label = QLabel("Spotify")
        label.setStyleSheet(
            "color: #b9bbbe; font-weight: bold; background: transparent; "
            "padding-left: 4px;")
        layout.addWidget(label)
        layout.addStretch()

        min_btn = QPushButton("–")
        max_btn = QPushButton("□")
        close_btn = QPushButton("×")
        for b in (min_btn, max_btn, close_btn):
            b.setFixedSize(34, 30)
            layout.addWidget(b)
        min_btn.setStyleSheet(self.BTN_BASE)
        max_btn.setStyleSheet(self.BTN_MAX)
        close_btn.setStyleSheet(self.BTN_CLOSE)
        min_btn.clicked.connect(parent_window.showMinimized)
        max_btn.clicked.connect(self._toggle_max)
        close_btn.clicked.connect(parent_window.close)

        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def _toggle_max(self):
        w = self._parent_window
        try:
            w.showNormal() if w.isMaximized() else w.showMaximized()
        except Exception:
            pass

    def paintEvent(self, event):
        # Titlebar alpha is user-tunable (SPOTIFY_TITLEBAR_ALPHA, default
        # 0.65): panel alpha proved too clear over bright wallpapers.
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(12, 12, 14, int(_TB_ALPHA * 255)))
        super().paintEvent(event)

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


class CornerGrip(QLabel):
    """Visible resize handle for the frameless window. Uses the compositor
    system-resize path (works on Wayland/X11); falls back to manual resize."""

    def __init__(self, parent_window):
        super().__init__("◢", parent_window)
        self._parent_window = parent_window
        self._drag = None
        self.setFixedSize(16, 16)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("color: rgba(255,255,255,110); background: transparent; font-size: 9px;")
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._parent_window.windowHandle()
            if handle is not None:
                try:
                    if handle.startSystemResize(Qt.Edge.RightEdge | Qt.Edge.BottomEdge):
                        event.accept()
                        return
                except Exception:
                    pass
            self._drag = (event.globalPosition().toPoint(), self._parent_window.size())
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            p0, s0 = self._drag
            d = event.globalPosition().toPoint() - p0
            self._parent_window.resize(max(480, s0.width() + d.x()), max(340, s0.height() + d.y()))
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag = None
        event.accept()


class BottomBar(QWidget):
    """Native replacement for Spotify's web now-playing bar (which renders
    as a broken clear strip in QtWebEngine). Same alpha as the titlebar,
    with artwork, transport, seek and volume driving the hidden web player.
    Never grabs the pointer (no drag-to-move -- titlebar owns that)."""

    # Transport falls back to clicking the hidden web player's buttons.
    _BTN_JS = {
        "prev": "(document.querySelector('[data-testid=\"control-button-skip-back\"]')||document.querySelector('[data-testid*=\"skip-back\"]'))?.click()",
        "next": "(document.querySelector('[data-testid=\"control-button-skip-forward\"]')||document.querySelector('[data-testid*=\"skip-forward\"]'))?.click()",
    }
    _PLAYPAUSE_JS = ("(() => { const a = document.querySelector('audio,video');"
                     " if (a) { if (a.paused) { a.play().catch(()=>{}); return 'playing'; } a.pause(); return 'paused'; }"
                     " (document.querySelector('[data-testid=\"control-button-playpause\"]')||document.querySelector('[data-testid*=\"playpause\"]'))?.click();"
                     " return ''; })()")
    # Deep one-shot scan (shadow DOM + iframes + slider inventory) used
    # when playback runs without a light-DOM media element.
    _DEEP_SCAN_JS = ("(() => { try {"
                     " const res = {lightAudio: document.querySelectorAll('audio,video').length,"
                     "  shadowAudio: [], frames: 0, frameAudio: [], sliders: []};"
                     " function walk(root, bucket) {"
                     "  root.querySelectorAll('audio,video').forEach(e => bucket.push(e.tagName + ' paused=' + e.paused));"
                     "  Array.from(root.querySelectorAll('*')).forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, bucket); }); }"
                     " const sh = [];"
                     " Array.from(document.querySelectorAll('*')).forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, sh); });"
                     " res.shadowAudio = sh;"
                     " const fr = document.querySelectorAll('iframe'); res.frames = fr.length;"
                     " Array.from(fr).forEach((f, i) => { try { const d = f.contentDocument;"
                     "  if (d) res.frameAudio.push(i + ':' + d.querySelectorAll('audio,video').length);"
                     "  else res.frameAudio.push(i + ':nodoc'); }"
                     "  catch (e) { res.frameAudio.push(i + ':blocked'); } });"
                     " Array.from(document.querySelectorAll('[role=\"slider\"],input[type=\"range\"]')).forEach(s => {"
                     "  res.sliders.push(s.tagName + ' label=' + (s.getAttribute('aria-label') || '')"
                     "   + ' val=' + (s.value !== undefined ? s.value : s.getAttribute('aria-valuenow'))"
                     "   + ' testid=' + (s.getAttribute('data-testid') || '')); });"
                     " return JSON.stringify(res);"
                     " } catch (e) { return '{\"err\":\"' + e + '\"}'; } })()")
    _SET_INPUT_JS = ("(el,val)=>{ if(!el) return false;"
                     " const d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');"
                     " if(d&&d.set) d.set.call(el,val); else el.value=val;"
                     " el.dispatchEvent(new Event('input',{bubbles:true}));"
                     " el.dispatchEvent(new Event('change',{bubbles:true})); return true; }")
    # One poll returns playback state. Prefers the real media element, but
    # Spotify may keep it detached (new Audio()) or in shadow DOM -- then
    # querySelector finds nothing and we fall back to the web player's own
    # progress/volume range inputs (value readable without layout).
    _STATE_JS = ("(() => { try {"
                 " const a = document.querySelector('audio,video');"
                 " let dur = 0;"
                 " if (a) { if (isFinite(a.duration) && a.duration > 0) dur = a.duration;"
                 "  else if (a.seekable && a.seekable.length)"
                 "  { try { dur = a.seekable.end(a.seekable.length - 1); } catch (e) {} } }"
                 " const pr = document.querySelector('input[type=\"range\"][aria-label*=\"eek\" i],input[type=\"range\"][aria-label*=\"rogress\" i],input[type=\"range\"][aria-label*=\"osition\" i]');"
                 " const vi = document.querySelector('input[type=\"range\"][aria-label*=\"olume\" i]');"
                 " return JSON.stringify({ hasMedia: !!a, tag: a ? a.tagName : '',"
                 "  paused: a ? a.paused : (navigator.mediaSession ? navigator.mediaSession.playbackState !== 'playing' : true),"
                 "  cur: (a && isFinite(a.currentTime)) ? a.currentTime : 0,"
                 "  dur: dur,"
                 "  pv: pr ? parseFloat(pr.value) : -1, pm: pr ? parseFloat(pr.max || '100') : 0,"
                 "  vol: (a && isFinite(a.volume)) ? a.volume : (vi ? parseFloat(vi.value) / (parseFloat(vi.max || '100') || 100) : 1),"
                 "  muted: a ? !!a.muted : false });"
                 " } catch (e) { return '{}'; } })()")

    def __init__(self, parent_window, page):
        super().__init__(parent_window)
        self._parent_window = parent_window
        self._page = page
        self._drag_pos = None
        self._vol_set = False
        self._no_media_warned = False
        self.setFixedHeight(68)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(6)

        self._buttons = {}
        for key, glyph in (("prev", "⏮"), ("playpause", "▶"), ("next", "⏭")):
            b = QPushButton(glyph)
            b.setFixedSize(34, 34)
            b.setStyleSheet(
                "QPushButton { color: white; background: rgba(255,255,255,30); border: none; border-radius: 8px; font-size: 15px; }"
                "QPushButton:hover { background: rgba(255,255,255,70); }"
            )
            if key == "playpause":
                b.clicked.connect(lambda _=False: self._run_js(self._PLAYPAUSE_JS))
            else:
                js = self._BTN_JS[key]
                b.clicked.connect(lambda _=False, _js=js: self._run_js(_js))
            layout.addWidget(b)
            self._buttons[key] = b

        self.cur_label = QLabel("--:--")
        self.cur_label.setStyleSheet("color: rgba(255,255,255,160); background: transparent;")
        layout.addWidget(self.cur_label)

        self.progress = QSlider(Qt.Orientation.Horizontal)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setStyleSheet(
            "QSlider { background: transparent; }"
            "QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,40); border-radius: 2px; }"
            "QSlider::handle:horizontal { width: 12px; background: white; border-radius: 6px; margin: -4px 0; }"
        )
        self.progress.sliderReleased.connect(self._on_seek)
        layout.addWidget(self.progress, 1)

        self.dur_label = QLabel("--:--")
        self.dur_label.setStyleSheet("color: rgba(255,255,255,160); background: transparent;")
        layout.addWidget(self.dur_label)

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(100)
        self.volume.setFixedWidth(80)
        self.volume.setToolTip("Volume")
        self.volume.setStyleSheet(
            "QSlider { background: transparent; }"
            "QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,40); border-radius: 2px; }"
            "QSlider::handle:horizontal { width: 10px; background: white; border-radius: 5px; margin: -3px 0; }"
        )
        self.volume.sliderReleased.connect(self._on_volume)
        layout.addWidget(self.volume)

        layout.addWidget(CornerGrip(parent_window), 0, Qt.AlignmentFlag.AlignBottom)

        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(pal.ColorRole.Window, QColor(12, 12, 14, int(_TB_ALPHA * 255)))
        self.setPalette(pal)
        self.setStyleSheet(
            f"BottomBar {{ background-color: rgba(12, 12, 14, {int(_TB_ALPHA * 255)}); border: none; }}"
            "QLabel { background: transparent; }"
        )
        self.setCursor(Qt.CursorShape.ArrowCursor)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_state)
        self._timer.start(1000)
        self.refresh_state()

    @staticmethod
    def _fmt(secs) -> str:
        try:
            s = int(float(secs))
        except (TypeError, ValueError):
            return "--:--"
        if s < 0:
            return "--:--"
        return f"{s // 60}:{s % 60:02d}"

    def _run_js(self, js: str):
        try:
            self._page.runJavaScript(js)
        except Exception as exc:
            if DEBUG:
                print(f"[player] js err: {exc}", flush=True)

    def refresh_state(self):
        try:
            self._page.runJavaScript(self._STATE_JS, self._on_state)
        except Exception:
            pass

    def _on_state(self, raw):
        import json as _json
        try:
            st = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            return
        paused = bool(st.get("paused", True))
        self._buttons["playpause"].setText("▶" if paused else "⏸")
        if DEBUG and not bool(st.get("hasMedia", False)) and not paused and not self._no_media_warned:
            self._no_media_warned = True
            print("[player] playing but no audio/video in light DOM, deep-scanning...", flush=True)
            try:
                self._page.runJavaScript(self._DEEP_SCAN_JS, self._on_deepscan)
            except Exception:
                pass
        if bool(st.get("hasMedia", False)):
            self._no_media_warned = False
        dur = float(st.get("dur") or 0)
        cur = max(0.0, float(st.get("cur") or 0))
        if dur <= 0:
            # No media element (or MSE Infinity): mirror the web player's
            # own progress input if it exists.
            try:
                pv = float(st.get("pv", -1))
                pm = float(st.get("pm") or 0)
            except (TypeError, ValueError):
                pv, pm = -1, 0
            if pv >= 0 and pm > 0 and not self.progress.isSliderDown():
                self.progress.setValue(int(pv / pm * 1000))
            self.dur_label.setText("--:--")
            self.cur_label.setText("--:--")
        else:
            self.dur_label.setText(self._fmt(dur) if dur > 0 else "--:--")
            self.cur_label.setText(self._fmt(cur))
            if not self.progress.isSliderDown():
                self.progress.setValue(int(cur / dur * 1000))
        vol = st.get("vol", None)
        if vol is not None and not self._vol_set and not self.volume.isSliderDown():
            try:
                self.volume.setValue(int(float(vol) * 100))
            except (TypeError, ValueError):
                pass

    def _on_seek(self):
        frac = self.progress.value() / 1000
        self._page.runJavaScript(
            f"(() => {{ const els = document.querySelectorAll('audio,video');"
            f" const a = els[0]; let cur = -1;"
            f" if (a) {{ let d = (isFinite(a.duration) && a.duration > 0) ? a.duration : 0;"
            f" if (!(d > 0) && a.seekable && a.seekable.length)"
            f" {{ try {{ d = a.seekable.end(a.seekable.length - 1); }} catch (e) {{}} }}"
            f" if (d > 0) {{ try {{ a.currentTime = d * {frac}; }} catch (e) {{}} cur = a.currentTime; }} }}"
            f" const set = {self._SET_INPUT_JS};"
            f" const pr = document.querySelector('input[type=\"range\"][aria-label*=\"eek\" i],input[type=\"range\"][aria-label*=\"rogress\" i],input[type=\"range\"][aria-label*=\"osition\" i]');"
            f" let web = false;"
            f" if (pr) {{ const mx = parseFloat(pr.max || '100') || 100; set(pr, {frac} * mx); web = true; }}"
            f" return JSON.stringify({{n: els.length, cur: cur, web: web}}); }})()",
            self._on_seek_result)

    def _on_seek_result(self, raw):
        if DEBUG:
            print(f"[player] seek -> {raw}", flush=True)

    def _on_deepscan(self, raw):
        print(f"[player] deepscan -> {raw}", flush=True)

    def _on_volume(self):
        self._vol_set = True
        self._page.runJavaScript(
            f"(() => {{ const v = {self.volume.value()} / 100;"
            f" const els = document.querySelectorAll('audio,video');"
            f" els.forEach(a => {{ try {{ a.volume = v; if (v > 0) a.muted = false; }} catch (e) {{}} }});"
            f" const set = {self._SET_INPUT_JS};"
            f" const vi = document.querySelector('input[type=\"range\"][aria-label*=\"olume\" i]');"
            f" let web = false;"
            f" if (vi) {{ const mx = parseFloat(vi.max || '100') || 100; set(vi, v * mx); web = true; }}"
            f" return JSON.stringify({{n: els.length, v: v, web: web}}); }})()",
            self._on_volume_result)

    def _on_volume_result(self, raw):
        if DEBUG:
            print(f"[player] volume -> {raw}", flush=True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(12, 12, 14, int(_TB_ALPHA * 255)))
        super().paintEvent(event)

    # NOTE: deliberately no drag-to-move here (titlebar owns that). The bar
    # never grabs pointer input, so sliders/buttons can't turn into moves.


TRANSCRIBE_CACHE_DIR = os.path.expanduser("~/.cache/SpotifyTransparent/transcribe")
TRANSCRIBE_MODEL = os.environ.get("SPOTIFY_WHISPER_MODEL", "base")
TRANSCRIBE_ENV = os.path.expanduser(
    "~/.local/share/SpotifyTransparent/transcribe-env")
_TRANSCRIBE_PATH_ADDED = False


def transcribe_cache_key(title: str, artist: str) -> str:
    import hashlib
    return hashlib.sha1(f"{title}\x00{artist}".lower().encode()).hexdigest()


class TranscribeWorker(QThread):
    """Auto-generate lyrics by transcription: finds the track on YouTube,
    downloads audio (yt-dlp), transcribes locally with faster-whisper
    (timestamped segments -> synced lines), caches the JSON. Runs fully
    off the UI thread; reports via signals + window bridge vars."""

    status = pyqtSignal(str)          # human-readable progress
    done = pyqtSignal(str, list, str, str)  # (key, lines, video_id, video_title)
    failed = pyqtSignal(str)          # reason (missing deps, no match...)

    def __init__(self, title, artist, duration, cache_key, excluded=None):
        super().__init__()
        self.title = title
        self.artist = artist
        self.duration = duration
        self.cache_key = cache_key
        self.excluded = set(excluded or [])

    BAD_UPLOAD_HINTS = (
        "cover", "remix", "sped up", "slowed", "nightcore", "8d audio",
        "instrumental", "karaoke", "reverb", "flip", " acoustic",
        "live ", "concert", "reaction", "review", "interview",
    )

    @classmethod
    def score_candidate(cls, title, artist, dur, vid_title, vid_dur):
        """Higher = more likely the actual studio track. Title word
        overlap dominates; altered-version uploads penalized; duration
        is a tiebreak with a hard 25s reject."""
        import re
        tw = {w for w in re.split(r"\W+", (title + " " + artist).lower())
              if len(w) > 2}
        vw = set(re.split(r"\W+", (vid_title or "").lower()))
        overlap = len(tw & vw)
        low = (vid_title or "").lower()
        penalty = sum(1 for b in cls.BAD_UPLOAD_HINTS if b in low)
        durdiff = abs(vid_dur - dur) if dur else 0
        return overlap * 10 - penalty * 8 - min(durdiff, 60) / 6, overlap

    @staticmethod
    def _bin(name: str) -> str | None:
        import shutil
        for cand in (os.path.join(TRANSCRIBE_ENV, "bin", name),):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        found = shutil.which(name)
        if found:
            return found
        local = os.path.expanduser(f"~/.local/bin/{name}")
        return local if os.path.isfile(local) and os.access(local, os.X_OK) else None

    @staticmethod
    def _use_env():
        """Put the transcribe venv's site-packages on sys.path so its
        faster-whisper / imageio-ffmpeg import in-process."""
        global _TRANSCRIBE_PATH_ADDED
        if _TRANSCRIBE_PATH_ADDED:
            return
        import glob
        for sp in glob.glob(os.path.join(TRANSCRIBE_ENV, "lib",
                                         "python3*", "site-packages")):
            if os.path.isdir(sp) and sp not in sys.path:
                sys.path.insert(0, sp)
        _TRANSCRIBE_PATH_ADDED = True

    @staticmethod
    def _ffmpeg_exe() -> str | None:
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            return exe if os.path.isfile(exe) else None
        except Exception:
            return None

    @classmethod
    def ensure_deps(cls, status_cb) -> str | None:
        """Isolated venv for transcriber deps (PEP 668-clean, no sudo,
        no system pollution). Returns None when ready, else a reason."""
        import subprocess
        cls._use_env()
        try:
            import faster_whisper  # noqa: F401
            have_fw = True
        except ImportError:
            have_fw = False
        if have_fw and cls._bin("yt-dlp") and (
                cls._bin("ffmpeg") or cls._ffmpeg_exe()):
            return None
        venv_pip = os.path.join(TRANSCRIBE_ENV, "bin", "pip")
        if not os.path.isfile(venv_pip):
            status_cb("Creating isolated transcription env (one-time)…")
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "venv", TRANSCRIBE_ENV],
                    capture_output=True, text=True, timeout=300)
            except Exception as e:
                return f"Could not create virtualenv: {e}"
            if proc.returncode != 0 or not os.path.isfile(venv_pip):
                tail = (proc.stderr or proc.stdout or "")[-400:]
                return ("Virtualenv failed — try: sudo pacman -S "
                        f"python-virtualenv. {tail}")
        status_cb("Installing transcription tools into isolated env…")
        try:
            proc = subprocess.run(
                [venv_pip, "install", "yt-dlp", "faster-whisper",
                 "imageio-ffmpeg"],
                capture_output=True, text=True, timeout=1200)
        except Exception as e:
            return f"Installer failed: {e}"
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-400:]
            return f"Installer failed. {tail}"
        global _TRANSCRIBE_PATH_ADDED
        _TRANSCRIBE_PATH_ADDED = False
        cls._use_env()
        # Re-verify.
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return "faster-whisper still missing after install."
        if cls._bin("yt-dlp") is None:
            return "yt-dlp still missing after install."
        if cls._bin("ffmpeg") is None and cls._ffmpeg_exe() is None:
            return "ffmpeg still missing after install."
        return None

    def run(self):
        import json as _json
        import shutil
        import subprocess
        import tempfile
        try:
            # 0. One-time user-space install (no sudo).
            missing = self.ensure_deps(self.status.emit)
            if missing:
                self.failed.emit(missing)
                return
            ytdlp = self._bin("yt-dlp")
            env = dict(os.environ)
            env["PATH"] = (os.path.join(TRANSCRIBE_ENV, "bin") +
                           os.pathsep +
                           os.path.expanduser("~/.local/bin") +
                           os.pathsep + env.get("PATH", ""))
            ff = self._bin("ffmpeg") or self._ffmpeg_exe()
            # 1. Search YouTube, score candidates (title overlap beats
            # duration; covers/remixes penalized; tried IDs excluded).
            self.status.emit("Searching YouTube…")
            query = f"{self.title} {self.artist} audio"
            proc = subprocess.run(
                [ytdlp, f"ytsearch10:{query}",
                 "--print", "%(id)s\t%(duration)s\t%(title)s",
                 "--no-download", "--quiet", "--no-warnings"],
                capture_output=True, text=True, timeout=120,
                env=env)
            cands = []
            for line in (proc.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) < 3 or parts[0] in self.excluded:
                    continue
                try:
                    cands.append((parts[0], float(parts[1]), parts[2]))
                except ValueError:
                    pass
            if not cands:
                self.failed.emit("YouTube found nothing new for this track.")
                return
            target = self.duration or 0
            scored = [(self.score_candidate(self.title, self.artist, target,
                                            t, d), (v, d, t))
                      for (v, d, t) in cands]
            scored.sort(key=lambda s: (s[0][0], -abs(s[1][1] - target)),
                        reverse=True)
            (score, overlap), (vid, vdur, vtitle) = scored[0]
            if overlap < 1:
                self.failed.emit(
                    "No YouTube result actually names this track — skipping.")
                return
            if target and abs(vdur - target) > 25:
                self.failed.emit(
                    f"Closest YouTube match is {vdur:.0f}s vs {target:.0f}s — skipping.")
                return
            # 2. Download audio.
            self.status.emit(f"Downloading audio ({vtitle[:40]}…)…")
            tmp = tempfile.mkdtemp(prefix="pytrans_")
            out = os.path.join(tmp, "audio.%(ext)s")
            dl_cmd = [ytdlp, vid, "-x", "--audio-format", "mp3",
                      "--audio-quality", "0", "-o", out,
                      "--quiet", "--no-warnings", "--no-playlist"]
            if ff:
                dl_cmd += ["--ffmpeg-location", ff]
            proc = subprocess.run(
                dl_cmd,
                capture_output=True, text=True, timeout=600,
                env=env)
            mp3 = os.path.join(tmp, "audio.mp3")
            if not os.path.isfile(mp3):
                tail = (proc.stderr or "")[-300:]
                self.failed.emit(f"Audio download failed. {tail}")
                return
            # 3. Transcribe locally (timestamps -> synced lines).
            self.status.emit("Transcribing (local Whisper AI, minutes)…")
            lines = transcribe_mp3(mp3)
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass
            if not lines:
                self.failed.emit("Transcription came back empty.")
                return
            os.makedirs(TRANSCRIBE_CACHE_DIR, exist_ok=True)
            with open(os.path.join(TRANSCRIBE_CACHE_DIR,
                                   self.cache_key + ".json"), "w") as f:
                _json.dump({"video_id": vid, "video_title": vtitle,
                            "lines": lines}, f)
            self.done.emit(self.cache_key, lines, vid, vtitle)
        except Exception as e:
            self.failed.emit(f"Transcription error: {e}")


def transcribe_mp3(mp3_path: str) -> list:
    """Local Whisper transcription -> [{t, x}] synced lines."""
    from faster_whisper import WhisperModel
    model = WhisperModel(TRANSCRIBE_MODEL, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(mp3_path, beam_size=5)
    return [{"t": float(s.start), "x": s.text.strip()}
            for s in segments if s.text and s.text.strip()]


class RecordWorker(QThread):
    """Transcribe the exact song by capturing live playback: seeks to the
    start (via page bridge), records the PipeWire monitor for the track
    duration, transcribes. Guaranteed the right song (it's what's playing)
    at the cost of one real-time listen with the volume up."""

    status = pyqtSignal(str)
    done = pyqtSignal(str, list, str, str)  # (key, lines, 'live', monitor)
    failed = pyqtSignal(str)

    def __init__(self, title, artist, duration, cache_key):
        super().__init__()
        self.title = title
        self.artist = artist
        self.duration = duration
        self.cache_key = cache_key

    def run(self):
        import json as _json
        import shutil
        import subprocess
        import tempfile
        import time
        try:
            missing = TranscribeWorker.ensure_deps(self.status.emit)
            if missing:
                self.failed.emit(missing)
                return
            ff = TranscribeWorker._bin("ffmpeg") or TranscribeWorker._ffmpeg_exe()
            if not ff:
                self.failed.emit("No ffmpeg available for recording.")
                return
            # Default sink's monitor source.
            proc = subprocess.run(["pactl", "get-default-sink"],
                                  capture_output=True, text=True, timeout=15)
            sink = (proc.stdout or "").strip().split("\n")[0].strip()
            if not sink:
                self.failed.emit("Could not find the audio output (pactl).")
                return
            monitor = sink + ".monitor"
            dur = int(self.duration or 0)
            if dur <= 0:
                self.failed.emit("Unknown track length — play the song first.")
                return
            # The page was asked to seek to 0 just before starting us;
            # Python only starts us once position reads ~0:00, so a
            # token settle delay suffices.
            self.status.emit("Recording live playback — keep volume up…")
            time.sleep(1)
            tmp = tempfile.mkdtemp(prefix="pyrec_")
            mp3 = os.path.join(tmp, "rec.mp3")
            proc = subprocess.run(
                [ff, "-y", "-f", "pulse", "-i", monitor,
                 "-t", str(dur + 5), "-ac", "1", "-ar", "16000", mp3],
                capture_output=True, text=True,
                timeout=dur + 180)
            if not os.path.isfile(mp3) or os.path.getsize(mp3) < 10000:
                tail = (proc.stderr or "")[-300:]
                self.failed.emit(f"Recording failed (is audio playing?). {tail}")
                return
            self.status.emit("Transcribing recording (local Whisper AI)…")
            lines = transcribe_mp3(mp3)
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass
            if not lines:
                self.failed.emit(
                    "Transcription empty — volume may have been muted.")
                return
            os.makedirs(TRANSCRIBE_CACHE_DIR, exist_ok=True)
            with open(os.path.join(TRANSCRIBE_CACHE_DIR,
                                   self.cache_key + ".json"), "w") as f:
                _json.dump({"video_id": "live-record",
                            "video_title": "live playback capture",
                            "lines": lines}, f)
            self.done.emit(self.cache_key, lines, "live-record",
                           "live playback capture")
        except Exception as e:
            self.failed.emit(f"Recording error: {e}")


class SpotifyWindow(QMainWindow):
    def __init__(self, no_adblock=False, opaque=False):
        super().__init__()
        self._opaque = opaque

        self.setWindowTitle("Spotify (Transparent)")
        self.resize(1000, 700)

        # --- Window-level transparency (real, OS-composited alpha) ---
        # NOTE: on Wayland, the compositor must have Blur/compositing
        # enabled, otherwise transparent just looks black.
        if not opaque:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window
            )

        central = QWidget()
        central.setStyleSheet("background: transparent;" if not opaque else "")
        self.setCentralWidget(central)
        self._central = central

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

        # Username/password login needs JS + DOM storage + persistent
        # cookies across accounts.spotify.com -> open.spotify.com.
        # These are defaults, but set explicitly so a distro Qt build
        # with odd defaults can't silently break login.
        settings = self.profile.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptEnabled, True
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalStorageEnabled, True
        )

        self.profile.scripts().insert(build_inject_script(INJECTED_CSS))
        # Page-level ad skipper (MainWorld: needs page-context webpack).
        self.profile.scripts().insert(build_adskip_script())
        # In-page lyrics panel (MainWorld: fetch + DOM).
        self.profile.scripts().insert(build_lyrics_script())

        # --- Ad blocker (skippable via --no-adblock for login tests) ---
        if not no_adblock:
            self.ad_interceptor = AdBlockInterceptor(self)
            self.profile.setUrlRequestInterceptor(self.ad_interceptor)
        elif DEBUG:
            print("[adblock] disabled via --no-adblock", flush=True)

        # MainPage (not plain QWebEnginePage) so "Continue with Google/
        # Facebook/Apple" login popups actually open instead of failing
        # silently.
        self.page = MainPage(self.profile, self)
        # Make the web page itself render with a transparent base color,
        # not just the Qt window around it -- this is what lets the
        # injected "transparent" CSS actually show your desktop through,
        # rather than showing Chromium's default white/black canvas.
        if not self._opaque:
            self.page.setBackgroundColor(QColor(0, 0, 0, 0))

        self.view = QWebEngineView()
        self.view.setPage(self.page)
        if not self._opaque:
            self.view.setStyleSheet("background: transparent;")
            self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            # Don't let the view paint an opaque background before the page loads.
            try:
                self.view.page().setBackgroundColor(QColor(0, 0, 0, 0))
            except Exception:
                pass
        outer.addWidget(self.view)

        if DEBUG:
            self.page.loadFinished.connect(
                lambda ok, url=self.view: print(
                    f"[load] ok={ok} url={url.url().toString()}", flush=True
                )
            )

        self.page.load(QUrl(SPOTIFY_URL))

        # Seed saved lyrics keys into the page once loaded.
        self.page.loadFinished.connect(lambda _ok: self._seed_lyrics_keys())

        # Clipboard bridge pump: the MainWorld script stashes every
        # programmatic copy in window.__pyClipboardPending; mirror it
        # onto the real system clipboard (see _pump_clipboard).
        self._last_clip = ""
        self._clip_timer = QTimer(self)
        self._clip_timer.timeout.connect(self._pump_clipboard)
        self._clip_timer.start(500)

        # Transcription bridge pump (1s): page requests via
        # window.__pyTranscribeReq; results/status go back through
        # window.__pyTranscribeResult / __pyTranscribeStatus.
        self._transcriber = None
        self._trans_req_seen = ""
        self._seek_wait = None
        self._trans_timer = QTimer(self)
        self._trans_timer.timeout.connect(self._pump_transcribe)
        self._trans_timer.start(1000)

        # SPOTIFY_DUMP=1: one-shot footer/art diagnostic (~12s after load),
        # printed as [dump] lines for pasting into a bug report.
        if os.environ.get("SPOTIFY_DUMP") == "1":
            QTimer.singleShot(12000, self._dump_footer)

        if _NATIVE_BAR and not self._opaque:
            # Native player bar (see BottomBar). Web footer stays hidden.
            self.bottombar = BottomBar(self, self.page)
            outer.addWidget(self.bottombar)
            if DEBUG:
                print("[debug] native bottom bar on (SPOTIFY_NATIVE_BAR=0 to hide)", flush=True)
        else:
            # Floating corner grip (no layout row, so no empty strip under
            # the player bar). Positioned in resizeEvent.
            self._grip = CornerGrip(self)
            self._grip.setParent(central)
            self._grip.raise_()
            self._grip.show()

    def _pump_transcribe(self):
        try:
            if self._seek_wait:
                self.page.runJavaScript(
                    "(function(){try{var b=document.querySelector("
                    "'[data-testid=\"progress-bar\"]');var m=((b&&b.getAttribute"
                    "('style'))||'').match(/--progress-bar-transform:\\s*"
                    "([\\d.]+)%/);var st='';try{st=navigator.mediaSession?"
                    "navigator.mediaSession.playbackState:'';}catch(e){}"
                    "return (m?m[1]:'')+'|'+st;}catch(e){return '';}})()",
                    self._on_seek_pos)
                return
            self.page.runJavaScript(
                "window.__pyTranscribeReq || ''", self._on_trans_req)
        except Exception:
            pass

    def _on_seek_pos(self, frac):
        """Start the recorder only once playback is back near 0:00 AND
        actually playing (a paused seek reads 0% but records silence)."""
        wait = self._seek_wait
        if not wait:
            return
        try:
            parts = str(frac).split("|")
            ok = (parts[0] != "" and float(parts[0]) < 3.0
                  and len(parts) > 1 and parts[1] == "playing")
        except (TypeError, ValueError):
            ok = False
        import time as _t
        if ok or _t.time() > wait["deadline"]:
            self._seek_wait = None
            if not ok and DEBUG:
                print("[lyrics] seek unverified, recording anyway",
                      flush=True)
            self._start_record_worker(wait)

    def _start_record_worker(self, wait):
        self._transcriber = RecordWorker(wait["title"], wait["artist"],
                                        wait["dur"], wait["key"])
        self._transcriber.status.connect(self._set_trans_status)
        self._transcriber.done.connect(
            lambda k, lines, _v, _t:
            self._deliver_trans_result(k, lines,
                                       "Transcribed · live recording",
                                       _v, _t))
        self._transcriber.failed.connect(
            lambda msg: self._deliver_trans_error(wait["key"], msg))
        self._transcriber.start()

    def _on_trans_req(self, text):
        if not text or text == self._trans_req_seen:
            return
        self._trans_req_seen = text
        try:
            req = json.loads(text)
            title, artist = req.get("title", ""), req.get("artist", "")
            dur = float(req.get("dur") or 0)
            key = req.get("key") or transcribe_cache_key(title, artist)
            mode = req.get("mode") or "youtube"
            excluded = [str(x) for x in (req.get("exclude") or [])]
        except Exception:
            return
        if not title:
            return
        if self._transcriber is not None and self._transcriber.isRunning():
            self._set_trans_status("Already transcribing — wait for it…")
            return
        # Cached result first (instant), unless retrying past it.
        if mode == "youtube":
            try:
                with open(os.path.join(TRANSCRIBE_CACHE_DIR, key + ".json")) as f:
                    cached = json.load(f)
                if isinstance(cached, list):
                    lines, vid, vtitle = cached, "", ""
                else:
                    lines, vid = cached.get("lines", []), cached.get("video_id", "")
                    vtitle = cached.get("video_title", "")
                if lines and vid not in excluded:
                    self._deliver_trans_result(
                        key, lines, "Transcribed · Whisper (cached)",
                        vid, vtitle)
                    return
            except Exception:
                pass
        else:
            # Live recording: resume if paused, restart the song via page
            # bridge, lock the player UI, then only start the recorder
            # once position reads ~0:00 while playing (blind sleeps miss
            # the head of the song; a paused seek would record silence).
            try:
                self.page.runJavaScript(
                    "window.__pyPlayReq = true;"
                    "window.__pySeekReq = {ms: 0, at: Date.now()};"
                    "window.__pyUiLock = true;")
            except Exception:
                pass
            import time as _t
            self._seek_wait = {"title": title, "artist": artist,
                               "dur": dur, "key": key,
                               "deadline": _t.time() + 15}
            self._set_trans_status("Restarting song…")
            return
        # The worker self-bootstraps its isolated venv on first run and
        # reports progress/failure back through the bridge.
        if mode == "record":
            self._transcriber = RecordWorker(title, artist, dur, key)
            label = "Transcribed · live recording"
        else:
            self._transcriber = TranscribeWorker(title, artist, dur, key,
                                                excluded)
            label = "Transcribed · Whisper (auto)"
        self._transcriber.status.connect(self._set_trans_status)
        self._transcriber.done.connect(
            lambda k, lines, _v, _t, label=label:
            self._deliver_trans_result(k, lines, label, _v, _t))
        self._transcriber.failed.connect(
            lambda msg: self._deliver_trans_error(key, msg))
        self._transcriber.start()
        self._set_trans_status("Starting transcription…")

    def _set_trans_status(self, s):
        try:
            self.page.runJavaScript(
                "window.__pyTranscribeStatus = " + json.dumps(str(s)))
        except Exception:
            pass

    def _deliver_trans_result(self, key, lines, label, vid="", vtitle=""):
        try:
            payload = json.dumps({"key": key, "lines": lines, "label": label,
                                  "video_id": vid, "video_title": vtitle})
            self.page.runJavaScript(
                "window.__pyTranscribeResult = " + payload + ";"
                "window.__pyTranscribeReq = '';"
                "window.__pyUiLock = false;")
            self._trans_req_seen = ""
            if DEBUG:
                print(f"[lyrics] transcribed {len(lines)} lines", flush=True)
        except Exception:
            pass

    def _deliver_trans_error(self, key, msg):
        try:
            payload = json.dumps({"key": key, "error": str(msg)})
            self.page.runJavaScript(
                "window.__pyTranscribeResult = " + payload + ";"
                "window.__pyTranscribeReq = '';"
                "window.__pyUiLock = false;")
            self._trans_req_seen = ""
            if DEBUG:
                print(f"[lyrics] transcribe failed: {msg}", flush=True)
        except Exception:
            pass

    def _pump_clipboard(self):
        try:
            self.page.runJavaScript(
                "window.__pyClipboardPending || ''", self._on_clip_text)
            self.page.runJavaScript(
                "window.__pyLyricsKeysPending || ''", self._on_keys_text)
        except Exception:
            pass

    def _on_clip_text(self, text):
        if not text or text == self._last_clip:
            return
        self._last_clip = text
        # SpicyTracker parity: strip ?si=/&si= tracking from Spotify
        # share links even when they travel via this bridge.
        try:
            cleaned = re.sub(r"\?si=[^&\s]*&", "?", text)
            cleaned = re.sub(r"[?&]si=[^&\s]*", "", cleaned)
        except Exception:
            cleaned = text
        try:
            QApplication.clipboard().setText(cleaned)
            self.page.runJavaScript("window.__pyClipboardPending = ''")
            if DEBUG:
                print(f"[clipboard] bridged {len(cleaned)} chars", flush=True)
        except Exception:
            pass

    def _on_keys_text(self, text):
        """Persist the lyrics panel config (keys + provider order)."""
        if not text:
            return
        try:
            cfg = json.loads(text)
            if not isinstance(cfg, dict):
                return
            if "keys" in cfg:  # new shape from settings view
                keys = cfg.get("keys") or {}
                data = {
                    "keys": {k: str(keys.get(k, "")) for k in ("mm", "vag", "gen")},
                    "order": [str(x) for x in (cfg.get("order") or [])][:16],
                    "off": {str(k): True for k in (cfg.get("off") or {})},
                }
            else:  # legacy flat keys
                data = {k: str(cfg.get(k, "")) for k in ("mm", "vag", "gen")}
        except Exception:
            return
        try:
            os.makedirs(LYRICS_CONFIG_DIR, exist_ok=True)
            with open(LYRICS_KEYS_PATH, "w") as f:
                json.dump(data, f)
            self.page.runJavaScript("window.__pyLyricsKeysPending = ''")
            if DEBUG:
                print("[lyrics] config saved", flush=True)
        except Exception:
            pass

    def _seed_lyrics_keys(self):
        """Push the saved lyrics config into the page on startup so
        providers and order apply without env vars."""
        try:
            with open(LYRICS_KEYS_PATH) as f:
                data = json.load(f)
        except Exception:
            return
        try:
            if isinstance(data, dict) and "keys" in data:
                payload = json.dumps(data)
            else:  # legacy flat keys
                keys = data if isinstance(data, dict) else {}
                payload = json.dumps({
                    "keys": {k: str(keys.get(k, "")) for k in ("mm", "vag", "gen")},
                    "order": [],
                    "off": {},
                })
            js = ("try{localStorage.setItem('py-lyr-cfg'," +
                  json.dumps(payload) + ")}catch(e){}")
            self.page.runJavaScript(js)
            if DEBUG:
                print("[lyrics] config seeded", flush=True)
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        grip = getattr(self, "_grip", None)
        if grip is not None:
            m = 4
            grip.move(
                self._central.width() - grip.width() - m,
                self._central.height() - grip.height() - m,
            )

    def _dump_footer(self):
        try:
            self.page.runJavaScript(_DUMP_JS, self._on_dump)
        except Exception as exc:
            print(f"[dump] err: {exc}", flush=True)

    def _on_dump(self, raw):
        import json as _json
        try:
            data = _json.loads(raw) if isinstance(raw, str) else {}
        except Exception:
            print(f"[dump] raw: {raw}", flush=True)
            return
        for key in ("footerFound", "footerRect", "viewport", "footerCSS",
                    "parentCSS", "parents", "imgFound", "imgRect",
                    "imgParentRect", "imgCSS", "npFound", "npRect", "npCSS",
                    "npParentCSS", "npParents", "npImgFound", "npImgRect",
                    "npImgParentRect", "npImgCSS", "controls"):
            if key in data:
                print(f"[dump] {key}={_json.dumps(data[key])}", flush=True)
        for key in ("imgHTML", "npImgHTML", "html"):
            if data.get(key):
                print(f"[dump] {key}={data[key][:600]}", flush=True)


def _find_widevine():
    """Locate libwidevinecdm.so for EME audio. QtWebEngine 6.4+ auto-finds
    /usr/lib/chromium/libwidevinecdm.so and Chrome/Firefox component dirs;
    this covers the rest (explicit --widevine-path). Returns path or None."""
    import glob as _glob
    for p in (
        "/usr/lib/chromium/libwidevinecdm.so",
        "/usr/lib/chromium-browser/libwidevinecdm.so",
        "/usr/lib64/chromium/libwidevinecdm.so",
        "/opt/google/chrome/libwidevinecdm.so",
        "/opt/google/chrome/WidevineCdm/_platform_specific/linux_x64/libwidevinecdm.so",
    ):
        if os.path.isfile(p):
            return p
    home = os.path.expanduser("~")
    found = []
    for pat in (
        f"{home}/.config/google-chrome/WidevineCdm/*/_platform_specific/linux_x64/libwidevinecdm.so",
        f"{home}/.config/google-chrome/WidevineCdm/*/libwidevinecdm.so",
        f"{home}/.config/chromium/WidevineCdm/*/_platform_specific/linux_x64/libwidevinecdm.so",
        f"{home}/.config/chromium/WidevineCdm/*/libwidevinecdm.so",
        f"{home}/.mozilla/firefox/*/gmp-widevinecdm/*/libwidevinecdm.so",
    ):
        found.extend(_glob.glob(pat))
    found = sorted(p for p in found if os.path.isfile(p))
    return found[-1] if found else None


def main():
    parser = argparse.ArgumentParser(description="Transparent Spotify wrapper")
    parser.add_argument("--debug", action="store_true", help="verbose JS/network logging")
    parser.add_argument("--no-adblock", action="store_true", help="disable ad blocker (login test)")
    parser.add_argument(
        "--opaque", action="store_true",
        help="disable transparency (compare: if opaque works but transparent shows black, it's the compositor/GPU path)",
    )
    args, qt_args = parser.parse_known_args()

    # Transparent QWebEngineView on Wayland/Vulkan (radv) needs the
    # Chromium transparent-visuals path. Must be set BEFORE QApplication.
    # Overridable: QTWEBENGINE_CHROMIUM_FLAGS="..." ./run.sh
    # If you still get solid black, try:
    #   QTWEBENGINE_CHROMIUM_FLAGS="--enable-transparent-visuals --disable-gpu-compositing" ./run.sh
    # (slower, but proves whether GPU compositing was hiding the alpha).
    existing_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--enable-transparent-visuals" not in existing_flags and not args.opaque:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            f"{existing_flags} --enable-transparent-visuals".strip()
        )

    # Widevine CDM for audio: Spotify streams are EME-encrypted, without the
    # CDM every track fails with "EMEError: No supported keysystem".
    # Must be set BEFORE QApplication. Override: SPOTIFY_WIDEVINE_PATH=/path/to/libwidevinecdm.so
    _wv_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    _widevine_used = None
    if "--widevine-path" not in _wv_flags:
        _widevine_used = os.environ.get("SPOTIFY_WIDEVINE_PATH") or _find_widevine()
        if _widevine_used and os.path.isfile(_widevine_used):
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
                f"{_wv_flags} --widevine-path={_widevine_used}".strip()
            )
        else:
            _widevine_used = None
    if _widevine_used is None and "--widevine-path" not in os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""):
        print("[widevine] no libwidevinecdm.so found - audio will fail with EMEError.", flush=True)
        print("  CachyOS/Arch: yay -S chromium-widevine  (or google-chrome), then relaunch.", flush=True)
        print("  Verify at https://bitmovin.com/demos/drm", flush=True)

    global DEBUG
    if args.debug:
        DEBUG = True
        os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = os.environ.get(
            "QTWEBENGINE_REMOTE_DEBUGGING", "9223"
        )
        print(
            f"[debug] remote debugging at http://localhost:{os.environ['QTWEBENGINE_REMOTE_DEBUGGING']}",
            flush=True,
        )

    # Re-exec QApplication with only Qt args (strip our own flags).
    app = QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName(APP_NAME)
    # Match spotify-transparent.desktop so the taskbar groups the window
    # with our pinned launcher + monochrome icon (else it shows as
    # generic "python"). Sets the Wayland app_id / X11 WM_CLASS.
    app.setDesktopFileName("spotify-transparent")

    if DEBUG:
        from PyQt6.QtCore import QT_VERSION_STR, qVersion

        try:
            from PyQt6.QtWebEngineCore import qWebEngineChromiumVersion

            chromium = qWebEngineChromiumVersion()
        except Exception:
            chromium = "<unknown>"
        print(f"[debug] Qt={QT_VERSION_STR} runtime={qVersion()} Chromium={chromium}", flush=True)
        print(f"[debug] UA={DESKTOP_CHROME_UA}", flush=True)
        print(f"[debug] profile={PROFILE_DIR}", flush=True)
        print(f"[debug] alpha={_BG_ALPHA} panel={_PANEL_BG} titlebar={_TB_ALPHA}", flush=True)
        print(f"[debug] chromium_flags={os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')}", flush=True)

    window = SpotifyWindow(no_adblock=args.no_adblock, opaque=args.opaque)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
