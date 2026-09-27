"""
Thin wrapper around Steam's official Web API, plus helpers for talking to
the real Steam client via steam:// URIs and checking whether it's running.

Steam's Web API is read-only public data (owned games, friends, profile
info) -- it cannot install/launch/uninstall games. That's why launching
still goes through the real Steam client via steam:// URIs (see
launch_game() below): there is no way to actually run a game without it.
"""

import json
import os
import subprocess
import urllib.request
import urllib.parse
import shutil


API_BASE = "https://api.steampowered.com"


class SteamAPIError(Exception):
    pass


def _get_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise SteamAPIError(str(e)) from e


def get_owned_games(api_key: str, steam_id: str) -> list[dict]:
    """Returns a list of dicts: appid, name, playtime_forever (minutes),
    img_icon_url. Requires the account's game details to be public, or an
    API key belonging to that same account."""
    params = urllib.parse.urlencode({
        "key": api_key,
        "steamid": steam_id,
        "include_appinfo": 1,
        "include_played_free_games": 1,
        "format": "json",
    })
    data = _get_json(f"{API_BASE}/IPlayerService/GetOwnedGames/v1/?{params}")
    return data.get("response", {}).get("games", [])


def get_friend_list(api_key: str, steam_id: str) -> list[dict]:
    params = urllib.parse.urlencode({
        "key": api_key,
        "steamid": steam_id,
        "relationship": "friend",
    })
    data = _get_json(f"{API_BASE}/ISteamUser/GetFriendList/v1/?{params}")
    return data.get("friendslist", {}).get("friends", [])


def get_player_summaries(api_key: str, steam_ids: list[str]) -> list[dict]:
    """Batch lookup of profile info (name, avatar, online status) for up
    to 100 SteamIDs at once."""
    if not steam_ids:
        return []
    params = urllib.parse.urlencode({
        "key": api_key,
        "steamids": ",".join(steam_ids),
    })
    data = _get_json(f"{API_BASE}/ISteamUser/GetPlayerSummaries/v2/?{params}")
    return data.get("response", {}).get("players", [])


def game_icon_url(appid: int, img_icon_url: str) -> str:
    return f"https://media.steampowered.com/steamcommunity/public/images/apps/{appid}/{img_icon_url}.jpg"


def avatar_url(player_summary: dict) -> str:
    return player_summary.get("avatarmedium", "")


def fetch_image_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read()


# --- Local library state (lets us watch installs without Steam's UI) ------
#
# Steam tracks installs on disk, no client interaction needed to read:
#   <steam>/steamapps/appmanifest_<appid>.acf  -> game fully installed
#   <steam>/steamapps/downloading/<appid>/     -> download in progress
# Polling these lets the launcher show progress and know when the
# client is no longer needed (so it can be shut down to save resources).

def find_steam_root() -> str | None:
    """Locate the Steam data dir (…/Steam containing steamapps/)."""
    home = os.path.expanduser("~")
    for cand in (os.path.join(home, ".steam", "steam"),
                 os.path.join(home, ".local", "share", "Steam")):
        if os.path.isdir(os.path.join(cand, "steamapps")):
            return cand
    return None


