# Steam Transparent (custom launcher)

A fully custom, transparent PyQt6 UI for browsing your Steam library and
friends list, and launching games -- built the same way the Spotify
wrapper was: your own real Qt window, real OS-level transparency, no
theming/patching of the actual Steam client involved.

## What this is (and isn't)

This does **not** reimplement Steam. It can't -- Valve's download
infrastructure, DRM, Proton, anti-cheat, Workshop, and matchmaking are
private, licensed, and not something any third party can legally or
technically rebuild.

What it actually does:
- Reads your **real** library and friends list from Steam's official
  public Web API (read-only).
- Launches games by telling the **real, locally-installed Steam client**
  to do it, via `steam://run/<appid>` -- the same mechanism Steam's own
  desktop shortcuts use. Steam still handles the actual launch, DRM
  check, and any first-run setup.
- Wraps all of that in a UI you fully control the look of.

So: your library, browsable and launchable, in a transparent window --
but downloading/installing games still happens through the real Steam
client running quietly in the background (this app starts it silently
for you if it isn't already running).

## Setup

### 1. Install dependencies
```bash
chmod +x run.sh
./run.sh
```
This installs `python-pyqt6` via pacman if it's missing, then launches
the app.

### 2. Get a Steam Web API key
Go to: https://steamcommunity.com/dev/apikey
Log in, register a key (you can put anything for the domain field, e.g.
`localhost`). Copy the key.

### 3. Find your SteamID64
Go to: https://steamid.io
Paste your Steam profile URL. It'll show your SteamID64 (a long number).

### 4. First run
The app will ask for both values the first time and save them to:
```
~/.config/SteamTransparent/config.json
```
(plain text, on your machine only -- this app never sends your key
anywhere except Steam's own API)

## How the hidden Steam client works

Anything the Web API can't do (installing/downloading games, uninstalls,
and the DRM/Proton setup that happens around launches) goes through the
real Steam client -- but it runs **tray-only** (`steam -silent`, no main
window), not as a full desktop app:

- **Launch**: client must stay alive while the game runs. No auto-close
  on this path -- quitting Steam kills the game.
- **Install**: Steam always pops its own location-picker dialog for this
  (Valve gives no way to suppress it). Confirm it once; the launcher
  watches `steamapps/downloading/<appid>/` for progress in the status
  line, and when the install lands it shuts the client down with
  `steam -shutdown` -- but only if this app started it. A client you
  already had open is never touched.
- **Uninstall**: confirm here, confirm in Steam's dialog, and once the
  `appmanifest_<appid>.acf` disappears the client is shut down the same
  way.
- Installed games show `✓ installed` in the library list.
- **Store tab**: search the Steam store without leaving the app (uses
  the store's own public search backend). Results show price/discount;
  double-click opens the game's page in the real client.
- **Game page links**: Store Page, Community Hub, Workshop, and
  Guides for the selected game, on the right-hand details page.
- **Community page**: friends list (double-click opens a profile),
  Steam Chat, and My Profile buttons.
- These all open inside the real Steam client via `steam://openurl/`
  (Store/Community/Workshop have no API -- this is the official way to
  point the client at them). Browsing summons Steam windows; only the
  background ops (install/download/uninstall) stay tray-hidden.

## Look + layout

Colorless translucent theme, matching the Spotify wrapper -- but laid
out like the real Steam client: top-level **STORE / LIBRARY /
COMMUNITY** nav, a left game list, and a right game-details page
(artwork hero, playtime + install status, PLAY / INSTALL / UNINSTALL,
Store/Hub/Workshop/Guides links, Properties button). Right-click any
library game for the same actions plus **Properties…** with
Steam-style tabs:
- **Properties…** with Steam-style tabs:
  - *General*: capsule artwork, developer, release date, playtime,
    install status/size.
  - *Launch Options*: view + edit per-game options (e.g.
    `gamemoderun %command%`), stored in `localconfig.vdf`.
  - *Updates*: always / on-launch-only / high-priority, stored in
    the appmanifest.
  - *Installed Files*: size, install path, Verify integrity,
    Browse files, Uninstall.
- Edits only save while Steam is fully closed (it rewrites those
  files on exit): if our hidden client is running you'll be asked to
  close it first; if it's your own client, editing is refused rather
  than risk clobbering. Every write takes a `.bak` backup first.

Limitation: per-game Proton version pinning lives in Steam's
`config.vdf` and has no URI/API -- that still needs the real client's
Properties dialog for now.

## Important: your library must be visible to the API

If your Steam profile's game details are set to private, the Web API
will return an empty list even with a valid key, *unless* the API key
belongs to the same account you're querying (which it will, in your
case) -- Valve does allow an account's own key to see its own private
library. If you still get an empty list, check:
Steam → Settings → Privacy → "Game details" is not blocking your own
key (this is rare, but some very old accounts have quirks here).

## Known limitations

- No store browsing built in yet -- only library + launch + install +
  uninstall + store search + friends. `steam_api.py` already has
  `open_store_page()` and `open_friends()` helpers for the next step.
- No season-pass/DLC or subscribed-Workshop-item management: Valve
  exposes no public API for those, and they only exist in the client's
  own UI (reachable via the Hub/Workshop buttons above).
- No live "friend just came online" push updates -- friends list is a
  snapshot, refreshed only on launch. Could be extended with a periodic
  refresh timer if you want that.
- Game icons load individually in the background; a very large library
  will trickle icons in over a few seconds rather than all at once.
- If Steam isn't installed or not on PATH, launching will fail with a
  clear error rather than silently doing nothing.
