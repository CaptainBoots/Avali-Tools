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
    from PyQt6.QtCore import Qt, QUrl, QTimer
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
/* Bottom player bar: stock solid fill, copied from the official client.
   The web footer's own buttons/sliders are used as-is (no JS bridging),
   so everything works. Solid (not translucent) on purpose: translucent
   layers mis-composite here and read as a broken clear strip.
   Pinned fixed: the root is display:block, so sticky/margin anchoring
   can't hold the bar above the fold at small heights. */
footer,
[data-testid="now-playing-bar"],
[data-testid*="now-playing"],
[data-testid*="player"],
div[class*="now-playing"],
div[class*="player"],
div[class*="Player"],
div[class*="playbar"],
div[class*="Playback"] {{
    background-color: #0a0a0c !important;
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
.Root div:not([data-testid="root"]):not([data-testid="topbar"]):not([data-testid="now-playing-bar"]):not([data-testid*="now-playing"]):not([data-testid*="player"]):not([data-testid="left-sidebar"]):not([data-testid="main-view"]):not([data-testid*="right-sidebar"]):not([data-testid*="progress"]):not([data-testid*="volume"]):not([class*="Root__"]):not([class*="main-view"]):not([class*="nav-bar"]):not([class*="now-playing"]):not([class*="player"]):not([class*="Player"]):not([class*="playbar"]):not([class*="Playback"]):not([class*="progress" i]):not([class*="slider" i]):not([class*="volume" i]):not([class*="right-sidebar"]):not([class*="RightSidebar"]):not([class*="top-bar"]):not([class*="YourLibrary"]):not([class*="card"]):not([class*="Card"]):not([role="slider"]),
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
                    var t = (nodes[i].innerText || '').trim();
                    if (!t || t.length > 60) continue;
                    var low = t.toLowerCase();
                    if (t === 'Install App' || t === 'Download the free app' ||
                        t === 'Browse' ||
                        low === 'go premium' || low === 'get premium' ||
                        low === 'try premium free' || low === 'start a free trial' ||
                        low === 'get 3 months free' || low.indexOf('premium free') !== -1) {{
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
            if (_tries++ > 120) {{ log('ListPlayer not found after 120 tries, DOM fallback only'); return false; }}
            var req = getReq();
            if (!req) return false;
            var cls = findListPlayer(req);
            if (!cls) return false;
            _lpClass = cls;
            try {{
                var origLoad = cls.prototype.load;
                cls.prototype.load = function(list) {{
                    try {{ _lp = this; }} catch (e) {{}}
                    // Pre-mute synchronously when the incoming track is an
                    // ad: the load hook fires before playback starts, so
                    // this kills the 1-2s audible blip before the skip
                    // lands. Interval below unmutes + advances.
                    try {{
                        var tr = (list && list._tracks && list._tracks[0]) ||
                                 (list && list.tracks && list.tracks[0]) || null;
                        var u = (tr && tr.uri) || '';
                        if (u.indexOf('spotify:ad:') !== -1 ||
                            u.indexOf(':ad:') !== -1) {{
                            setMediaMuted(true); log('pre-muted ad at load');
                        }}
                    }} catch (e) {{}}
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
        function setMediaMuted(muted) {{
            try {{
                var els = document.querySelectorAll('audio,video');
                for (var i = 0; i < els.length; i++) {{
                    try {{
                        if (els[i].muted !== muted) els[i].muted = muted;
                    }} catch (x) {{}}
                }}
                _mutedByUs = muted;
            }} catch (e) {{}}
        }}
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
        var _skipCool = 0;
        setInterval(function() {{
            try {{
                if (!_lpClass) hookPlayer();
                var ad = isAdTrack() || (!_lp && domAdSuspect());
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
                    if (!_mutedByUs) {{ setMediaMuted(true); log('muted ad media'); }}
                }} else if (_mutedByUs) {{
                    setMediaMuted(false); log('unmuted (music back)');
                }}
            }} catch (e) {{}}
        }}, 150);
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

        self.setAutoFillBackground(True)
        _tb_a = int(_TB_ALPHA * 255)
        pal = self.palette()
        pal.setColor(pal.ColorRole.Window, QColor(12, 12, 14, _tb_a))
        self.setPalette(pal)
        self.setStyleSheet(
            f"TitleBar {{ background-color: rgba(12, 12, 14, {_tb_a}); border: none; }}"
            "QLabel { background: transparent; }"
        )
        if DEBUG:
            print(f"[debug] titlebar alpha={_TB_ALPHA} ({_tb_a}/255)", flush=True)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def paintEvent(self, event):
        # Explicit fill: stylesheet/palette fills are unreliable on
        # translucent frameless windows (Wayland), painter always runs.
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
        self.setFixedSize(24, 24)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("color: rgba(255,255,255,110); background: transparent; font-size: 14px;")
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

        # Clipboard bridge pump: the MainWorld script stashes every
        # programmatic copy in window.__pyClipboardPending; mirror it
        # onto the real system clipboard (see _pump_clipboard).
        self._last_clip = ""
        self._clip_timer = QTimer(self)
        self._clip_timer.timeout.connect(self._pump_clipboard)
        self._clip_timer.start(500)

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

    def _pump_clipboard(self):
        try:
            self.page.runJavaScript(
                "window.__pyClipboardPending || ''", self._on_clip_text)
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
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