def _dir_size_bytes(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def is_game_installed(appid: int) -> bool:
    root = find_steam_root()
    if root is None:
        return False
    return os.path.isfile(
        os.path.join(root, "steamapps", f"appmanifest_{appid}.acf"))


def download_progress(appid: int) -> tuple[bool, int]:
    """(in_progress, bytes_downloaded_so_far) for one app."""
    root = find_steam_root()
    if root is None:
        return False, 0
    d = os.path.join(root, "steamapps", "downloading", str(appid))
    if not os.path.isdir(d):
        return False, 0
    return True, _dir_size_bytes(d)


# --- Talking to the real, locally-installed Steam client ------------------

def find_steam_binary() -> str | None:
    return shutil.which("steam")


def is_steam_running() -> bool:
    try:
        result = subprocess.run(["pgrep", "-x", "steam"], capture_output=True)
        return result.returncode == 0
    except FileNotFoundError:
        return False


def ensure_steam_running():
    """Starts the real Steam client minimized/silent in the background if
    it isn't already running. Needed because steam:// URIs (launching
    games, opening friends, etc.) require the client to be alive."""
    start_hidden()


def start_hidden():
    """Start the real Steam client with no main window (tray icon only).

    `steam -silent` suppresses the main window and friends/chat UI --
    this is as hidden as the official client gets; there is no true
    headless mode. Downloads, Proton setup, and DRM all still work in
    this state, which is exactly what we want for background operations.
    No-op if Steam is already running.
    """
    if is_steam_running():
        return
    binary = find_steam_binary()
    if binary is None:
        raise SteamAPIError("Steam binary not found on PATH.")
    subprocess.Popen(
        [binary, "-silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def shutdown():
    """Ask the Steam client to exit cleanly. Only call this when nothing
    needs it: no game launched through it is running and no download is
    in progress (quitting mid-download just resumes next start, but
    quitting on a running game kills the game)."""
    shutdown_steam()


def install_game(appid: int):
    """Queue a game install via the real client.

    NOTE: Steam shows its own install dialog (location picker) for this
    and there is no way to suppress it -- the client stays windowless
    except for that one dialog. Once the user confirms, the download
    proceeds in the background and the client can go back to tray-only.
    """
    ensure_steam_running()
    _run_steam_uri(f"steam://install/{appid}")


def _run_steam_uri(uri: str):
    ensure_steam_running()
    binary = find_steam_binary()
    if binary is None:
        raise SteamAPIError("Steam binary not found on PATH.")
    subprocess.Popen(
        [binary, uri],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def launch_game(appid: int):
    _run_steam_uri(f"steam://run/{appid}")


def uninstall_game(appid: int):
    _run_steam_uri(f"steam://uninstall/{appid}")


def open_friends():
    _run_steam_uri("steam://open/friends")


def open_store_page(appid: int):
    _run_steam_uri(f"steam://store/{appid}")


def open_url_in_steam(url: str):
    """Open any store/community URL inside the real client's browser.

    Store, Community, Workshop, Guides, Discussions have no Web API --
    they only exist as client/web UI. steam://openurl/ is the official
    way to point the client at them (same mechanism as links clicked
    inside Steam itself).
    """
    ensure_steam_running()
    _run_steam_uri(f"steam://openurl/{url}")


def open_store_front():
    open_url_in_steam("https://store.steampowered.com/")


def open_game_hub(appid: int):
    """Community hub: screenshots, artwork, guides, discussions."""
    open_url_in_steam(f"https://steamcommunity.com/app/{appid}/")


def open_workshop(appid: int):
    open_url_in_steam(f"https://steamcommunity.com/app/{appid}/workshop/")


def open_guides(appid: int):
    open_url_in_steam(f"https://steamcommunity.com/app/{appid}/guides/")


def open_profile(steam_id: str):
    open_url_in_steam(f"https://steamcommunity.com/profiles/{steam_id}/")


STORE_SEARCH_API = "https://store.steampowered.com/api/storesearch/"


def store_search(term: str, country: str = "US", language: str = "english",
                 limit: int = 25) -> list[dict]:
    """In-app store search via the public search endpoint (unofficial but
    openly served JSON, same backend as the store's own search box).

    Returns dicts: id, name, tiny_image, price {initial, final,
    discount_percent} (price may be absent for unreleased/free items).
    """
    params = urllib.parse.urlencode({
        "term": term, "cc": country, "l": language,
    })
    data = _get_json(f"{STORE_SEARCH_API}?{params}")
    return (data.get("items") or [])[:limit]


def shutdown_steam():
    binary = find_steam_binary()
    if binary is None:
        return
    subprocess.Popen(
        [binary, "-shutdown"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# --- Properties backend (on-disk facts, no client needed) -----------------
#
# Per-game settings live in two KeyValues files:
#   steamapps/appmanifest_<appid>.acf  -- install state, size, update mode
#   userdata/<uid>/config/localconfig.vdf -- per-game launch options
# Both are only safe to WRITE while Steam is closed (the client rewrites
# them on exit). The UI guarantees that; these helpers just do the edit
# with a .bak backup first.

def _manifest_path(appid: int) -> str | None:
    root = find_steam_root()
    if root is None:
        return None
    p = os.path.join(root, "steamapps", f"appmanifest_{appid}.acf")
    return p if os.path.isfile(p) else None


def read_manifest(appid: int) -> dict:
    """Top-level "key" "value" pairs from the appmanifest ({} if absent)."""
    import re
    p = _manifest_path(appid)
    if p is None:
        return {}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    return dict(re.findall(r'^\s*"([^"]+)"\s+"([^"]*)"', text, re.M))


def _backup(path: str):
    import shutil as _sh
    try:
        _sh.copy2(path, path + ".bak")
    except OSError:
        pass


def set_manifest_value(appid: int, key: str, value: str) -> bool:
    """Surgical single-key replace in the appmanifest. Caller must ensure
    Steam is not running."""
    import re
    p = _manifest_path(appid)
    if p is None:
        return False
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        pat = re.compile(r'^(\s*"%s"\s+)"[^"]*"' % re.escape(key), re.M)
        if not pat.search(text):
            return False
        _backup(p)
        with open(p, "w", encoding="utf-8") as f:
            f.write(pat.sub(r'\1"%s"' % value, text, count=1))
        return True
    except OSError:
        return False


AUTO_UPDATE_LABELS = {
    "0": "Always keep this game updated",
    "1": "Only update this game when I launch it",
    "2": "High priority — always update first",
}


def game_install_path(appid: int) -> str | None:
    root = find_steam_root()
    man = read_manifest(appid)
    dirname = man.get("installdir")
    if not root or not dirname:
        return None
    p = os.path.join(root, "steamapps", "common", dirname)
    return p if os.path.isdir(p) else None


def open_game_folder(appid: int) -> bool:
    p = game_install_path(appid)
    if p is None:
        return False
    try:
        subprocess.Popen(["xdg-open", p],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except (OSError, FileNotFoundError):
        return False


def validate_game(appid: int):
    """Ask the client to verify file integrity (needs client alive)."""
    _run_steam_uri(f"steam://validate/{appid}")


def find_userdata_dir() -> str | None:
    root = find_steam_root()
    if root is None:
        return None
    ud = os.path.join(root, "userdata")
    try:
        subs = [d for d in os.listdir(ud)
                if d.isdigit() and os.path.isdir(os.path.join(ud, d))]
    except OSError:
        return None
    if not subs:
        return None
    return os.path.join(ud, subs[0])


def _localconfig_path() -> str | None:
    ud = find_userdata_dir()
    if ud is None:
        return None
    p = os.path.join(ud, "config", "localconfig.vdf")
    return p if os.path.isfile(p) else None


def _find_block(text: str, key: str, start: int = 0):
    """Locate `"key" { ... }` from `start`, return (inner_start,
    inner_end) via brace matching, or None."""
    import re
    m = re.search(r'"%s"\s*\n?\s*\{' % re.escape(key), text[start:])
    if not m:
        return None
    i = start + m.end()  # just past the opening brace
    depth = 1
    in_str = False
    esc = False
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i, i  # inner span is (open+1 .. i)
        i += 1
    return None


def get_launch_options(appid: int) -> str:
    """Read per-game launch options ("" if unset). Safe anytime."""
    import re
    p = _localconfig_path()
    if p is None:
        return ""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    app = _find_block(text, str(appid))
    if app is None:
        return ""
    inner = text[text.index("{", text.index('"%s"' % appid)) + 1:app[0]]
    m = re.search(r'"LaunchOptions"\s+"((?:[^"\\]|\\.)*)"', inner)
    if not m:
        return ""
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\")


def set_launch_options(appid: int, opts: str) -> bool:
    """Write per-game launch options. Caller must ensure Steam is NOT
    running (it rewrites this file on exit). Takes a .bak first."""
    p = _localconfig_path()
    if p is None:
        return False
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    esc = opts.replace("\\", "\\\\").replace('"', '\\"')
    import re
    app = _find_block(text, str(appid))
    _backup(p)
    try:
        if app is not None:
            open_idx = text.index("{", text.index('"%s"' % appid))
            inner = text[open_idx + 1:app[0]]
            pat = re.compile(r'"LaunchOptions"\s+"(?:[^"\\]|\\.)*"')
            if pat.search(inner):
                inner = pat.sub('"LaunchOptions"\t\t"%s"' % esc, inner, count=1)
            else:
                inner = inner + '\t\t\t"LaunchOptions"\t\t"%s"\n' % esc
            text = text[:open_idx + 1] + inner + text[app[0]:]
        else:
            apps = _find_block(text, "apps")
            if apps is None:
                return False
            apps_open = text.index("{", text.index('"apps"'))
            block = ('\n\t\t\t\t"%s"\n\t\t\t\t{\n'
                     '\t\t\t\t\t"LaunchOptions"\t\t"%s"\n'
                     '\t\t\t\t}\n' % (appid, esc))
            text = text[:apps_open + 1] + block + text[apps_open + 1:]
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except (OSError, ValueError):
        return False


def get_store_details(appid: int) -> dict:
    """Name, developers, release date, blurb via the public appdetails
    endpoint (same backend as store pages). Best-effort: {} on failure."""
    params = urllib.parse.urlencode({"appids": appid})
    try:
        data = _get_json(
            f"https://store.steampowered.com/api/appdetails/?{params}")
    except SteamAPIError:
        return {}
    try:
        entry = data.get(str(appid), {})
        if not entry.get("success"):
            return {}
        d = entry.get("data", {})
        return {
            "name": d.get("name", ""),
            "developers": ", ".join(d.get("developers", [])),
            "release": (d.get("release_date") or {}).get("date", ""),
            "blurb": d.get("short_description", ""),
            "header": d.get("header_image", ""),
        }
    except (AttributeError, TypeError):
        return {}
