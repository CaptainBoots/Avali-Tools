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

## Controls

The bottom player bar is Spotify's own web footer with a solid stock
fill — all its buttons, seek and volume work natively, no bridging.

## Spicetify snippets (ported)

These Marketplace snippets are built in (CSS + text matching, so they
survive Spotify renames):

* Hide Made For You (home shelf)
* Remove Browse button
* Remove Popular shelves (radio, albums, new releases, #SpotifyWrapped)
* Hide Full Screen button
* Hide Mini Player button

## Spicetify extensions (ported)

* **adblockify** — already built in (network-level blocker, on by default)
* **Auto Skip Videos** — skips playing video tracks (muted Canvas loops
  excluded). Off: `SPOTIFY_SKIP_VIDEOS=0 ./run.sh`
* **AI Band Blocker** — skips your blocklisted artists, matched against
  the playing artist name:
  `SPOTIFY_BLOCKED_ARTISTS="artist one,artist two" ./run.sh`, and/or
  `SPOTIFY_BLOCKED_ARTISTS_FILE=~/.config/spotify-blocked-artists.txt`
  (one per line, `#` comments allowed)
* **SpicyTracker** — strips `?si=` tracking from Spotify share links on
  copy (copy-event + clipboard-API paths), automatic
* Not portable: Spicy/Copy/More Lyrics (need Genius keys / desktop-only UI)

The window is frameless, so resizing is via the ◢ grip in the
bottom-right corner (compositor edge-resize where supported).

If you'd rather install manually:
```bash
sudo pacman -S python-pyqt6 python-pyqt6-webengine
python3 main.py
```

Audio needs Widevine (Spotify streams are DRM-encrypted — without it
you get `EMEError: No supported keysystem` and tracks won't play):
```bash
yay -S chromium-widevine   # or: yay -S google-chrome
```
`run.sh` checks for it on launch. Verify at
https://bitmovin.com/demos/drm. Override path with
`SPOTIFY_WIDEVINE_PATH=/path/to/libwidevinecdm.so`.

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
