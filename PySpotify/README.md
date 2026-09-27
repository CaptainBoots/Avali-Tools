# Transparent Spotify (PyQt6 wrapper)

Loads the real Spotify web player (open.spotify.com) inside a native,
frameless, transparent Qt window. Works on a Free account (with your
existing browser-level ad blocker not applying here — see "Ads" below).

## Run

```bash
cd ~/spotify-transparent   # wherever you saved the files
chmod +x run.sh            # first time only
./run.sh
```

`run.sh` checks for `python-pyqt6` and `python-pyqt6-webengine` and
installs whichever is missing via `pacman` (will prompt for your sudo
password), then launches the app. You only need `./run.sh` — it calls
`main.py` for you.

If you'd rather install manually:
```bash
sudo pacman -S python-pyqt6 python-pyqt6-webengine
python3 main.py
```

First run: log into Spotify inside the window like any normal browser
login. Your session is saved to `~/.local/share/SpotifyTransparent/`,
so you won't need to log in again on future launches.

## Making it actually look transparent

Two separate things both need to be true, same as with every other
transparency attempt today:

1. **The app's own background must be transparent.** This script
   already handles that — both the Qt window and the web page's base
   canvas are set to alpha 0, and CSS is injected to strip Spotify's
   own solid backgrounds.
2. **Your desktop compositor must actually blur/composite behind it.**
   On KWin (Plasma), make sure the **Blur** desktop effect is enabled
   in System Settings → Desktop Effects. Without this, "transparent"
   will just look like a black hole instead of glass.

## If it still looks solid black/white

Spotify's internal class names shift over time (same root cause as
the Spicetify problems). To find current ones:

1. Run the app once.
2. Add `self.page.setUrl(QUrl("about:blank"))`-free debugging by
   instead temporarily adding this near the top of `main()`:
   ```python
   os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = "9223"
   ```
3. Relaunch, then open `http://localhost:9223` in Firefox — this
   gives you a real, non-flaky Chromium DevTools session (much more
   reliable than the Spotify Electron app's debug port, since Qt's
   WebEngine handles it more predictably).
4. Inspect the background elements, find the real class/data-testid
   names, and update the `INJECTED_CSS` selector list in `main.py`.

## Ad blocking

Built in, two layers:

1. **Network-level blocking** (`AdBlockInterceptor` in `main.py`) —
   refuses requests to a curated list of known ad/tracking domains
   before they're even sent. This is the primary defense, since it
   also stops ad *audio*, not just banner images.
2. **CSS fallback** — hides any leftover ad banner container elements
   in case a request slips through on a domain not yet in the list.

If you notice ads getting through: open the debugging session (see
below), check the Network tab for the domain the ad request came
from, and add that domain to `AD_HOST_FRAGMENTS` near the top of
`main.py`.

If blocking ever breaks actual playback (an ad domain overlapping
with a real audio CDN), remove the offending entry from
`AD_HOST_FRAGMENTS`.

## Known limitations

- No native media keys / MPRIS integration out of the box (unlike
  the official app or `librespot`-based clients).
- No Spotify Connect (won't show up as a "device" for other Spotify
  apps to cast to).
- Window dragging/resizing is custom-built here (frameless windows
  lose the OS titlebar), so it's simpler than a native window manager
  frame — good enough to move/resize, nothing fancier.
