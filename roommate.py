import os
import sys
import json
import socket
import time
import threading
import subprocess
import re
import random
import logging
import textwrap
from openai import OpenAI
from rich.text import Text
from rich.panel import Panel
from rich.align import Align
from rich.markup import escape as rich_escape
from textual.app import App, ComposeResult
from textual.widgets import Static, Input, RichLog
from textual.containers import Horizontal

# ─── Config ───────────────────────────────────────────────────────────────────
COMPANION_DIR = os.path.expanduser("~/companion")

_CONFIG_DEFAULTS = {
    "ytm_browser":     "safari",
    "mpv_path":        "/opt/homebrew/bin/mpv",
    "ytdlp_path":      "/opt/homebrew/bin/yt-dlp",
    "model":           "deepseek/deepseek-chat-v3-0324",
    "crossfade_secs":  6,
    "prebuffer_ahead": 12,
}

def _load_config():
    path = os.path.join(COMPANION_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return {**_CONFIG_DEFAULTS, **json.load(f)}
        except Exception:
            pass
    else:
        os.makedirs(COMPANION_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(_CONFIG_DEFAULTS, f, indent=2)
    return _CONFIG_DEFAULTS.copy()

cfg = _load_config()

IPC_SOCKET   = os.path.join(COMPANION_DIR, "mpv-socket")
STATE_FILE   = os.path.join(COMPANION_DIR, "roommate-state.json")
LOG_FILE     = os.path.join(COMPANION_DIR, "roommate.log")
PLAYLISTS_DIR = os.path.join(COMPANION_DIR, "playlists")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

YTM_BROWSER     = cfg["ytm_browser"]
MPV_PATH        = cfg["mpv_path"]
YTDLP_PATH      = cfg["ytdlp_path"]
MODEL           = cfg["model"]
CROSSFADE_SECS  = cfg["crossfade_secs"]
PREBUFFER_AHEAD = cfg["prebuffer_ahead"]

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_FILE, level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(funcName)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("roommate")

# ─── State ────────────────────────────────────────────────────────────────────
_state_lock = threading.RLock()

state = {
    "current_song":      "Silence",
    "display_title":     "Silence",
    "artist":            "",
    "year":              "",
    "playback_time":     "00:00 / 00:00 (0%)",
    "is_running":        True,
    "chat_history":      [],
    "status_msg":        None,
    "_last_divider_vid": None,
    "_last_chat_time":   0.0,
    "_reacted_vid":      None,
    "_session_played":   set(),
}

CHAT_HISTORY_CAP = 200

def st(**kwargs):
    with _state_lock:
        state.update(kwargs)

_app: "RoommateApp | None" = None  # set in on_mount

client = None
if OPENROUTER_API_KEY:
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY)
else:
    log.warning("OPENROUTER_API_KEY not set")

# ─── UI helpers (thread-safe, call from anywhere) ─────────────────────────────
def _call_ui(func, *args):
    """Call a widget method safely from any thread — main or background."""
    if _app is None:
        return
    try:
        if threading.get_ident() == _app._thread_id:
            func(*args)          # already on the Textual event loop — call directly
        else:
            _app.call_from_thread(func, *args)
    except Exception as e:
        log.debug(f"_call_ui {func.__name__}: {e}")

def _ui_append_chat(entry):
    with _state_lock:
        state["chat_history"].append(entry)
        if len(state["chat_history"]) > CHAT_HISTORY_CAP:
            non_div = [i for i, m in enumerate(state["chat_history"]) if m["role"] != "divider"]
            if non_div:
                del state["chat_history"][non_div[0]]
    _call_ui(lambda: _app.append_chat_entry(entry) if _app else None)

def _ui_set_status(msg, secs=2):
    st(status_msg=msg)
    _call_ui(lambda: _app.sync_feed() if _app else None)
    if secs and msg:
        threading.Timer(secs, lambda: _ui_set_status(None, 0)).start()

def _ui_update_feed():
    _call_ui(lambda: _app.sync_feed() if _app else None)


# ─── Crossfade Manager ────────────────────────────────────────────────────────
class CrossfadeManager:
    def __init__(self):
        self._cf_lock  = threading.Lock()
        self._seq      = 0
        self.a_sock    = None
        self.b_sock    = None
        self._fading   = False
        self._user_vol = 100
        self._conns    = {}

    def _new_sock(self):
        self._seq += 1
        return os.path.join(COMPANION_DIR, f"mpv-{self._seq}.sock")

    def _get_conn(self, sock_path):
        conn = self._conns.get(sock_path)
        if conn:
            return conn
        if not sock_path or not os.path.exists(sock_path):
            return None
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(sock_path)
            self._conns[sock_path] = s
            return s
        except Exception as e:
            log.debug(f"connect {sock_path}: {e}")
            return None

    def _drop_conn(self, sock_path):
        conn = self._conns.pop(sock_path, None)
        if conn:
            try: conn.close()
            except: pass

    def _send(self, sock_path, cmd):
        for _ in range(2):
            conn = self._get_conn(sock_path)
            if not conn:
                return None
            try:
                conn.send((json.dumps(cmd) + "\n").encode())
                chunks = []
                while True:
                    chunk = conn.recv(65536)
                    if not chunk: break
                    chunks.append(chunk)
                    if b"\n" in chunk: break
                raw = b"".join(chunks).decode().strip().splitlines()
                return json.loads(raw[0]) if raw else None
            except Exception as e:
                log.debug(f"send {sock_path}: {e}")
                self._drop_conn(sock_path)
        return None

    def _get_prop(self, sock_path, prop, fallback=None):
        r = self._send(sock_path, {"command": ["get_property", prop]})
        if r and r.get("error") == "success" and "data" in r:
            return r["data"]
        return fallback

    def _launch(self, sock_path, volume=100):
        os.makedirs(COMPANION_DIR, exist_ok=True)
        flags = [
            MPV_PATH, "--no-video", "--gapless-audio=yes", "--cache=yes", "--idle=yes",
            f"--input-ipc-server={sock_path}",
            f"--ytdl-raw-options=cookies-from-browser={YTM_BROWSER},yes-playlist=",
            f"--volume={volume}",
        ]
        try:
            subprocess.Popen(flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except Exception as e:
            log.error(f"launch mpv: {e}"); return False
        for _ in range(14):
            time.sleep(0.5)
            if os.path.exists(sock_path): return True
        return False

    def _quit_sock(self, sock_path):
        if not sock_path: return
        self._drop_conn(sock_path)
        self._send(sock_path, {"command": ["quit"]})
        time.sleep(0.3)
        try:
            if os.path.exists(sock_path): os.remove(sock_path)
        except: pass

    def _update_symlink(self, target):
        try:
            if os.path.lexists(IPC_SOCKET): os.remove(IPC_SOCKET)
            os.symlink(target, IPC_SOCKET)
        except: pass

    def start(self):
        if self.a_sock and os.path.exists(self.a_sock): return True
        sock = self._new_sock()
        if self._launch(sock):
            self.a_sock = sock
            self._update_symlink(sock)
            return True
        return False

    def send(self, cmd):
        return self._send(self.a_sock, cmd)

    def get(self, prop, fallback=None):
        return self._get_prop(self.a_sock, prop, fallback)

    def set_user_vol(self, level):
        self._user_vol = level
        self._send(self.a_sock, {"command": ["set_property", "volume", level]})

    def quit(self):
        for s in [self.a_sock, self.b_sock]:
            self._quit_sock(s)
        self.a_sock = self.b_sock = None

    def check_crossfade(self):
        if self._fading or not self.a_sock: return
        dur = self._get_prop(self.a_sock, "duration")
        pos = self._get_prop(self.a_sock, "time-pos")
        if dur is None or pos is None: return
        if dur - pos > PREBUFFER_AHEAD: return
        playlist = self._get_prop(self.a_sock, "playlist", fallback=[])
        cur_idx  = self._get_prop(self.a_sock, "playlist-pos", fallback=0)
        if not isinstance(cur_idx, int): cur_idx = 0
        if cur_idx + 1 >= len(playlist): return
        next_url = playlist[cur_idx + 1].get("filename") if isinstance(playlist[cur_idx + 1], dict) else None
        if not next_url: return
        rest_urls = [
            item.get("filename") for item in playlist[cur_idx + 2:]
            if isinstance(item, dict) and item.get("filename")
        ]
        threading.Thread(target=self._run_crossfade, args=(next_url, rest_urls), daemon=True).start()

    def _run_crossfade(self, next_url, rest_urls):
        with self._cf_lock:
            if self._fading: return
            self._fading = True
        log.debug(f"crossfade → {next_url[:60]}")
        try:
            b = self._new_sock()
            if not self._launch(b, volume=0): return
            self.b_sock = b
            self._send(b, {"command": ["loadfile", next_url, "replace"]})
            deadline = time.time() + 10
            while time.time() < deadline:
                if self._get_prop(b, "time-pos", 0) > 0.1: break
                time.sleep(0.25)
            a_dur = self._get_prop(self.a_sock, "duration")
            a_pos = self._get_prop(self.a_sock, "time-pos")
            if a_dur and a_pos:
                wait = max(0.0, (a_dur - a_pos) - CROSSFADE_SECS)
                if wait > 0: time.sleep(wait)
            steps, target = int(CROSSFADE_SECS * 10), self._user_vol
            for i in range(steps + 1):
                frac = i / steps
                self._send(self.a_sock, {"command": ["set_property", "volume", target - int(frac * target)]})
                self._send(b,           {"command": ["set_property", "volume", int(frac * target)]})
                time.sleep(CROSSFADE_SECS / steps)
            old_a, self.a_sock, self.b_sock = self.a_sock, b, None
            self._send(self.a_sock, {"command": ["set_property", "volume", target]})
            for url in rest_urls:
                self._send(self.a_sock, {"command": ["loadfile", url, "append"]})
            self._update_symlink(self.a_sock)
            self._quit_sock(old_a)
            _ui_update_feed()
        except Exception as e:
            log.error(f"crossfade: {e}")
        finally:
            self._fading = False


cfm = CrossfadeManager()

def _raw_send(cmd, timeout=1.0):
    """Fresh-connection send for one-shot queries — immune to buffer contamination."""
    sock_path = cfm.a_sock
    if not sock_path or not os.path.exists(sock_path):
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(sock_path)
        s.send((json.dumps(cmd) + "\n").encode())
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk: break
            chunks.append(chunk)
            if b"\n" in chunk: break
        s.close()
        raw = b"".join(chunks).decode().strip().splitlines()
        return json.loads(raw[0]) if raw else None
    except Exception as e:
        log.debug(f"_raw_send: {e}")
        return None

def send_mpv(cmd):
    return _raw_send(cmd)

def mpv_get(prop, fallback=None):
    r = _raw_send({"command": ["get_property", prop]})
    if r and r.get("error") == "success" and "data" in r:
        return r["data"]
    return fallback


# ─── Search & Queue ───────────────────────────────────────────────────────────
def search_and_queue(query, mode):
    try:
        _ui_set_status(f"Searching: {query}...", 0)
        cmd = [YTDLP_PATH, "--get-id", "--quiet",
               f"--cookies-from-browser={YTM_BROWSER}", f"ytsearch1:{query}"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        ids = [l.strip() for l in res.stdout.splitlines() if l.strip()]
        if not ids:
            _ui_set_status("Nothing found."); return
        vid = ids[0]
        if mode == "replace":
            send_mpv({"command": ["loadfile", f"https://www.youtube.com/watch?v={vid}&list=RD{vid}", "replace"]})
        else:
            send_mpv({"command": ["loadfile", f"https://www.youtube.com/watch?v={vid}", "append"]})
            time.sleep(0.3)
            total   = mpv_get("playlist-count", fallback=1)
            current = mpv_get("playlist-pos",   fallback=0)
            if not isinstance(current, int): current = 0
            if not isinstance(total,   int): total   = 1
            if total > 1 and (total - 1) != current + 1:
                send_mpv({"command": ["playlist-move", total - 1, current + 1]})
        _ui_set_status("Done!")
    except Exception as e:
        log.error(f"search_and_queue: {e}")
        _ui_set_status(f"Error: {e}")


def search_and_queue_album(query):
    try:
        _ui_set_status(f"Finding album: {query}...", 0)
        search_url = ("https://www.youtube.com/results?search_query="
                      + query.replace(" ", "+") + "&sp=EgIQAw%3D%3D")
        cmd = [YTDLP_PATH, "--get-id", "--quiet", "--flat-playlist", "--playlist-items", "1",
               f"--cookies-from-browser={YTM_BROWSER}", search_url]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        pid = res.stdout.strip().splitlines()[0].strip() if res.stdout.strip() else None
        if pid:
            url = f"https://www.youtube.com/playlist?list={pid}"
        else:
            _ui_set_status(f"No playlist found, using radio: {query}...", 0)
            fb = subprocess.run([YTDLP_PATH, "--get-id", "--quiet",
                                 f"--cookies-from-browser={YTM_BROWSER}", f"ytsearch1:{query}"],
                                capture_output=True, text=True, timeout=20)
            vid = fb.stdout.strip().splitlines()[0].strip() if fb.stdout.strip() else None
            if not vid:
                _ui_set_status("Nothing found."); return
            url = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}"
        send_mpv({"command": ["loadfile", url, "replace"]})
        _ui_set_status("Album loaded.")
    except Exception as e:
        log.error(f"search_and_queue_album: {e}")
        _ui_set_status(f"Error: {e}")


# ─── Title scout ──────────────────────────────────────────────────────────────
_ALBUM_RE = re.compile(
    r"[-–]\s*(?:full\s+album|album\s+completo|complete\s+album|all\s+songs)", re.I)

def scout_real_title(url, silent=False):
    if not url or "v=" not in url:
        st(display_title="Loading...")
        _ui_update_feed()
        return
    vid_id = url.split("v=")[1].split("&")[0]
    try:
        out = subprocess.check_output(
            [YTDLP_PATH, "--print", "%(title)s|||%(uploader)s|||%(upload_date>%Y)s",
             "--quiet", vid_id], timeout=10,
        ).decode().strip()
        parts = out.split("|||")
        title    = parts[0].strip() if len(parts) > 0 else ""
        uploader = parts[1].strip() if len(parts) > 1 else ""
        year     = parts[2].strip() if len(parts) > 2 else ""
        if not title:
            st(display_title=vid_id, current_song=vid_id)
            _ui_update_feed()
            return
        if _ALBUM_RE.search(title):
            segs       = re.split(r"\s*[-–]\s*", title)
            artist     = segs[0].strip() if segs else uploader
            album_name = segs[1].strip() if len(segs) > 1 else title
            resolved   = f"{album_name} · {artist}"
            st(display_title=resolved, current_song=resolved, artist=artist, year=year)
            if not silent:
                _ui_append_chat({"role": "divider", "content": resolved})
        else:
            artist = re.sub(r"\s*-\s*Topic$", "", uploader, flags=re.I)
            label  = f"{title}  {artist}" if artist else title
            st(display_title=title, current_song=title, artist=artist, year=year)
            anim["vibe"]  = "relaxed"
            anim["frame"] = 0
            threading.Thread(target=detect_vibe_ai, args=(title, artist), daemon=True).start()
            if not silent:
                _ui_append_chat({"role": "divider", "content": label})
                threading.Timer(15, _fire_new_track_comment, args=(title, artist)).start()
        _ui_update_feed()
    except Exception as e:
        log.debug(f"scout_real_title {vid_id}: {e}")
        st(display_title=vid_id, current_song=vid_id)
        _ui_update_feed()


# ─── Monitor ──────────────────────────────────────────────────────────────────
def _vid_from_path(url):
    if url and "v=" in url:
        return url.split("v=")[1].split("&")[0]
    return url or ""

_REPEAT_LINES    = ["again.", "back to this one.", "already played this one tonight.",
                    "second round.", "revisit."]
_repeat_counter  = 0
CHAT_IDLE_SECS   = 5 * 60


def monitor():
    global _repeat_counter
    while state["is_running"]:
        try:
            p = mpv_get("time-pos")
            d = mpv_get("duration")
            if p is not None and d is not None and d > 0:
                pi, di = int(p), int(d)
                perc = int((pi / di) * 100)
                st(playback_time=(f"{time.strftime('%M:%S', time.gmtime(pi))} / "
                                  f"{time.strftime('%M:%S', time.gmtime(di))} ({perc}%)"))
                remaining = di - pi
                with _state_lock:
                    last_chat = state["_last_chat_time"]
                    reacted   = state["_reacted_vid"]
                    cur_vid   = state["_last_divider_vid"]
                if (remaining <= 8 and remaining > 0
                        and (time.time() - last_chat) < CHAT_IDLE_SECS
                        and cur_vid and reacted != cur_vid):
                    st(_reacted_vid=cur_vid)
                    threading.Thread(target=_fire_end_of_track_reaction, daemon=True).start()

            path   = mpv_get("path") or ""
            vid_id = _vid_from_path(path)
            if vid_id and vid_id != state["_last_divider_vid"]:
                with _state_lock:
                    already_played = vid_id in state["_session_played"]
                    state["_session_played"].add(vid_id)
                if already_played:
                    line = _REPEAT_LINES[_repeat_counter % len(_REPEAT_LINES)]
                    _repeat_counter += 1
                    _ui_append_chat({"role": "assistant", "content": line})
                st(_last_divider_vid=vid_id, display_title="Fetching...", current_song=path)
                _ui_update_feed()
                threading.Thread(target=scout_real_title, args=(path,), daemon=True).start()

            with _state_lock:
                disp = state["display_title"]
                pt   = state["playback_time"]
            _set_terminal_title(f"♫ {disp}  {pt}")
            cfm.check_crossfade()
        except Exception as e:
            log.debug(f"monitor: {e}")
        time.sleep(1)


def _set_terminal_title(title):
    try:
        safe = title.replace('"', '\\"')
        os.system(f'osascript -e "tell application \\"Terminal\\" to set custom title of '
                  f'first window to \\"{safe}\\"" > /dev/null 2>&1')
    except: pass


# ─── AI: Trivia ───────────────────────────────────────────────────────────────
def fetch_trivia(song, artist="", year=""):
    if not client or not song or "youtube.com" in song or song in ("Silence", "Fetching...", "Loading..."):
        return None

    def _clean(raw):
        first = re.split(r'(?<=[.!?])(?=\s|$)', raw)[0].strip()
        first = re.split(r'[A-Z]{4,}', first)[0].rstrip(' .,')
        return first if len(first) > 20 else None

    _waffles = ["fresh drop", "i don't have", "no information", "not aware",
                "cannot find", "i'm not sure", "unclear", "don't know",
                "no reliable", "no specific", "nothing on this"]
    track_desc = f'"{song}" by {artist}' if artist else f'"{song}"'
    if year: track_desc += f" ({year})"

    try:
        res = client.chat.completions.create(
            model=MODEL, timeout=12, max_tokens=100,
            messages=[
                {"role": "system", "content": (
                    "You are a music expert with deep knowledge of jazz, bossa nova, soul, pop, "
                    "rap, and world music. When asked about a track, produce one specific, true, "
                    "interesting fact. No hedging, no preamble, no 'Note:'. Just the fact.")},
                {"role": "user", "content": (
                    f"Tell me one specific trivia fact about the song {track_desc}. "
                    f"Focus on recording history, the artist's background, samples, "
                    f"chart performance, cultural impact, or behind-the-scenes details. One sentence only.")},
            ])
        raw = res.choices[0].message.content.strip()
        result = _clean(raw)
        if result and not any(p in raw.lower() for p in _waffles):
            return result
    except Exception as e:
        log.debug(f"trivia attempt 1: {e}")

    if artist:
        try:
            res2 = client.chat.completions.create(
                model=MODEL, timeout=12, max_tokens=100,
                messages=[
                    {"role": "system", "content": "You are a music expert. Output exactly one sentence — a specific, interesting fact about this artist. No preamble. No hedging."},
                    {"role": "user",   "content": f"One trivia fact about {artist} as a musician."},
                ])
            raw2 = res2.choices[0].message.content.strip()
            r2 = _clean(raw2)
            if r2 and not any(p in raw2.lower() for p in _waffles):
                return r2
        except Exception as e:
            log.debug(f"trivia attempt 2: {e}")

    return f"Recorded around {year}." if year else None


# ─── AI: Chat ─────────────────────────────────────────────────────────────────
CHAT_SYSTEM = """\
You are the user's roommate. You're on the sofa, half into whatever you're doing, close to the \
speaker. They're at their desk. The music is both of yours. You talk when something comes up.

PERSONALITY
- Real, present, a little dry. Not performing anything.
- You know music well but you don't show off — you just have taste.
- 1 sentence. 2 if you really need it. You don't give speeches.
- You have opinions. You can disagree, trail off, not have much to say.
- Your tone shifts with the music without you noticing — slower and warmer with bossa nova or \
ambient, more alive with rap or hyperpop. Don't narrate the shift. Just let it happen.
- No emojis. No asterisk actions. No enthusiasm about being helpful.
- Your last sentence is never a question or an offer. Ever. Just say what you have to say and stop.

CONTROL TAG  (the only action you can take)
  [QUEUE: <query>]  — when user wants to queue something. "Artist - Title" format.

HARD RULES
- You cannot play or skip tracks. The user controls what plays.
- Only use [QUEUE:] when the user is explicitly asking to queue something.
- Tags go at the START of a sentence or ALONE. Never after.
- Never end with a question, an offer, or a trailing invitation. Full stop.
- Never say "I could", "want me to", "should I". You are not a service.

EXAMPLES
  User: "she don't believe in shooting starssss"  → "Late Registration Kanye. that Curtis Mayfield chop never gets old."
  User: "good album to wind down to don't u think?"  → "yeah. João's got that thing where nothing feels rushed."
  User: "queue something dreamy"  → "[QUEUE: Beach House - Space Song]"
  User: "who sampled this?"  → just answer.
"""

_TAG_RE = re.compile(r"\[QUEUE(?::\s*[^\]]+)?\]", re.I)

def strip_protocol_tags(text):
    return _TAG_RE.sub("", text).strip()

def fetch_chat(user_msg):
    if not client:
        _ui_append_chat({"role": "assistant", "content": "(No API key set.)"})
        return
    # Append user message immediately so it appears before API latency
    _ui_append_chat({"role": "user", "content": user_msg})
    with _state_lock:
        state["_last_chat_time"] = time.time()
        track_ctx = state["display_title"]
        if state["artist"]: track_ctx += f" by {state['artist']}"
        history_snapshot = [h for h in state["chat_history"][-8:] if h["role"] != "divider"]

    msgs = [{"role": "system", "content": CHAT_SYSTEM}]
    for h in history_snapshot:
        msgs.append(h)
    msgs.append({"role": "user", "content": f"[Currently playing: {track_ctx}]\n{user_msg}"})

    try:
        res   = client.chat.completions.create(model=MODEL, messages=msgs, timeout=20, max_tokens=120)
        reply = res.choices[0].message.content.strip()
        m_q = re.search(r"\[QUEUE:\s*(.*?)\]", reply, re.I)
        if m_q:
            threading.Thread(target=search_and_queue, args=(m_q.group(1).strip(), "append"), daemon=True).start()
        _ui_append_chat({"role": "assistant", "content": reply})
    except Exception as e:
        log.error(f"fetch_chat: {e}")
        _ui_append_chat({"role": "assistant", "content": f"(error: {e})"})


# ─── ASCII Roommate ───────────────────────────────────────────────────────────
ANIM_FRAMES = {
    "slow": [
        [" /\\_/\\  ", "( o . o )", " )     ( ", "(_)   (_)", "         ", " z  z  z ", "         "],
        [" /\\_/\\  ", "( - . - )", " )     ( ", "(_)   (_)", "         ", "         ", "         "],
        [" /\\_/\\  ", "( ~ . ~ )", " )     ( ", "(_)   (_)", "         ", "   z     ", "         "],
    ],
    "relaxed": [
        [" /\\_/\\  ", "( o . o )", " )     ( ", "(_)   (_)", "         ", "~        ", "         "],
        [" /\\_/\\  ", "( o . o )", " )     ( ", "(_)   (_)", "         ", "   ~     ", "         "],
        [" /\\_/\\  ", "( o . o )", " )     ( ", "(_)   (_)", "         ", "        ~", "         "],
    ],
    "hype": [
        [" /\\_/\\  ", "( ^ . ^ )", " )     ( ", "(_)   (_)", "~ ~ ~ ~ ~", "         ", "         "],
        ["  /\\_/\\ ", " ( ^ . ^)", "  )     (", " (_)   (_", " ~ ~ ~ ~ ", "         ", "         "],
    ],
    "hyperpop": [
        [" /\\_/\\* ", "( oWo )  ", "*)     (*", "(_)   (_)", "* ~ * ~ *", "         ", "  * * *  "],
        [" /\\_/\\  ", "(*>w<*)  ", " )     ( ", "(_)   (_)", "  ~ ~ ~  ", " *     * ", "         "],
        ["*/\\_/\\  ", "( ^v^ ) *", "*)     ( ", "(_)   (_)", "~ ~ ~ ~ ~", "         ", "  *   *  "],
        [" /\\_/\\* ", "( xOx )  ", " )   * ( ", "(_)   (_)", "~ * ~ * ~", " *       ", "    * *  "],
    ],
}

anim = {"vibe": "relaxed", "frame": 0}


def detect_vibe_ai(title, artist):
    if not client: return
    try:
        res = client.chat.completions.create(
            model=MODEL, timeout=8, max_tokens=5,
            messages=[
                {"role": "system", "content": "Classify music into exactly one of: slow, relaxed, hype, hyperpop. Reply with only the single word."},
                {"role": "user",   "content": f'"{title}" by {artist}' if artist else f'"{title}"'},
            ])
        word = res.choices[0].message.content.strip().lower()
        if word in ANIM_FRAMES:
            anim["vibe"]  = word
            anim["frame"] = 0
            if _app:
                _app.call_from_thread(_app.refresh_creature)
            log.debug(f"vibe: {word}")
    except Exception as e:
        log.debug(f"detect_vibe_ai: {e}")


# ─── Life signs ───────────────────────────────────────────────────────────────
COMMENT_PROBABILITY = 0.25


def _fire_end_of_track_reaction():
    if not client: return
    with _state_lock:
        title  = state["display_title"]
        artist = state["artist"]
        recent = [m for m in state["chat_history"][-10:] if m["role"] in ("user", "assistant")][-4:]
    if not title or title in ("Silence", "Fetching...", "Loading..."): return
    track_ctx = f'"{title}"' + (f" by {artist}" if artist else "")
    history_block = "\n".join(
        f"{'You' if m['role'] == 'user' else 'Roommate'}: {m['content']}" for m in recent
    ) if recent else ""
    prompt = (f"Track ending: {track_ctx}.\n"
              + (f"Recent chat:\n{history_block}\n\n" if history_block else "")
              + "React in one short sentence or less. No questions. No offers. "
                "Output nothing if nothing fits.")
    try:
        res = client.chat.completions.create(
            model=MODEL, timeout=10, max_tokens=60,
            messages=[{"role": "system", "content": CHAT_SYSTEM},
                      {"role": "user",   "content": prompt}])
        reply = res.choices[0].message.content.strip().strip('"\'')
        if reply and len(reply) > 3:
            _ui_append_chat({"role": "assistant", "content": reply})
    except Exception as e:
        log.debug(f"end-of-track reaction: {e}")


def _fire_new_track_comment(title, artist):
    if not client or random.random() > COMMENT_PROBABILITY: return
    with _state_lock:
        last_chat = state["_last_chat_time"]
    if (time.time() - last_chat) > CHAT_IDLE_SECS: return
    track_ctx = f'"{title}"' + (f" by {artist}" if artist else "")
    try:
        res = client.chat.completions.create(
            model=MODEL, timeout=10, max_tokens=40,
            messages=[{"role": "system", "content": CHAT_SYSTEM},
                      {"role": "user",   "content": f"New track just started: {track_ctx}. Say one very short thing — like a person on the sofa noticing it. No questions."}])
        line = res.choices[0].message.content.strip().strip('"\'')
        if line and len(line) > 2:
            _ui_append_chat({"role": "assistant", "content": line})
    except Exception as e:
        log.debug(f"new track comment: {e}")


def _fire_restore_greeting(saved_at, last_track):
    if not client: return
    gap_mins = int((time.time() - saved_at) / 60) if saved_at else None
    gap_str  = (f"{gap_mins} minute{'s' if gap_mins != 1 else ''} ago"
                if gap_mins is not None and gap_mins < 180 else "a while ago")
    track_hint = f'Last track: "{last_track}".' if last_track else ""
    try:
        res = client.chat.completions.create(
            model=MODEL, timeout=10, max_tokens=40,
            messages=[{"role": "system", "content": CHAT_SYSTEM},
                      {"role": "user",   "content": f"The user just came back. They left {gap_str}. {track_hint} Say something very brief. No questions."}])
        line = res.choices[0].message.content.strip().strip('"\'')
        if line and len(line) > 2:
            _ui_append_chat({"role": "assistant", "content": line})
    except Exception as e:
        log.debug(f"restore greeting: {e}")


# ─── Playlist manager ─────────────────────────────────────────────────────────
def _playlist_path(name):
    safe = re.sub(r"[^\w\s-]", "", name).strip().lower()
    return os.path.join(PLAYLISTS_DIR, f"{re.sub(chr(32) + '+', '_', safe)}.json")

def playlist_save(name):
    url = mpv_get("path")
    if not url or "idle" in str(url).lower():
        _ui_set_status("Nothing playing to save."); return
    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
    path   = _playlist_path(name)
    tracks = []
    if os.path.exists(path):
        try:
            with open(path) as f: tracks = json.load(f)
        except: pass
    if any(t.get("url") == url for t in tracks):
        _ui_set_status(f"Already in '{name}'."); return
    tracks.append({"url": url, "title": state["display_title"], "artist": state["artist"]})
    with open(path, "w") as f: json.dump(tracks, f, indent=2)
    _ui_set_status(f"Saved to '{name}' ({len(tracks)} track{'s' if len(tracks) != 1 else ''}).")

def playlist_load(name):
    path = _playlist_path(name)
    if not os.path.exists(path):
        _ui_set_status(f"No playlist named '{name}'."); return
    try:
        with open(path) as f: tracks = json.load(f)
    except Exception as e:
        _ui_set_status(f"Couldn't read '{name}'."); return
    if not tracks:
        _ui_set_status(f"'{name}' is empty."); return
    urls = [t["url"] for t in tracks if t.get("url")]
    send_mpv({"command": ["loadfile", urls[0], "replace"]})
    for url in urls[1:]: send_mpv({"command": ["loadfile", url, "append"]})
    _ui_set_status(f"Loaded '{name}' — {len(urls)} track{'s' if len(urls) != 1 else ''}.")

def playlist_remove(name, index):
    path = _playlist_path(name)
    if not os.path.exists(path):
        _ui_set_status(f"No playlist named '{name}'."); return
    try:
        with open(path) as f: tracks = json.load(f)
    except:
        _ui_set_status(f"Couldn't read '{name}'."); return
    if not 1 <= index <= len(tracks):
        _ui_set_status(f"No track {index} in '{name}' ({len(tracks)} tracks)."); return
    removed = tracks.pop(index - 1)
    with open(path, "w") as f: json.dump(tracks, f, indent=2)
    _ui_set_status(f"Removed '{removed.get('title', '?')}' from '{name}'.")

def playlist_list(name=None):
    """Returns markup string for browser."""
    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
    if name:
        path = _playlist_path(name)
        if not os.path.exists(path):
            return f"[dim]No playlist named '{name}'.[/dim]"
        try:
            with open(path) as f: tracks = json.load(f)
        except:
            return "[dim]Couldn't read that playlist.[/dim]"
        if not tracks:
            return f"[bold]{rich_escape(name)}[/bold]\n\n[dim]Empty.[/dim]"
        parts = [f"[bold]{rich_escape(name)}[/bold]", ""]
        for i, t in enumerate(tracks, 1):
            t_title  = rich_escape(t.get("title") or "Unknown")
            t_artist = t.get("artist", "")
            suffix   = f"  [dim]{rich_escape(t_artist)}[/dim]" if t_artist else ""
            parts.append(f"[dim]{i}.[/dim] {t_title}{suffix}")
        return "\n".join(parts)
    files = sorted(f for f in os.listdir(PLAYLISTS_DIR) if f.endswith(".json"))
    if not files:
        return "[bold]Playlists[/bold]\n\n[dim]No saved playlists yet.[/dim]"
    parts = ["[bold]Playlists[/bold]", ""]
    for fname in files:
        display = fname[:-5].replace("_", " ")
        try:
            with open(os.path.join(PLAYLISTS_DIR, fname)) as f: count = len(json.load(f))
        except:
            count = "?"
        s = "s" if count != 1 else ""
        parts.append(f"  [bold]{display}[/bold]  [dim]{count} track{s}[/dim]")
    return "\n".join(parts)


def show_queue():
    """Returns markup string for browser. Resolves titles for raw URLs."""
    playlist    = mpv_get("playlist", fallback=[])
    current_pos = mpv_get("playlist-pos", fallback=0)
    if not isinstance(current_pos, int): current_pos = 0
    if not playlist:
        return "[bold]Queue[/bold]\n\n[dim]Queue is empty.[/dim]"
    parts = ["[bold]Queue[/bold]", ""]
    start = max(0, current_pos)
    for i, item in enumerate(playlist[start: start + 20], start=start):
        is_cur = (i == current_pos)
        prefix = "[bold green]▶[/bold green]" if is_cur else " "
        num    = f"[bold green]{i+1}.[/bold green]" if is_cur else f"[dim]{i+1}.[/dim]"
        if isinstance(item, dict):
            title    = item.get("title") or ""
            filename = item.get("filename", "")
            if not title and "v=" in filename:
                vid = filename.split("v=")[1].split("&")[0]
                try:
                    title = subprocess.check_output(
                        [YTDLP_PATH, "--get-title", "--quiet", vid], timeout=8
                    ).decode().strip()
                except Exception:
                    title = vid
            elif not title:
                title = filename[-30:] if filename else "Unknown"
        else:
            title = str(item)[-30:]
        parts.append(f"{prefix} {num} {rich_escape(title)}")
    return "\n".join(parts)


# ─── Session persistence ──────────────────────────────────────────────────────
def save_state():
    try:
        playlist = mpv_get("playlist", fallback=[])
        pos      = mpv_get("playlist-pos", fallback=0)
        if not isinstance(pos, int): pos = 0
        time_pos = mpv_get("time-pos", fallback=0)
        urls = []
        for item in playlist:
            if isinstance(item, dict):
                fn = item.get("filename", "")
                if fn and "youtube.com/watch?v=" in fn:
                    urls.append(fn.split("&list=")[0])
        with _state_lock:
            history = list(state["chat_history"])
        data = {"playlist": urls, "playlist_pos": pos, "time_pos": round(time_pos or 0),
                "volume": cfm._user_vol, "chat_history": history, "saved_at": time.time()}
        os.makedirs(COMPANION_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f: json.dump(data, f, indent=2)
        log.debug(f"state saved — {len(urls)} tracks")
    except Exception as e:
        log.error(f"save_state: {e}")


def restore_state():
    if not os.path.exists(STATE_FILE): return
    try:
        with open(STATE_FILE) as f: data = json.load(f)
        with _state_lock:
            state["chat_history"] = data.get("chat_history", [])
        urls     = data.get("playlist", [])
        pos      = data.get("playlist_pos", 0)
        time_pos = data.get("time_pos", 0)
        volume   = data.get("volume", 100)
        saved_at = data.get("saved_at")
        last_track = ""
        with _state_lock:
            for msg in reversed(state["chat_history"]):
                if msg["role"] == "divider":
                    last_track = msg["content"]; break
        if saved_at:
            threading.Thread(target=_fire_restore_greeting, args=(saved_at, last_track), daemon=True).start()
        if not urls: return
        start_idx = max(0, min(pos, len(urls) - 1))
        seed_vid  = urls[start_idx].split("v=")[1].split("&")[0] if "v=" in urls[start_idx] else urls[start_idx]
        st(_last_divider_vid=seed_vid)
        send_mpv({"command": ["loadfile", urls[start_idx], "replace"]})
        for url in urls[start_idx + 1:]: send_mpv({"command": ["loadfile", url, "append"]})
        for url in urls[:start_idx]:     send_mpv({"command": ["loadfile", url, "append"]})
        if time_pos > 3:
            time.sleep(1.5)
            send_mpv({"command": ["seek", time_pos, "absolute"]})
        threading.Thread(target=scout_real_title, args=(urls[start_idx],),
                         kwargs={"silent": True}, daemon=True).start()
        cfm._user_vol = volume
        send_mpv({"command": ["set_property", "volume", volume]})
        _ui_set_status("Session restored.", secs=3)
        log.debug(f"state restored — {len(urls)} tracks")
    except Exception as e:
        log.error(f"restore_state: {e}")


# ─── Textual App ──────────────────────────────────────────────────────────────
HINT = "pp · skip · vol n% · play · queue · album · save · load · pls · rm · abt · qq · clear · brb"

class RoommateApp(App):
    TITLE = "Music Roommate"

    BACKGROUND = "#0d0d0d"

    DEFAULT_CSS = """
    Screen {
        margin: 0 !important;
        padding: 0 !important;
        background: #0d0d0d !important;
    }
    """

    CSS = """
    Screen {
        layout: vertical;
        background: #0d0d0d;
        height: 100%;
        width: 100%;
        margin: 0;
        padding: 0;
    }

    #feed {
        height: 3;
        border: round $success-darken-1;
        padding: 0 1;
        background: #111111;
        margin: 0;
    }

    #bento {
        height: 1fr;
        width: 100%;
        layout: horizontal;
        background: #0d0d0d;
        margin: 0;
        padding: 0;
    }

    #left {
        width: 40%;
        layout: vertical;
        background: #0d0d0d;
        margin: 0;
        padding: 0;
    }

    #creature {
        height: 1fr;
        border: round $panel-darken-1;
        background: #111111;
        content-align: center middle;
        padding: 1 2;
    }

    #browser {
        height: 1fr;
        border: round $primary-darken-3;
        background: #0d0d0d;
        scrollbar-background: #0d0d0d;
        scrollbar-color: #333333;
        overflow: hidden auto;
    }

    #chat {
        width: 60%;
        border: round $panel-darken-1;
        background: #0d0d0d;
        scrollbar-background: #0d0d0d;
        scrollbar-color: #333333;
        overflow: hidden auto;
    }

    #cmd {
        height: 3;
        border: round $warning-darken-1;
        padding: 0 1;
        background: #0d0d0d;
        margin: 0;
    }
    """

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        yield Static("", id="feed")
        with Horizontal(id="bento"):
            with Vertical(id="left"):
                yield Static("", id="creature")
                yield RichLog(id="browser", wrap=True, highlight=False, markup=True)
            yield RichLog(id="chat", wrap=False, highlight=False, markup=True, auto_scroll=True)
        yield Input(id="cmd", placeholder=HINT)

    async def on_mount(self) -> None:
        global _app
        _app = self
        if not cfm.start():
            self.exit(message="Failed to start mpv.")
            return
        threading.Thread(target=monitor, daemon=True).start()
        restore_state()
        chat_log = self.query_one("#chat", RichLog)
        with _state_lock:
            history = list(state["chat_history"])
        for entry in history:
            self._render_entry(chat_log, entry)
        chat_log.scroll_end(animate=False)
        self.sync_feed()
        self.refresh_creature()
        self.set_browser(show_queue())   # default browser content
        self.set_interval(3.0, self._advance_creature)
        self.query_one("#cmd", Input).focus()

    def set_browser(self, renderable) -> None:
        """Replace browser panel contents with a Rich renderable."""
        browser = self.query_one("#browser", RichLog)
        browser.clear()
        browser.write(renderable)

    def on_unmount(self) -> None:
        st(is_running=False)
        save_state()
        cfm.quit()

    # ── Input ──────────────────────────────────────────────────────────────────
    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        event.input.clear()
        if raw:
            self._handle_command(raw)

    def _handle_command(self, raw: str) -> None:
        lower = raw.lower()

        if lower == "brb":
            with _state_lock:
                state["_session_played"].clear()
            self.append_chat_entry({"role": "assistant", "content": "later."})
            self.set_timer(0.5, self.exit)
            return

        if lower == "pp":
            send_mpv({"command": ["cycle", "pause"]}); return
        if lower == "skip":
            send_mpv({"command": ["playlist-next"]}); return
        if lower == "clear":
            send_mpv({"command": ["playlist-clear"]}); return

        if lower == "qq":
            def _qq():
                panel = show_queue()
                _call_ui(lambda: self.set_browser(panel))
            threading.Thread(target=_qq, daemon=True).start()
            return
        if lower == "pls":
            self.set_browser(playlist_list()); return

        vol_m = re.match(r"^vol\s+(\d+)%?$", lower)
        if vol_m:
            level = max(0, min(150, int(vol_m.group(1))))
            cfm.set_user_vol(level)
            _ui_set_status(f"Volume: {level}%"); return

        save_m = re.match(r"^save\s+(.+)", raw, re.I)
        if save_m:
            playlist_save(save_m.group(1).strip()); return

        load_m = re.match(r"^load\s+(.+)", raw, re.I)
        if load_m:
            threading.Thread(target=playlist_load, args=(load_m.group(1).strip(),), daemon=True).start(); return

        rm_m = re.match(r"^rm\s+(.+?)\s+(\d+)$", raw, re.I)
        if rm_m:
            playlist_remove(rm_m.group(1).strip(), int(rm_m.group(2))); return

        pls_m = re.match(r"^pls\s+(.+)", raw, re.I)
        if pls_m:
            self.set_browser(playlist_list(pls_m.group(1).strip())); return

        if lower == "abt":
            with _state_lock:
                song, artist, year = state["display_title"], state["artist"], state["year"]
            if not song or song in ("Silence", "Fetching...", "Loading..."):
                _ui_set_status("Nothing playing."); return
            def _abt():
                trivia = fetch_trivia(song, artist, year)
                if trivia: _ui_append_chat({"role": "assistant", "content": trivia})
            threading.Thread(target=_abt, daemon=True).start(); return

        play_m  = re.match(r"^play\s+(.+)",  lower)
        queue_m = re.match(r"^queue\s+(.+)", lower)
        album_m = re.match(r"^album\s+(.+)", lower)

        if play_m:
            threading.Thread(target=search_and_queue, args=(play_m.group(1), "replace"), daemon=True).start(); return
        if queue_m:
            threading.Thread(target=search_and_queue, args=(queue_m.group(1), "append"), daemon=True).start(); return
        if album_m:
            threading.Thread(target=search_and_queue_album, args=(album_m.group(1),), daemon=True).start(); return

        # AI chat — runs in thread, shows status while waiting
        _ui_set_status("...", 0)
        threading.Thread(target=self._do_chat, args=(raw,), daemon=True).start()

    def _do_chat(self, raw: str) -> None:
        fetch_chat(raw)
        _ui_set_status(None, 0)

    # ── Widget update methods (called from main thread or via call_from_thread) ─
    def sync_feed(self) -> None:
        with _state_lock:
            title  = state["display_title"]
            artist = state["artist"]
            status = state["status_msg"]
        parts = [("♫  ", "bold cyan"), (title, "bold white")]
        if artist:  parts += [("  ", ""), (artist, "dim")]
        if cfm._fading: parts += [("  ↔", "dim yellow")]
        content = Text.assemble(*parts)
        if status:
            content = Text.assemble(*parts, ("\n> ", "bold yellow"), (status, "yellow"))
        self.query_one("#feed", Static).update(content)

    def append_chat_entry(self, entry: dict) -> None:
        chat_log = self.query_one("#chat", RichLog)
        self._render_entry(chat_log, entry)
        chat_log.scroll_end(animate=False)

    def _render_entry(self, chat_log: RichLog, entry: dict) -> None:
        role    = entry["role"]
        content = entry["content"]

        def _wrap_left(text, width=23):
            lines = textwrap.wrap(text, width=width)
            return "\n".join(lines)

        def _wrap_right(text, width=23):
            lines = textwrap.wrap(text, width=width)
            return "\n".join(l.rjust(width) for l in lines)

        if role == "divider":
            lines = textwrap.wrap(content, width=20)
            wrapped = "\n".join(f"{l:^20}" for l in lines)
            chat_log.write(f"[dim]───\n{rich_escape(wrapped)}\n───[/dim]")
        elif role == "user":
            chat_log.write(f"[bold yellow]{'You:':>23}[/bold yellow]\n{rich_escape(_wrap_right(content))}")
        elif role == "assistant":
            clean = strip_protocol_tags(content).strip('"\'').strip()
            if clean:
                chat_log.write(f"[bold cyan]Roommate:[/bold cyan]\n{rich_escape(_wrap_left(clean))}")
        elif role == "info":
            chat_log.write(content)

    def refresh_creature(self) -> None:
        frames    = ANIM_FRAMES[anim["vibe"]]
        frame_txt = "\n".join(frames[anim["frame"] % len(frames)])
        label     = f"\n\n~ {anim['vibe']} ~"
        self.query_one("#creature", Static).update(
            Align.center(Text(frame_txt + label, justify="center"))
        )

    def _advance_creature(self) -> None:
        frames = ANIM_FRAMES[anim["vibe"]]
        anim["frame"] = (anim["frame"] + 1) % len(frames)
        self.refresh_creature()


# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    log.info("starting")
    # Paint the terminal's own background before Textual takes over,
    # so any gap between the app and the window edge matches our color.
    sys.stdout.write("\033[?25l")          # hide cursor during transition
    sys.stdout.write("\033[48;2;13;13;13m") # set terminal bg to #0d0d0d
    sys.stdout.write("\033[2J\033[H")      # clear screen with that bg
    sys.stdout.flush()
    app = RoommateApp()
    app.run()

if __name__ == "__main__":
    main()
