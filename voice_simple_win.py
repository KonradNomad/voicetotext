#!/usr/bin/env python3
"""
Voice to Text — Simple (Windows / cross-platform edition)
────────────────────────────────────────────────────────────────────
Accessible voice-to-text with big buttons, built in Tkinter so it runs
on Windows with no extra GUI install (Tkinter ships with Python).

Four big buttons (each configurable on/off via the gear):
  RECORD / STOP  ·  COPY TEXT  ·  READ ALOUD  ·  START OVER

Features:
  • Groq Whisper transcription
  • Gentle AI cleanup that never adds words (fixes the phantom-word issue)
  • Silence detection + hallucination filtering
  • Read-aloud via the built-in system voice (SAPI5 on Windows)
  • Groq API key entered in the gear settings (no terminal needed)

Runs on Windows and Linux. Build a standalone .exe with PyInstaller
(see the note at the bottom of this file).
"""

APP_VERSION = "3.18.0"

import os, sys, wave, tempfile, threading, subprocess, time, json, re, struct
from pathlib import Path

# ─── Dependency check ─────────────────────────────────────────────────────────
def _pip_install(pkgs):
    args = [sys.executable, "-m", "pip", "install", "-q"] + pkgs
    # --break-system-packages only exists / is needed on some Linux setups
    if sys.platform.startswith("linux"):
        args.insert(4, "--break-system-packages")
    subprocess.check_call(args)

def check_and_install_deps():
    required = {
        "pyaudio": "pyaudio",
        "groq": "groq",
        "requests": "requests",
        "pyttsx3": "pyttsx3",
        "pyautogui": "pyautogui",
    }
    missing = []
    for mod, pkg in required.items():
        try: __import__(mod)
        except ImportError: missing.append(pkg)
    if missing:
        print(f"Installing: {', '.join(missing)}")
        _pip_install(missing)
        os.execv(sys.executable, [sys.executable] + sys.argv)

check_and_install_deps()

import pyaudio, requests
from groq import Groq
import pyttsx3
import pyautogui
import tkinter as tk
from tkinter import ttk, messagebox

pyautogui.FAILSAFE = False  # don't abort if mouse hits screen corner

# ─── Windows DPI awareness ─────────────────────────────────────────────────────
# Different computers use different display scaling (100%, 125%, 150%, etc).
# Without telling Windows this app understands scaling, Windows silently
# stretches or shrinks the whole rendered window to compensate — which is
# exactly what made everything (buttons, text, the floating widget) look
# fine on one machine and distorted on another. Declaring DPI awareness
# here, before any window is created, stops Windows from doing that, so
# the app draws at the real resolution and can scale itself properly
# instead (see DPI_SCALE / px() further down).
if sys.platform.startswith("win"):
    try:
        import ctypes as _dpi_ctypes
        try:
            _dpi_ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
        except Exception:
            _dpi_ctypes.windll.user32.SetProcessDPIAware()       # older Windows fallback
    except Exception:
        pass

# ─── Layout-independent typing on Windows ─────────────────────────────────────
# pyautogui.write() simulates individual key presses assuming a US keyboard
# layout, so characters that sit on different keys on other layouts can come
# out wrong or missing (this is what caused the missing @ symbol earlier).
#
# Rather than trying to detect and adapt to whichever of the many keyboard
# layouts is active — fragile, since there are dozens with different dead
# keys and symbol placements — Windows offers a way to sidestep the problem
# entirely: SendInput with the KEYEVENTF_UNICODE flag injects the actual
# character directly at the OS level, not a simulated physical key press.
# It works correctly no matter which layout is active, because it never
# goes through layout mapping in the first place. This works automatically
# for every layout without the app needing to know what layout is in use.
if sys.platform.startswith("win"):
    import ctypes

    _PUL = ctypes.POINTER(ctypes.c_ulong)

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort),
                    ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong),
                    ("dwExtraInfo", _PUL)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", ctypes.c_ulong),
                    ("wParamL", ctypes.c_short),
                    ("wParamH", ctypes.c_ushort)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long),
                    ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong),
                    ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong),
                    ("dwExtraInfo", _PUL)]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", _INPUT_UNION)]

    _INPUT_KEYBOARD = 1
    _KEYEVENTF_UNICODE = 0x0004
    _KEYEVENTF_KEYUP = 0x0002

    def _send_unicode_char(ch, keyup=False):
        extra = ctypes.c_ulong(0)
        flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if keyup else 0)
        ki = _KEYBDINPUT(0, ord(ch), flags, 0, ctypes.pointer(extra))
        inp = _INPUT(_INPUT_KEYBOARD, _INPUT_UNION(ki=ki))
        ctypes.windll.user32.SendInput(1, ctypes.pointer(inp), ctypes.sizeof(inp))

    def type_unicode_windows(text, delay=0.004):
        """Type text one character at a time via direct Unicode injection —
        correct on any keyboard layout, because layout is never consulted."""
        for ch in text:
            if ch == "\n":
                # Enter needs a real virtual-key event, not a unicode one
                ctypes.windll.user32.keybd_event(0x0D, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0x0D, 0, _KEYEVENTF_KEYUP, 0)
            else:
                _send_unicode_char(ch, keyup=False)
                _send_unicode_char(ch, keyup=True)
            if delay:
                time.sleep(delay)

# ─── Paths / config ───────────────────────────────────────────────────────────
# Config lives in a per-user folder that works on both Windows and Linux.
if sys.platform.startswith("win"):
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "VoiceToText"
else:
    CONFIG_DIR = Path.home() / ".config" / "voicetyper"
CONFIG_FILE = CONFIG_DIR / "config.json"
CRASH_LOG   = CONFIG_DIR / "crash_log.txt"

# ─── Crash logging ────────────────────────────────────────────────────────────
# The built .exe has no console, so errors would vanish silently. This writes
# any crash (and handled errors) to crash_log.txt so problems can be diagnosed.
import traceback as _tb
import datetime as _dt

def log_error(where, exc):
    """Append an error with full traceback and useful context to the crash log."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write("\n" + "="*60 + "\n")
            f.write(f"TIME:    {_dt.datetime.now().isoformat()}\n")
            f.write(f"VERSION: {APP_VERSION}\n")
            f.write(f"WHERE:   {where}\n")
            f.write(f"PYTHON:  {sys.version.split()[0]}  PLATFORM: {sys.platform}\n")
            f.write(f"ERROR:   {type(exc).__name__}: {exc}\n")
            f.write("TRACEBACK:\n")
            f.write("".join(_tb.format_exception(type(exc), exc, exc.__traceback__)))
            f.write("\n")
    except Exception:
        pass  # never let logging itself crash the app

def _global_excepthook(exc_type, exc_value, exc_tb):
    log_error("uncaught", exc_value.with_traceback(exc_tb) if exc_value else exc_type())
    # Also print in case a console is attached (dev/testing)
    _tb.print_exception(exc_type, exc_value, exc_tb)

sys.excepthook = _global_excepthook

def safe_thread(fn):
    """Decorator: wrap a thread target so its errors go to the crash log."""
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:
            log_error(f"thread:{fn.__name__}", e)
    return wrapper

# ─── Activity log ─────────────────────────────────────────────────────────────
# A running record of what the app is doing — button clicks, recordings,
# transcription results, mic tests — so that if something looks wrong, the
# exact sequence of events can be copied and sent for diagnosis, instead of
# just "it didn't work".
ACTIVITY_LOG = CONFIG_DIR / "activity_log.txt"
_ACTIVITY_MAX_LINES = 800   # keep the file from growing forever

# Updated whenever something meaningful happens (button clicks, recording,
# etc.) — used by the auto-close-after-inactivity feature to know how long
# the app has genuinely sat unused, not just idle time since launch.
_last_activity_time = [time.time()]
_ACTIVITY_CATEGORIES = {"BUTTON", "RECORD", "TRANSCRIBE", "CLEANUP", "TYPE", "MIC_TEST"}

def log_event(category, message):
    """Append one line to the activity log: [HH:MM:SS] CATEGORY: message"""
    if category in _ACTIVITY_CATEGORIES:
        _last_activity_time[0] = time.time()
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        line = f"[{_dt.datetime.now().strftime('%H:%M:%S')}] {category}: {message}\n"
        with open(ACTIVITY_LOG, "a", encoding="utf-8") as f:
            f.write(line)
        _trim_activity_log()
    except Exception:
        pass  # logging must never crash the app

def seconds_since_last_activity():
    return time.time() - _last_activity_time[0]

def _trim_activity_log():
    """Keep the activity log to a reasonable size by dropping old lines."""
    try:
        if not ACTIVITY_LOG.exists():
            return
        with open(ACTIVITY_LOG, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if len(lines) > _ACTIVITY_MAX_LINES:
            with open(ACTIVITY_LOG, "w", encoding="utf-8") as f:
                f.writelines(lines[-_ACTIVITY_MAX_LINES:])
    except Exception:
        pass

def build_full_report():
    """
    Assemble one combined, copyable report: system info, the activity log,
    and the tail of the crash log. This is what the Settings 'Report' box
    shows and what gets copied to the clipboard.
    """
    parts = []
    parts.append("=" * 50)
    parts.append("VOICE TO TEXT — DIAGNOSTIC REPORT")
    parts.append("=" * 50)
    parts.append(f"Version:   {APP_VERSION}")
    parts.append(f"Platform:  {sys.platform}")
    parts.append(f"Python:    {sys.version.split()[0]}")
    parts.append(f"Generated: {_dt.datetime.now().isoformat()}")
    parts.append("")
    parts.append("-" * 50)
    parts.append("RECENT ACTIVITY")
    parts.append("-" * 50)
    try:
        if ACTIVITY_LOG.exists():
            with open(ACTIVITY_LOG, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            parts.append("".join(lines[-150:]).rstrip() or "(no activity recorded yet)")
        else:
            parts.append("(no activity recorded yet)")
    except Exception as e:
        parts.append(f"(couldn't read activity log: {e})")

    parts.append("")
    parts.append("-" * 50)
    parts.append("RECENT ERRORS")
    parts.append("-" * 50)
    try:
        if CRASH_LOG.exists():
            with open(CRASH_LOG, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            # Keep just the last few error blocks so the report isn't huge
            blocks = content.split("=" * 60)
            tail = "=" * 60 + ("=" * 60).join(blocks[-3:]) if len(blocks) > 1 else content
            parts.append(tail.strip() or "(no errors recorded)")
        else:
            parts.append("(no errors recorded)")
    except Exception as e:
        parts.append(f"(couldn't read crash log: {e})")

    return "\n".join(parts)


DEFAULT_CONFIG = {
    "groq_api_key":      "",
    "model":             "whisper-large-v3-turbo",
    "language":          "",
    "ai_cleanup":        True,
    "ai_provider":       "groq",
    "groq_cleanup_model":"llama-3.3-70b-versatile",
    "sample_rate":       16000,
    "channels":          1,
    "show_copy":         True,
    "show_read":         True,
    "show_clear":        True,
    "auto_type":         True,   # type the words where the cursor is
    "type_delay":        5,      # seconds to wait before typing (3-15)
    "show_floating":      True,   # persistent floating record button (on by default)
    "floating_choice_made": False, # becomes True once the user explicitly toggles it
    "button_size":        110,    # main window buttons — independent of the popup
    "floating_size":      110,    # diameter of the floating record button
    "floating_x":         None,   # remembered position after dragging
    "floating_y":         None,
    "floating_show_text": True,   # status text next to the buttons; off = buttons only
    "speech_rate":       165,   # words per minute for read-aloud
    "scale_override":    None,  # manual correction if auto-detected DPI looks wrong
    "auto_close_enabled": False,  # quit automatically after a period of no use
    "auto_close_minutes": 3,      # how long to wait (2-5), only used if enabled
    "auto_launch":       False,   # start automatically when Windows starts
}

def load_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = {**DEFAULT_CONFIG, **json.load(f)}
                # Old saved files (from before the floating widget defaulted
                # to on) can have an explicit "show_floating": false baked
                # in, which would otherwise silently override the new
                # default forever. Force it on until the user has actually
                # made a deliberate choice about it themselves.
                if not cfg.get("floating_choice_made"):
                    cfg["show_floating"] = True
                return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def _uninstall_app():
    """
    Remove the app's traces: desktop shortcut, config folder, and (on Windows,
    when running as a built .exe) schedule the .exe and its folder for deletion
    after the app closes. Leaves Python itself alone.
    """
    import shutil
    # 1. Remove desktop shortcut(s)
    try:
        if sys.platform.startswith("win"):
            desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
        else:
            desktop = Path.home() / "Desktop"
        for name in ["Voice to Text.lnk", "Voice to Text.desktop"]:
            p = desktop / name
            if p.exists():
                p.unlink()
    except Exception:
        pass

    # 2. Remove the config folder (key + settings)
    try:
        if CONFIG_DIR.exists():
            shutil.rmtree(CONFIG_DIR, ignore_errors=True)
    except Exception:
        pass

    # 3. If running as a frozen .exe on Windows, schedule self-deletion.
    #    A tiny batch file waits for the app to close, deletes the exe,
    #    then deletes itself.
    try:
        if sys.platform.startswith("win") and getattr(sys, "frozen", False):
            exe = Path(sys.executable)
            folder = exe.parent
            bat = folder / "_remove.bat"
            bat.write_text(
                "@echo off\n"
                "timeout /t 2 /nobreak >nul\n"
                f'del /f /q "{exe}" >nul 2>&1\n'
                f'del /f /q "{folder}\\_remove.bat" >nul 2>&1\n'
            )
            subprocess.Popen(["cmd", "/c", str(bat)],
                             creationflags=0x00000008)  # DETACHED_PROCESS
    except Exception:
        pass

# ─── Gentle AI prompt (strictly no additions) ────────────────────────────────
GENTLE_PROMPT = """You are a silent transcription editor. Clean up this dictated text so it reads clearly. You output ONLY the cleaned result and never anything else.

ABSOLUTE RULES:
1. NEVER add words, sentences, ideas, or information that are not in the original. This is the most important rule.
2. If the text is empty, gibberish, or just noise with no real words, your entire response must be completely blank — zero characters. Do NOT write a sentence about it being noise, do NOT explain, do NOT mention rules. Just output nothing.
3. NEVER explain yourself. NEVER mention these rules, your reasoning, or what you did. NEVER write things like "there is no text to clean", "this appears to be noise", "should be rewritten as", "note:", or any commentary about the text or the task. Your response is ONLY the final cleaned text itself, nothing wrapped around it.
4. Only fix: grammar, spelling, punctuation, capitalisation, and obvious speech-recognition errors.
5. Remove filler words (um, uh, er) and false starts, but keep the speaker's own words and meaning exactly.
6. Do not summarise, expand, rephrase for style, or "improve" the content. Keep it faithful.
7. Do not add greetings, sign-offs, or pleasantries like "thank you" or "hello" unless the speaker clearly said them.

CONTACT INFO AND ADDRESSES — when the speaker is dictating an email address, website, phone number, or postal address, either alone or as part of a sentence:
- Convert spelled-out or spoken-out-loud forms into their normal compact written form. "k dash o dash n at gmail dot com" becomes "k-o-n@gmail.com". "double you double you double you dot example dot com" becomes "www.example.com". "oh one two three, four five six, seven eight nine oh" becomes "0123 456 7890".
- If the ENTIRE recording was just that one piece of information with nothing else, output ONLY that item — no leading or trailing words, no "here is your email", no explanation of what you changed. Just the clean email/website/phone/address by itself.
- If it was said as part of a fuller sentence, keep it naturally inline within that sentence, cleaned up the same way.

If you are unsure whether something was said, leave it out. Faithfulness matters more than polish. Remember: your entire reply is nothing but the final text — not a description of it, not a comment on it, just it."""

# Phrases that indicate the AI ignored the "no commentary" instruction and
# explained itself instead of just returning clean text. If the cleaned
# result contains any of these, the whole response is treated as invalid
# and discarded rather than typed out.
AI_COMMENTARY_MARKERS = [
    "there is no text", "no text to clean", "appears to be noise",
    "appears to be gibberish", "cannot determine", "unable to determine",
    "should be rewritten as", "should be written as", "rewritten as",
    "according to rule", "as per the rules", "as an ai", "i cannot",
    "i'm unable to", "note:", "please note", "this seems to be",
    "this looks like noise", "not actual words", "doesn't contain",
    "does not contain", "no clear words", "no discernible words",
]

def contains_ai_commentary(text):
    t = text.lower()
    return any(marker in t for marker in AI_COMMENTARY_MARKERS)

HALLUCINATION_PHRASES = {
    "thank you", "thank you.", "thanks for watching", "thanks for watching.",
    "thank you for watching", "thank you for watching.", "please subscribe",
    "subscribe", "thanks for watching!", "you", ".", "bye", "bye.",
    "thank you very much", "thank you very much.", "okay", "ok",
    "thank you so much", "thank you so much.", "see you next time",
    "i'll see you next time", "so", "the", "thank", "thank you for your attention",
    "please subscribe to my channel", "don't forget to subscribe",
}

def is_probable_hallucination(text):
    t = text.strip().lower()
    if not t:
        return True
    if t in HALLUCINATION_PHRASES:
        return True
    words = t.split()
    if len(words) <= 4 and t.rstrip(".!?") in HALLUCINATION_PHRASES:
        return True
    return False

# ─── Audio (manual RMS, no deprecated audioop) ────────────────────────────────
class AudioRecorder:
    def __init__(self, rate=16000, channels=1):
        self.rate, self.channels = rate, channels
        self.chunk, self.fmt = 1024, pyaudio.paInt16
        self._frames, self._recording = [], False
        self._pa = self._stream = self._thread = None
        self.open_error = None   # set if the mic couldn't be opened at all

    def start(self):
        self._frames, self._recording = [], True
        self.open_error = None
        try:
            self._pa = pyaudio.PyAudio()
            self._stream = self._pa.open(format=self.fmt, channels=self.channels,
                                         rate=self.rate, input=True,
                                         frames_per_buffer=self.chunk)
        except Exception as e:
            # Couldn't even open the microphone — usually a permissions or
            # device problem, not "no speech". Record this distinctly.
            self.open_error = str(e)
            self._recording = False
            log_error("AudioRecorder.start", e)
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._recording:
            try:
                self._frames.append(self._stream.read(self.chunk, exception_on_overflow=False))
            except Exception as e:
                log_error("AudioRecorder._loop", e)
                break

    def stop(self):
        self._recording = False
        if self._thread: self._thread.join(timeout=2)
        if self._stream:
            try: self._stream.stop_stream(); self._stream.close()
            except Exception: pass
        if self._pa:
            try: self._pa.terminate()
            except Exception: pass

    def save_wav(self, path):
        if not self._frames: return False
        with wave.open(path, "wb") as wf:
            wf.setnchannels(self.channels); wf.setsampwidth(2)
            wf.setframerate(self.rate); wf.writeframes(b"".join(self._frames))
        return True

    def duration(self):
        return len(self._frames) * self.chunk / self.rate

    def _rms_and_peak(self):
        """Compute RMS and peak amplitude without the deprecated audioop module."""
        data = b"".join(self._frames)
        if len(data) < 2:
            return 0, 0
        count = len(data) // 2
        samples = struct.unpack(f"<{count}h", data[:count*2])
        if not samples:
            return 0, 0
        peak = max(abs(s) for s in samples)
        sq = sum(s*s for s in samples) / count
        rms = int(sq ** 0.5)
        return rms, peak

    def is_mostly_silent(self):
        if not self._frames:
            return True
        rms, peak = self._rms_and_peak()
        return rms < 120 and peak < 900

    def silence_kind(self):
        """
        Distinguish a genuinely quiet recording from one where Windows (or
        another OS) is blocking microphone access. When access is blocked,
        the stream typically still "succeeds" but every sample comes back
        as exact zero — never even room tone or a breath. That's a strong
        signal it's a permissions problem, not "please talk louder".
        Returns: 'ok' (had real audio), 'quiet' (low but plausible), or
        'blocked' (looks like no real signal ever reached the app).
        """
        if not self._frames:
            return "blocked"
        rms, peak = self._rms_and_peak()
        if peak == 0:
            return "blocked"
        if rms < 120 and peak < 900:
            return "quiet"
        return "ok"

    # ── Streaming support ─────────────────────────────────────────────────────
    def frame_count(self):
        return len(self._frames)

    def slice_bytes(self, start, end):
        """Return raw audio bytes for frames[start:end] (thread-safe enough:
        _frames is append-only during recording, so slicing a settled range
        is safe)."""
        return b"".join(self._frames[start:end])

    def recent_is_pause(self, n_frames=6, threshold=250):
        """True if the last n_frames are quiet — a natural pause in speech."""
        if len(self._frames) < n_frames:
            return False
        data = b"".join(self._frames[-n_frames:])
        if len(data) < 2:
            return True
        count = len(data) // 2
        samples = struct.unpack(f"<{count}h", data[:count*2])
        if not samples:
            return True
        sq = sum(s*s for s in samples) / count
        return int(sq ** 0.5) < threshold

def _bytes_rms(data):
    if len(data) < 2:
        return 0
    count = len(data) // 2
    samples = struct.unpack(f"<{count}h", data[:count*2])
    if not samples:
        return 0
    sq = sum(s*s for s in samples) / count
    return int(sq ** 0.5)

def transcribe_bytes(raw_bytes, cfg, rate=16000, channels=1):
    """Transcribe a chunk of raw PCM audio bytes by wrapping it in a WAV."""
    if _bytes_rms(raw_bytes) < 120:
        return ""   # silent chunk — skip (prevents phantom words)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    try:
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(channels); wf.setsampwidth(2)
            wf.setframerate(rate); wf.writeframes(raw_bytes)
        text = transcribe(tmp, cfg)
    finally:
        try: os.unlink(tmp)
        except Exception: pass
    if is_probable_hallucination(text):
        return ""
    return text

# ─── Transcription ────────────────────────────────────────────────────────────
def transcribe(audio_path, cfg):
    """
    Send audio to Groq for transcription. Retries once after a short pause
    on failure — VPN connections sometimes drop a single request and then
    work again moments later, so this can quietly recover from a blip
    without the person needing to notice or press Record again. It won't
    help with a VPN that's blocked outright the whole session, only
    genuinely transient hiccups.
    """
    client = Groq(api_key=cfg["groq_api_key"])
    kwargs = dict(file=("audio.wav", open(audio_path,"rb")),
                  model=cfg["model"], response_format="json", temperature=0)
    if cfg.get("language"):
        kwargs["language"] = cfg["language"]

    try:
        return client.audio.transcriptions.create(**kwargs).text.strip()
    except Exception as e:
        log_event("TRANSCRIBE", f"first attempt failed ({type(e).__name__}), retrying once...")
        time.sleep(1.5)
        # Re-open the file — it was consumed by the failed attempt
        kwargs["file"] = ("audio.wav", open(audio_path, "rb"))
        return client.audio.transcriptions.create(**kwargs).text.strip()

# ─── AI cleanup ───────────────────────────────────────────────────────────────
def _extract_after_rewrite_phrase(text):
    """
    If the AI wrote something like '...should be rewritten as X' instead of
    just returning X, pull X back out so real content isn't lost just
    because the AI narrated instead of obeying the no-commentary rule.
    """
    m = re.search(
        r'(?:should be (?:re)?written as|rewritten as|becomes)\s*[:\-]?\s*'
        r'["\']?([^"\'\n]+?)["\']?\.?\s*$',
        text, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None

# Phrases that specifically mean "the AI decided this was noise" — when one
# of these shows up inside otherwise-invalid commentary, the right move is
# to discard the result (which is what should have happened silently),
# not to show the explanation or fall back to possibly-meaningless raw text.
NOISE_INDICATOR_MARKERS = [
    "appears to be noise", "appears to be gibberish", "this looks like noise",
    "not actual words", "no clear words", "no discernible words",
    "there is no text", "no text to clean", "cannot determine",
    "unable to determine", "doesn't contain", "does not contain",
]

def ai_cleanup(text, cfg):
    if not cfg.get("ai_cleanup") or not text.strip():
        return text
    user_msg = f"{GENTLE_PROMPT}\n\nText to clean:\n{text}"
    try:
        model = cfg.get("groq_cleanup_model", "llama-3.3-70b-versatile")
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {cfg['groq_api_key']}","Content-Type":"application/json"},
            json={"model":model,"messages":[{"role":"user","content":user_msg}],
                  "max_tokens":2048,"temperature":0.0}, timeout=20)
        r.raise_for_status()
        out = r.json()["choices"][0]["message"]["content"].strip()
        out = out.strip('"').strip()

        if contains_ai_commentary(out):
            # The AI explained itself instead of just returning clean text.
            # Try to recover gracefully rather than showing the narration.
            extracted = _extract_after_rewrite_phrase(out)
            if extracted:
                log_event("CLEANUP", f"stripped AI commentary, kept: \"{extracted[:60]}\"")
                return extracted
            if any(m in out.lower() for m in NOISE_INDICATOR_MARKERS):
                log_event("CLEANUP", "AI flagged input as noise via commentary — discarding")
                return ""
            # Unrecognised commentary shape: fall back to the pre-cleanup
            # text rather than risk losing real content or showing narration.
            log_event("CLEANUP", f"unrecognised AI commentary, using raw instead: \"{out[:80]}\"")
            return text

        return out
    except Exception as e:
        print(f"AI cleanup failed: {e}")
        return text

# ─── Text to speech ───────────────────────────────────────────────────────────
class Speaker:
    """Wraps pyttsx3 for read-aloud. Uses SAPI5 on Windows, espeak on Linux."""
    def __init__(self, rate=165):
        self._rate = rate
        self._thread = None
        self._engine = None

    def speak(self, text):
        if not text.strip():
            return
        self.stop()
        self._thread = threading.Thread(target=self._run, args=(text,), daemon=True)
        self._thread.start()

    def _run(self, text):
        try:
            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", self._rate)
            self._engine.say(text)
            self._engine.runAndWait()
        except Exception as e:
            print(f"TTS failed: {e}")
        finally:
            self._engine = None

    def stop(self):
        try:
            if self._engine:
                self._engine.stop()
        except Exception:
            pass

# ─── Colours (warm, modern, high-contrast) ────────────────────────────────────
C_BG      = "#faf8f4"   # warm paper background
C_SURFACE = "#ffffff"   # cards / text area
C_TEXT    = "#1c1b22"   # deep ink
C_MUTED   = "#8a8794"   # secondary text
C_BORDER  = "#e7e3db"   # hairline borders

C_GREEN   = "#2e7d5b"   # record (deep sage)
C_GREEN_H = "#276b4e"   # hover
C_RED     = "#c2402f"   # stop / warnings (warm brick)
C_RED_H   = "#a83626"
C_BLUE    = "#3f5f8f"   # copy (slate)
C_BLUE_H  = "#35527d"
C_PURPLE  = "#6f5a9e"   # read aloud (soft plum)
C_PURPLE_H= "#5f4c89"
C_GREY    = "#7a7568"   # start over (warm stone)
C_GREY_H  = "#69645a"

# ─── DPI scaling ──────────────────────────────────────────────────────────────
# Set once in main(), right after the real display scaling is known (see
# main()). Every size in the app — fonts, button dimensions, paddings —
# is computed through px()/ui_font() so the whole UI scales consistently
# with whatever display scaling the actual computer is using, instead of
# using fixed pixel counts that only looked right on one specific screen.
DPI_SCALE = 1.0

def px(n):
    """Scale a design-pixel measurement by the current DPI factor."""
    return max(1, int(round(n * DPI_SCALE)))

def ui_font(size, bold=False):
    """Segoe UI on Windows, fallback elsewhere. Scales with DPI_SCALE."""
    fam = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"
    scaled = max(7, int(round(size * DPI_SCALE)))
    return (fam, scaled, "bold") if bold else (fam, scaled)

# ─── Rounded button (canvas-drawn, hover + pulse ring) ────────────────────────
class RoundButton(tk.Canvas):
    """A modern rounded square button drawn on a canvas.
    Draws an actual vector icon (not a text glyph — those render
    inconsistently across fonts/platforms and were the cause of the
    misaligned-looking buttons) plus a label underneath, both centered.
    Supports hover colour, resizing, and a pulsing ring while recording."""
    RADIUS_FRAC = 0.13   # corner radius as a fraction of the button size

    def __init__(self, parent, icon, label, colour, hover, command,
                 size=200, bg=None, show_label=True, bind_click=True):
        super().__init__(parent, width=size, height=size,
                         bg=bg or C_BG, highlightthickness=0, cursor="hand2")
        self._size = size
        self._icon = icon          # one of: record, stop, copy, read, clear, close
        self._colour = colour
        self._hover = hover
        self._command = command
        self._label_text = label
        self._show_label = show_label
        self._pulse_on = False
        self._pulse_step = 0

        self._draw(self._colour)
        self.bind("<Enter>", lambda e: self._draw(self._hover))
        self.bind("<Leave>", lambda e: self._draw(self._colour))
        if bind_click:
            # Some callers (like the draggable floating widget) need to
            # tell a click apart from the start of a drag themselves, so
            # they manage the click binding externally instead.
            self.bind("<Button-1>", lambda e: self._command())

    def _rounded_rect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0+r,y0, x1-r,y0, x1,y0, x1,y0+r, x1,y1-r, x1,y1,
               x1-r,y1, x0+r,y1, x0,y1, x0,y1-r, x0,y0+r, x0,y0]
        return self.create_polygon(pts, smooth=True, **kw)

    # ── Icon drawing (vector, so it always looks crisp and centered) ─────────
    def _draw_icon(self, cx, cy, r, white="white"):
        ic = self._icon
        if ic == "record":
            self.create_oval(cx-r, cy-r, cx+r, cy+r, fill=white, outline="")
        elif ic == "stop":
            k = r*0.86
            self.create_rectangle(cx-k, cy-k, cx+k, cy+k, fill=white, outline="")
        elif ic == "copy":
            w, h = r*1.15, r*1.4
            off = r*0.32
            lw = max(2, int(r*0.14))
            # back sheet
            self._rounded_rect(cx-w/2+off, cy-h/2-off, cx+w/2+off, cy+h/2-off,
                               r*0.22, fill="", outline=white, width=lw)
            # front sheet (filled background colour to "cut" the overlap)
            self._rounded_rect(cx-w/2-off, cy-h/2+off, cx+w/2-off, cy+h/2+off,
                               r*0.22, fill=self._current_fill, outline=white, width=lw)
        elif ic == "read":
            # Speaker body (polygon) + two sound-wave arcs
            bw, bh = r*0.55, r*0.9
            self.create_polygon(
                cx-r*0.95, cy-bh*0.32, cx-r*0.95+bw*0.5, cy-bh*0.32,
                cx-r*0.15, cy-bh, cx-r*0.15, cy+bh,
                cx-r*0.95+bw*0.5, cy+bh*0.32, cx-r*0.95, cy+bh*0.32,
                fill=white, outline="")
            lw = max(2, int(r*0.13))
            self.create_arc(cx-r*0.15, cy-r*0.55, cx+r*0.55, cy+r*0.55,
                            start=-55, extent=110, style="arc", outline=white, width=lw)
            self.create_arc(cx-r*0.15, cy-r*0.95, cx+r*0.95, cy+r*0.95,
                            start=-50, extent=100, style="arc", outline=white, width=lw)
        elif ic == "clear":
            # Circular arrow (refresh / start over)
            lw = max(2, int(r*0.16))
            self.create_arc(cx-r*0.85, cy-r*0.85, cx+r*0.85, cy+r*0.85,
                            start=40, extent=280, style="arc", outline=white, width=lw)
            # Arrowhead at the open end (~40 degrees)
            import math
            ang = math.radians(40)
            ax = cx + r*0.85*math.cos(ang)
            ay = cy - r*0.85*math.sin(ang)
            self.create_polygon(
                ax, ay-r*0.28, ax+r*0.30, ay+r*0.06, ax-r*0.14, ay+r*0.26,
                fill=white, outline="")
        elif ic == "close":
            # A simple X, used for the floating widget's close button
            lw = max(2, int(r*0.22))
            self.create_line(cx-r*0.6, cy-r*0.6, cx+r*0.6, cy+r*0.6,
                             fill=white, width=lw, capstyle="round")
            self.create_line(cx-r*0.6, cy+r*0.6, cx+r*0.6, cy-r*0.6,
                             fill=white, width=lw, capstyle="round")

    def _draw(self, fill):
        self._current_fill = fill
        self.delete("all")
        s = self._size
        pad = max(8, int(s*0.05))
        radius = max(10, s * self.RADIUS_FRAC)

        if self._pulse_on:
            # A clearly visible "breathing" ring — cycles smoothly out and
            # back in with both its distance from the button and its
            # thickness changing, so it reads as an obvious "recording is
            # happening" animation rather than the small 2px wobble this
            # used to be (easy to miss, especially on the floating widget).
            cycle = 10
            t = (self._pulse_step % cycle) / cycle
            phase = t*2 if t < 0.5 else (1-t)*2   # triangle wave: 0 -> 1 -> 0
            ring_pad = max(1, pad - 4 - int(phase * 12))
            ring_width = max(2, 2 + int(phase * 3))
            if self._show_label:
                self._rounded_rect(ring_pad, ring_pad, s-ring_pad, s-ring_pad,
                                   radius+8, fill="", outline=self._colour, width=ring_width)
            else:
                self.create_oval(ring_pad, ring_pad, s-ring_pad, s-ring_pad,
                                 fill="", outline=self._colour, width=ring_width)

        if not self._show_label:
            # Icon-only mode (used by the floating widget): a true circle
            # with the icon filling most of it, no label — small and clean.
            self.create_oval(pad, pad, s-pad, s-pad, fill=fill, outline="")
            self._draw_icon(s/2, s/2, s*0.28)
            return

        self._rounded_rect(pad, pad, s-pad, s-pad, radius, fill=fill, outline="")

        # Icon sits in a fixed zone in the upper part of the button. The
        # label is anchored to its TOP edge (not centered) at a fixed gap
        # below the icon's bottom edge — so however many lines the label
        # wraps into, it can only ever grow downward, never upward into
        # the icon. That guarantees they can't overlap, regardless of how
        # small the button gets or how the text happens to wrap.
        icon_r = s * 0.15
        icon_top_margin = s * 0.16
        icon_cy = icon_top_margin + icon_r
        icon_bottom = icon_cy + icon_r

        font_size = max(8, int(s * 0.078))
        gap = s * 0.10
        label_top = icon_bottom + gap
        label_bottom_limit = s - (s * 0.06)   # small margin above the button's bottom edge

        self._draw_icon(s/2, icon_cy, icon_r)

        fam = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"
        label_id = self.create_text(s/2, label_top, text=self._label_text,
                         font=(fam, font_size, "bold"), fill="white",
                         justify="center", width=int(s*0.86), anchor="n")

        # If the label still doesn't fit below the icon (very small button,
        # or an unusually long label), shrink the font just enough to fit
        # rather than letting it run past the button's edge.
        bbox = self.bbox(label_id)
        if bbox and bbox[3] > label_bottom_limit and font_size > 7:
            while bbox and bbox[3] > label_bottom_limit and font_size > 7:
                font_size -= 1
                self.itemconfig(label_id, font=(fam, font_size, "bold"))
                bbox = self.bbox(label_id)

        # Remembered so a group of buttons can be made uniform afterward —
        # each one fits its OWN label at this size, but different labels
        # ("COPY TEXT" vs "RECORD") can need different sizes to fit the
        # same button, which looks inconsistent side by side. The caller
        # can query this and force every button in the row to match the
        # smallest one that was actually needed.
        self._label_id = label_id
        self._fitted_font_size = font_size
        self._label_family = fam

    def set(self, label=None, icon=None, colour=None, hover=None):
        if label is not None:  self._label_text = label
        if icon is not None:   self._icon = icon
        if colour is not None: self._colour = colour
        if hover is not None:  self._hover = hover
        self._draw(self._colour)

    def resize(self, new_size):
        if new_size == self._size:
            return
        self._size = new_size
        self.config(width=new_size, height=new_size)
        self._draw(self._colour)

    def get_fitted_font_size(self):
        """The font size this button settled on to fit its own label —
        used to work out the smallest size needed across a row of
        buttons, so they can all be made to match."""
        return getattr(self, "_fitted_font_size", None)

    def force_label_font(self, font_size):
        """Override the label's font size directly (used to make a whole
        row of buttons share the same, smaller-if-necessary size instead
        of each fitting independently, which can look inconsistent when
        labels are different lengths)."""
        label_id = getattr(self, "_label_id", None)
        fam = getattr(self, "_label_family", None)
        if label_id is not None and fam:
            try:
                self.itemconfig(label_id, font=(fam, font_size, "bold"))
                self._fitted_font_size = font_size
            except Exception:
                pass

    def pulse_start(self):
        if self._pulse_on:
            return
        self._pulse_on = True
        self._pulse()

    def pulse_stop(self):
        self._pulse_on = False
        self._draw(self._colour)

    def _pulse(self):
        if not self._pulse_on:
            return
        self._pulse_step += 1
        self._draw(self._colour)
        self.after(120, self._pulse)


# ─── Spinner (animated wheel) ─────────────────────────────────────────────────
class Spinner(tk.Canvas):
    """A smooth spinning arc to show that work is happening."""
    def __init__(self, parent, size=44, width=5, colour=C_BLUE, bg=C_BG):
        super().__init__(parent, width=size, height=size,
                         bg=bg, highlightthickness=0)
        self._size = size
        self._width = width
        self._colour = colour
        self._angle = 0
        self._running = False
        self._arc = None

    def start(self, colour=None):
        if colour:
            self._colour = colour
        if self._running:
            return
        self._running = True
        self._spin()

    def stop(self):
        self._running = False
        self.delete("all")

    def _spin(self):
        if not self._running:
            return
        self.delete("all")
        pad = self._width + 2
        # A 300-degree arc that rotates, leaving a moving gap
        self.create_arc(pad, pad, self._size - pad, self._size - pad,
                        start=self._angle, extent=300,
                        style="arc", outline=self._colour, width=self._width)
        self._angle = (self._angle - 12) % 360
        self.after(33, self._spin)   # ~30fps


# ─── Settings window ──────────────────────────────────────────────────────────
class SettingsWindow(tk.Toplevel):
    def __init__(self, parent, cfg, on_save, first_run=False, app=None):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=C_BG)
        self.cfg = cfg
        self.on_save = on_save
        self.app = app   # lets the size slider live-preview the floating widget
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        # ── Size to fit the screen, same fix as the main window ──────────────
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = min(px(600), screen_w - px(60))
        win_h = min(px(640), screen_h - px(100))
        win_w = max(win_w, px(440))
        win_h = max(win_h, px(440))
        # Safety net: never let the floor above push the window past the
        # actual screen size, even if DPI scaling was detected incorrectly.
        win_w = min(win_w, screen_w - 20)
        win_h = min(win_h, screen_h - 20)
        x = (screen_w - win_w) // 2
        y = max(20, (screen_h - win_h) // 2)
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.minsize(px(340), px(360))

        outer = tk.Frame(self, bg=C_BG)
        outer.pack(fill="both", expand=True)

        # ── Button bar: fixed at the very bottom, ALWAYS visible ─────────────
        # Packed first with side="bottom" so it keeps its spot no matter how
        # tall the scrollable content above becomes.
        btns = tk.Frame(outer, bg=C_BG)
        btns.pack(side="bottom", fill="x", pady=12)
        tk.Button(btns, text="Cancel", font=ui_font(11),
                  command=self._cancel).pack(side="right", padx=10)
        tk.Button(btns, text="Save", font=ui_font(11, bold=True),
                  bg=C_GREEN, fg="white", padx=14, pady=4,
                  command=self._save).pack(side="right")
        tk.Button(btns, text="Remove this app", font=ui_font(9),
                  fg=C_RED, bd=0, command=self._remove_app).pack(side="left", padx=10)

        link_row = tk.Frame(outer, bg=C_BG)
        link_row.pack(side="bottom", fill="x")
        tk.Button(link_row, text="Open error report folder", font=ui_font(9),
                  fg=C_MUTED, bd=0, cursor="hand2",
                  command=self._open_logs).pack(side="left", padx=20, pady=(0,4))

        # ── Scrollable content area ───────────────────────────────────────────
        # Everything else lives inside a scrolling canvas, so if the screen
        # is small or more settings get added later, you can always scroll to
        # reach them — nothing can ever be silently cut off again.
        canvas = tk.Canvas(outer, bg=C_BG, highlightthickness=0)
        vscroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=C_BG)
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")

        def _on_body_configure(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        body.bind("<Configure>", _on_body_configure)

        def _on_canvas_configure(e):
            canvas.itemconfig(body_id, width=e.width)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(e):
            delta = -1 if e.delta > 0 else 1
            if sys.platform.startswith("win"):
                delta = -1 if e.delta > 0 else 1
            canvas.yview_scroll(delta, "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # ── Settings fields (inside `body`, so they scroll) ───────────────────
        pad = {"padx": 20, "pady": 6}

        if first_run:
            welcome = tk.Label(body,
                text="Welcome! Paste your Groq key below to get started.",
                font=ui_font(12, bold=True), bg=C_BG, fg=C_GREEN,
                wraplength=win_w-60, justify="left")
            welcome.pack(anchor="w", padx=20, pady=(16, 4))
        else:
            tk.Label(body, text="Settings", font=ui_font(15, bold=True),
                     bg=C_BG, fg=C_TEXT).pack(anchor="w", **pad)

        tk.Label(body, text="Groq API key  (from console.groq.com)",
                 font=ui_font(10), bg=C_BG, fg=C_MUTED).pack(anchor="w", padx=20)
        self.key_var = tk.StringVar(value=cfg.get("groq_api_key",""))
        self.key_entry = tk.Entry(body, textvariable=self.key_var, show="*",
                                  font=ui_font(11), width=36)
        self.key_entry.pack(anchor="w", padx=20, pady=4, fill="x")

        self.show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(body, text="Show key", variable=self.show_var,
                       command=self._toggle_key, bg=C_BG,
                       font=ui_font(9)).pack(anchor="w", padx=20)

        ttk.Separator(body, orient="horizontal").pack(fill="x", padx=20, pady=10)

        # ── Display size correction ───────────────────────────────────────────
        # Windows doesn't always report screen scaling correctly, especially
        # on some computers — this lets it be corrected by eye instead of
        # relying on automatic detection alone.
        tk.Label(body, text="Display Size", font=ui_font(12, bold=True),
                 bg=C_BG, fg=C_TEXT).pack(anchor="w", padx=20)
        tk.Label(body,
                 text="If everything looks too big or too small on this "
                      "computer, choose a different size below, then close "
                      "and reopen the app.",
                 font=ui_font(9), bg=C_BG, fg=C_MUTED, justify="left",
                 wraplength=win_w-60).pack(anchor="w", padx=20)

        scale_row = tk.Frame(body, bg=C_BG)
        scale_row.pack(anchor="w", padx=20, pady=(6, 4))
        self.scale_var = tk.StringVar(
            value=str(cfg.get("scale_override")) if cfg.get("scale_override") else "auto")
        self._scale_buttons = {}
        for label, val in [("Smaller", "0.8"), ("Normal", "1.0"),
                           ("Larger", "1.3"), ("Auto", "auto")]:
            b = tk.Button(scale_row, text=label, font=ui_font(9, bold=True),
                         bd=1, relief="solid", padx=px(8), pady=px(3),
                         command=lambda v=val: self._pick_scale(v))
            b.pack(side="left", padx=(0, 6))
            self._scale_buttons[val] = b
        self._refresh_scale_buttons()

        ttk.Separator(body, orient="horizontal").pack(fill="x", padx=20, pady=10)

        # ── Auto-close after inactivity ───────────────────────────────────────
        tk.Label(body, text="Auto-Close", font=ui_font(12, bold=True),
                 bg=C_BG, fg=C_TEXT).pack(anchor="w", padx=20)
        tk.Label(body,
                 text="Close the app automatically after it hasn't been "
                      "used for a while, so it doesn't keep running in "
                      "the background unnecessarily.",
                 font=ui_font(9), bg=C_BG, fg=C_MUTED, justify="left",
                 wraplength=win_w-60).pack(anchor="w", padx=20)

        self.autoclose_var = tk.BooleanVar(value=cfg.get("auto_close_enabled", False))
        tk.Checkbutton(body, text="Close automatically when unused",
                       variable=self.autoclose_var, bg=C_BG,
                       font=ui_font(11)).pack(anchor="w", padx=30, pady=(6, 4))

        close_time_row = tk.Frame(body, bg=C_BG)
        close_time_row.pack(anchor="w", padx=30, pady=(0, 10))
        tk.Label(close_time_row, text="After:", font=ui_font(9),
                bg=C_BG, fg=C_MUTED).pack(side="left", padx=(0, 6))
        self.autoclose_minutes_var = tk.StringVar(
            value=str(int(cfg.get("auto_close_minutes", 3))))
        self._autoclose_buttons = {}
        for n in [2, 3, 4, 5]:
            b = tk.Button(close_time_row, text=f"{n} min", font=ui_font(9, bold=True),
                         bd=1, relief="solid", padx=px(8), pady=px(3),
                         command=lambda v=n: self._pick_autoclose_minutes(v))
            b.pack(side="left", padx=(0, 6))
            self._autoclose_buttons[str(n)] = b
        self._refresh_autoclose_buttons()

        ttk.Separator(body, orient="horizontal").pack(fill="x", padx=20, pady=10)

        # ── Auto-launch on startup ────────────────────────────────────────────
        tk.Label(body, text="Start With Windows", font=ui_font(12, bold=True),
                 bg=C_BG, fg=C_TEXT).pack(anchor="w", padx=20)
        tk.Label(body,
                 text="Open Voice to Text automatically whenever this "
                      "computer turns on, so it's always ready without "
                      "needing to find and click the icon.",
                 font=ui_font(9), bg=C_BG, fg=C_MUTED, justify="left",
                 wraplength=win_w-60).pack(anchor="w", padx=20)

        self.autolaunch_var = tk.BooleanVar(
            value=cfg.get("auto_launch", False) or get_autostart_state())
        tk.Checkbutton(body, text="Start automatically when the computer turns on",
                       variable=self.autolaunch_var, bg=C_BG,
                       font=ui_font(11), command=self._toggle_autolaunch
                       ).pack(anchor="w", padx=30, pady=(6, 10))

        ttk.Separator(body, orient="horizontal").pack(fill="x", padx=20, pady=10)

        # ── Microphone test ────────────────────────────────────────────────
        tk.Label(body, text="Microphone", font=ui_font(12, bold=True),
                 bg=C_BG, fg=C_TEXT).pack(anchor="w", padx=20)
        tk.Label(body,
                 text="If Record keeps saying it can't hear anything, test\n"
                      "the microphone here first.",
                 font=ui_font(9), bg=C_BG, fg=C_MUTED,
                 justify="left").pack(anchor="w", padx=20)

        mic_row = tk.Frame(body, bg=C_BG)
        mic_row.pack(anchor="w", padx=20, pady=6, fill="x")
        self.mic_test_btn = tk.Button(mic_row, text="Test Microphone",
                                      font=ui_font(10, bold=True),
                                      bg=C_BLUE, fg="white", padx=10, pady=4,
                                      command=self._test_microphone)
        self.mic_test_btn.pack(side="left")

        # Live level meter (a thin canvas bar)
        self.mic_meter = tk.Canvas(mic_row, width=180, height=18, bg="#eeece5",
                                   highlightthickness=1, highlightbackground=C_BORDER)
        self.mic_meter.pack(side="left", padx=10)
        self._mic_meter_fill = None

        self.mic_status_lbl = tk.Label(body, text="", font=ui_font(9),
                                       bg=C_BG, fg=C_MUTED, justify="left",
                                       wraplength=win_w-60)
        self.mic_status_lbl.pack(anchor="w", padx=20)

        if sys.platform.startswith("win"):
            tk.Button(body, text="Open Windows Microphone Settings",
                     font=ui_font(9), fg=C_BLUE, bd=0, cursor="hand2",
                     command=self._open_mic_privacy).pack(anchor="w", padx=20, pady=(2,0))

        ttk.Separator(body, orient="horizontal").pack(fill="x", padx=20, pady=10)

        # ── Connection test ────────────────────────────────────────────────
        tk.Label(body, text="Internet Connection", font=ui_font(12, bold=True),
                 bg=C_BG, fg=C_TEXT).pack(anchor="w", padx=20)
        tk.Label(body,
                 text="If recordings keep failing, a VPN can sometimes block "
                      "the connection. Test it here.",
                 font=ui_font(9), bg=C_BG, fg=C_MUTED,
                 justify="left", wraplength=win_w-60).pack(anchor="w", padx=20)

        self.conn_test_btn = tk.Button(body, text="Test Connection",
                                       font=ui_font(10, bold=True),
                                       bg=C_BLUE, fg="white", padx=10, pady=4,
                                       command=self._test_connection)
        self.conn_test_btn.pack(anchor="w", padx=20, pady=6)
        self.conn_status_lbl = tk.Label(body, text="", font=ui_font(9),
                                        bg=C_BG, fg=C_MUTED, justify="left",
                                        wraplength=win_w-60)
        self.conn_status_lbl.pack(anchor="w", padx=20)

        ttk.Separator(body, orient="horizontal").pack(fill="x", padx=20, pady=10)

        tk.Label(body, text="Which buttons should show?",
                 font=ui_font(12, bold=True), bg=C_BG, fg=C_TEXT).pack(anchor="w", padx=20)
        tk.Label(body, text="The Record button always stays.",
                 font=ui_font(9), bg=C_BG, fg=C_MUTED).pack(anchor="w", padx=20)

        self.copy_var  = tk.BooleanVar(value=cfg.get("show_copy", True))
        self.read_var  = tk.BooleanVar(value=cfg.get("show_read", True))
        self.clear_var = tk.BooleanVar(value=cfg.get("show_clear", True))
        for text, var in [("Copy Text", self.copy_var),
                          ("Read Aloud", self.read_var),
                          ("Start Over", self.clear_var)]:
            tk.Checkbutton(body, text=text, variable=var, bg=C_BG,
                           font=ui_font(11)).pack(anchor="w", padx=30, pady=2)

        ttk.Separator(body, orient="horizontal").pack(fill="x", padx=20, pady=10)

        # ── Floating record button ────────────────────────────────────────────
        tk.Label(body, text="Floating Record Button", font=ui_font(12, bold=True),
                 bg=C_BG, fg=C_TEXT).pack(anchor="w", padx=20)
        tk.Label(body,
                 text="A small record button that stays on top of every other "
                      "window, in the bottom-right of the screen. Click it "
                      "to record without switching back to this app.",
                 font=ui_font(9), bg=C_BG, fg=C_MUTED, justify="left",
                 wraplength=win_w-60).pack(anchor="w", padx=20)

        self.floating_var = tk.BooleanVar(value=cfg.get("show_floating", True))
        tk.Checkbutton(body, text="Show the floating record button",
                       variable=self.floating_var, bg=C_BG,
                       font=ui_font(11)).pack(anchor="w", padx=30, pady=(4, 2))
        tk.Label(body, text="You can also close it any time with its own X button, "
                            "or drag it anywhere by pressing and holding. To "
                            "change its size, use the slider under the buttons "
                            "on the main screen.",
                 font=ui_font(9), bg=C_BG, fg=C_MUTED, justify="left",
                 wraplength=win_w-90).pack(anchor="w", padx=30, pady=(0, 8))

        self.floating_text_var = tk.BooleanVar(value=cfg.get("floating_show_text", True))
        tk.Checkbutton(body, text="Show status text next to the popup buttons",
                       variable=self.floating_text_var, bg=C_BG,
                       font=ui_font(11)).pack(anchor="w", padx=30, pady=(0, 2))
        tk.Label(body, text="Turn this off to keep just the buttons, with no "
                            "text — a smaller, simpler popup.",
                 font=ui_font(9), bg=C_BG, fg=C_MUTED, justify="left",
                 wraplength=win_w-90).pack(anchor="w", padx=30, pady=(0, 10))

        ttk.Separator(body, orient="horizontal").pack(fill="x", padx=20, pady=10)

        self.autotype_var = tk.BooleanVar(value=cfg.get("auto_type", True))
        tk.Checkbutton(body, text="Type the words where I click (after recording)",
                       variable=self.autotype_var, bg=C_BG,
                       font=ui_font(11)).pack(anchor="w", padx=20, pady=2)
        tk.Label(body, text="After it finishes, you get a few seconds to click\nwhere the words should go, then they type themselves.",
                 font=ui_font(9), bg=C_BG, fg=C_MUTED,
                 justify="left").pack(anchor="w", padx=30, pady=(0, 8))

        delay_row = tk.Frame(body, bg=C_BG)
        delay_row.pack(anchor="w", padx=30, pady=(0, 16), fill="x")
        tk.Label(delay_row, text="Wait time:", font=ui_font(10),
                bg=C_BG, fg=C_TEXT).pack(side="left")
        self.delay_var = tk.IntVar(value=int(cfg.get("type_delay", 5)))
        self.delay_value_lbl = tk.Label(delay_row, text=f"{self.delay_var.get()} sec",
                                        font=ui_font(10, bold=True), bg=C_BG, fg=C_BLUE)
        self.delay_value_lbl.pack(side="right")
        delay_scale = tk.Scale(body, from_=3, to=15, orient="horizontal",
                               variable=self.delay_var, bg=C_BG, fg=C_TEXT,
                               troughcolor=C_BORDER, highlightthickness=0,
                               showvalue=False, length=win_w-70,
                               sliderlength=px(34), width=px(22), sliderrelief="raised",
                               activebackground=C_BLUE,
                               command=lambda v: self.delay_value_lbl.config(
                                   text=f"{int(float(v))} sec"))
        delay_scale.pack(anchor="w", padx=30, pady=(0, 16))

        ttk.Separator(body, orient="horizontal").pack(fill="x", padx=20, pady=10)

        # ── Diagnostic report: copyable activity + error log ─────────────────
        tk.Label(body, text="Activity & Error Report", font=ui_font(12, bold=True),
                 bg=C_BG, fg=C_TEXT).pack(anchor="w", padx=20)
        tk.Label(body,
                 text="Shows what the app has been doing — button clicks, "
                      "recordings, and any errors. If something looks wrong, "
                      "copy this and send it over.",
                 font=ui_font(9), bg=C_BG, fg=C_MUTED, justify="left",
                 wraplength=win_w-60).pack(anchor="w", padx=20)

        report_frame = tk.Frame(body, bg=C_BORDER)
        report_frame.pack(fill="both", padx=20, pady=6)
        _report_fam = "Consolas" if sys.platform.startswith("win") else "Courier"
        self.report_text = tk.Text(report_frame, height=8,
                                   font=(_report_fam, max(7, int(round(9*DPI_SCALE)))),
                                   wrap="word", bg="#fbfaf7", fg=C_TEXT, bd=0,
                                   padx=8, pady=8)
        report_scroll = ttk.Scrollbar(report_frame, orient="vertical",
                                      command=self.report_text.yview)
        self.report_text.configure(yscrollcommand=report_scroll.set)
        self.report_text.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        report_scroll.pack(side="right", fill="y")

        report_btn_row = tk.Frame(body, bg=C_BG)
        report_btn_row.pack(anchor="w", padx=20, pady=(0, 6))
        tk.Button(report_btn_row, text="Refresh", font=ui_font(9),
                 command=self._refresh_report).pack(side="left", padx=(0, 8))
        tk.Button(report_btn_row, text="Copy Report to Clipboard",
                 font=ui_font(9, bold=True), bg=C_BLUE, fg="white",
                 command=self._copy_report).pack(side="left")
        self.report_copied_lbl = tk.Label(report_btn_row, text="", font=ui_font(9),
                                          bg=C_BG, fg=C_GREEN)
        self.report_copied_lbl.pack(side="left", padx=10)

        self._refresh_report()

        if first_run:
            # Big friendly first-run save button right in the flow too,
            # in case the user doesn't scroll down to the bottom bar.
            tk.Button(body, text="Save and start using the app",
                     font=ui_font(12, bold=True), bg=C_GREEN, fg="white",
                     padx=16, pady=8, command=self._save).pack(padx=20, pady=(0, 20))

    def _refresh_report(self):
        try:
            report = build_full_report()
        except Exception as e:
            report = f"(couldn't build report: {e})"
        self.report_text.delete("1.0", "end")
        self.report_text.insert("1.0", report)

    def _copy_report(self):
        try:
            report = self.report_text.get("1.0", "end").strip()
            self.clipboard_clear()
            self.clipboard_append(report)
            self.report_copied_lbl.config(text="Copied!")
            self.after(2000, lambda: self.report_copied_lbl.config(text=""))
        except Exception as e:
            log_error("copy_report", e)

    def _toggle_key(self):
        self.key_entry.config(show="" if self.show_var.get() else "*")

    def _pick_scale(self, val):
        # Saves immediately rather than waiting for the main Save button —
        # this needs a restart regardless, so it's more foolproof to just
        # apply it right away than to rely on remembering a second step.
        self.scale_var.set(val)
        self.cfg["scale_override"] = None if val == "auto" else float(val)
        save_config(self.cfg)
        self._refresh_scale_buttons()
        log_event("SETTINGS", f"display size set to {val}")

    def _refresh_scale_buttons(self):
        current = self.scale_var.get()
        for val, btn in self._scale_buttons.items():
            if val == current:
                btn.config(bg=C_BLUE, fg="white", activebackground=C_BLUE_H,
                          highlightbackground=C_BLUE)
            else:
                btn.config(bg=C_SURFACE, fg=C_TEXT, activebackground=C_BORDER,
                          highlightbackground=C_BORDER)

    def _pick_autoclose_minutes(self, n):
        self.autoclose_minutes_var.set(str(n))
        self._refresh_autoclose_buttons()

    def _refresh_autoclose_buttons(self):
        current = self.autoclose_minutes_var.get()
        for val, btn in self._autoclose_buttons.items():
            if val == current:
                btn.config(bg=C_BLUE, fg="white", activebackground=C_BLUE_H,
                          highlightbackground=C_BLUE)
            else:
                btn.config(bg=C_SURFACE, fg=C_TEXT, activebackground=C_BORDER,
                          highlightbackground=C_BORDER)

    def _toggle_autolaunch(self):
        enabled = self.autolaunch_var.get()
        ok = set_autostart(enabled)
        if not ok:
            # Couldn't write to the registry for some reason — reflect
            # reality in the checkbox rather than claim it worked.
            self.autolaunch_var.set(get_autostart_state())

    def _open_mic_privacy(self):
        try:
            os.startfile("ms-settings:privacy-microphone")
        except Exception as e:
            log_error("open_mic_privacy", e)

    def _test_connection(self):
        """Send a tiny request to Groq to check whether something (usually a
        VPN) is blocking it, without needing to record anything first."""
        log_event("BUTTON", "Test Connection clicked")
        self.conn_test_btn.config(state="disabled", text="Testing...")
        self.conn_status_lbl.config(text="Checking...", fg=C_BLUE)

        def work():
            key = self.cfg.get("groq_api_key", "")
            if not key:
                result = ("no_key", "Add your Groq key above first, then test.")
            else:
                try:
                    r = requests.get("https://api.groq.com/openai/v1/models",
                                     headers={"Authorization": f"Bearer {key}"},
                                     timeout=10)
                    if r.status_code == 200:
                        result = ("ok", "Connected! The app can reach Groq normally.")
                    elif r.status_code == 403:
                        result = ("blocked",
                                  "Blocked (error 403). This is almost always a "
                                  "VPN or network filter. Try turning off any "
                                  "VPN and test again.")
                    elif r.status_code == 401:
                        result = ("badkey", "The Groq key was rejected — check it's correct.")
                    else:
                        result = ("other", f"Got an unexpected response (code {r.status_code}).")
                except requests.exceptions.Timeout:
                    result = ("timeout", "No response — check the internet connection.")
                except Exception as e:
                    result = ("error", f"Couldn't connect: {e}")
            log_event("CONN_TEST", f"result: {result[0]}")
            self.after(0, lambda: self._show_conn_result(result))

        threading.Thread(target=work, daemon=True).start()

    def _show_conn_result(self, result):
        kind, msg = result
        self.conn_test_btn.config(state="normal", text="Test Connection")
        colour = C_GREEN if kind == "ok" else (C_RED if kind in ("blocked","error") else C_MUTED)
        self.conn_status_lbl.config(text=msg, fg=colour)

    def _test_microphone(self):
        """Record 3 seconds and show a live level meter, so it's obvious
        whether audio is reaching the app at all — the key diagnostic for
        Windows microphone-permission problems."""
        log_event("BUTTON", "Test Microphone clicked")
        self.mic_test_btn.config(state="disabled", text="Listening...")
        self.mic_status_lbl.config(text="Make some noise or talk for a moment...", fg=C_BLUE)
        rec = AudioRecorder(self.cfg.get("sample_rate", 16000), self.cfg.get("channels", 1))
        rec.start()

        if rec.open_error:
            log_event("MIC_TEST", f"failed to open — {rec.open_error}")
            self.mic_test_btn.config(state="normal", text="Test Microphone")
            self.mic_status_lbl.config(
                text="Couldn't open the microphone at all. This usually means "
                     "Windows is blocking it for this app. Click the blue link "
                     "above to open Microphone Settings, then make sure "
                     "'Microphone access' and 'Let desktop apps access your "
                     "microphone' are both turned ON.",
                fg=C_RED)
            return

        self._mic_test_start = time.time()
        self._mic_test_recorder = rec
        self._update_mic_meter()

    def _update_mic_meter(self):
        rec = getattr(self, "_mic_test_recorder", None)
        if not rec:
            return
        elapsed = time.time() - self._mic_test_start
        # Read the most recent short window of audio for a responsive meter
        n = rec.frame_count()
        recent = rec.slice_bytes(max(0, n-4), n)
        level = _bytes_rms(recent) if recent else 0
        # Scale roughly 0..4000 to 0..1
        frac = max(0.0, min(1.0, level / 4000.0))
        self.mic_meter.delete("all")
        w = int(176 * frac)
        colour = C_GREEN if frac > 0.05 else C_BORDER
        if w > 0:
            self.mic_meter.create_rectangle(2, 2, 2+w, 16, fill=colour, outline="")

        if elapsed >= 3.0:
            rec.stop()
            kind = rec.silence_kind()
            log_event("MIC_TEST", f"result: {kind}")
            self.mic_test_btn.config(state="normal", text="Test Microphone")
            if kind == "blocked":
                self.mic_status_lbl.config(
                    text="No sound reached the app at all, even though the "
                         "microphone opened. This is almost always a Windows "
                         "permission setting. Click the blue link above, then "
                         "turn on microphone access for desktop apps.",
                    fg=C_RED)
            elif kind == "quiet":
                self.mic_status_lbl.config(
                    text="Only very quiet sound was picked up. Try moving "
                         "closer to the microphone, or check the right "
                         "microphone is selected as default in Windows sound "
                         "settings.",
                    fg=C_MUTED)
            else:
                self.mic_status_lbl.config(
                    text="The microphone is working — sound is reaching the app.",
                    fg=C_GREEN)
            self._mic_test_recorder = None
            return

        self.after(100, self._update_mic_meter)

    def _open_logs(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(CONFIG_DIR))
            else:
                subprocess.Popen(["xdg-open", str(CONFIG_DIR)])
        except Exception as e:
            log_error("open_logs", e)

    def _remove_app(self):
        # Friendly two-step confirmation so it can't happen by accident
        ok = messagebox.askyesno(
            "Remove this app?",
            "This will remove Voice to Text and its settings from this "
            "computer.\n\nThe desktop icon will be deleted too.\n\n"
            "Are you sure you want to remove it?",
            icon="warning", parent=self)
        if not ok:
            return
        try:
            _uninstall_app()
        except Exception as e:
            messagebox.showerror("Problem", f"Could not fully remove: {e}", parent=self)
            return
        messagebox.showinfo(
            "Removed",
            "Voice to Text has been removed.\n\nThe app will now close.",
            parent=self)
        # Close the whole program
        self.master.destroy()
        os._exit(0)

    def _cancel(self):
        self.destroy()

    def _save(self):
        self.cfg["groq_api_key"] = self.key_var.get().strip()
        self.cfg["show_copy"]  = self.copy_var.get()
        self.cfg["show_read"]  = self.read_var.get()
        self.cfg["show_clear"] = self.clear_var.get()
        self.cfg["auto_type"]  = self.autotype_var.get()
        self.cfg["type_delay"] = int(self.delay_var.get())
        self.cfg["show_floating"] = self.floating_var.get()
        self.cfg["floating_show_text"] = self.floating_text_var.get()
        self.cfg["floating_choice_made"] = True
        self.cfg["auto_close_enabled"] = self.autoclose_var.get()
        self.cfg["auto_close_minutes"] = int(self.autoclose_minutes_var.get())
        self.cfg["auto_launch"] = self.autolaunch_var.get()
        save_config(self.cfg)
        self.on_save()
        self.destroy()

# ─── Main window ──────────────────────────────────────────────────────────────
def classify_api_error(exc):
    """
    Turn a raw exception from Groq into a plain-language, actionable message.
    A 403 'access denied' is Groq blocking the request before it even reaches
    the account — almost always a VPN or network filter, NOT a bad key or
    silence. A 401 means the key itself is wrong. Anything else falls back
    to a generic network message.
    """
    s = str(exc).lower()
    if "403" in s or "access denied" in s or "permissiondenied" in s.lower():
        return ("It looks like a VPN or network is blocking the connection. "
                "If a VPN is running, try turning it off, or ask about "
                "allowing this app through it.")
    if "401" in s or "invalid api key" in s or "authentication" in s:
        return "There may be a problem with the Groq key (check the gear)."
    if "timeout" in s or "connection" in s or "network" in s:
        return "Couldn't reach the internet — check the connection and try again."
    return None


# ─── Floating always-on-top record widget ────────────────────────────────────
class FloatingWidget(tk.Toplevel):
    """
    A small always-on-top window with a record button, a close button, and
    a status/countdown label. Sits above every other window so recording
    never requires switching back to the main app window.

    Both buttons are real RoundButtons (not tiny text), and the whole
    widget can be dragged by pressing and holding anywhere on it —
    including on the buttons themselves — and moving the mouse. A short
    movement threshold distinguishes "held and dragged" from "just
    clicked", so dragging never accidentally triggers Record or Close.

    Size can change live while the Settings slider is being dragged, via
    apply_size(), without needing to close and reopen the widget.
    """
    DRAG_THRESHOLD = 4    # pixels of movement before a press counts as a drag
    MIN_SIZE = 50
    MAX_SIZE = 200

    def _clamp_to_screen(self, x, y, width, height):
        """Keep the widget fully within the visible screen — used at
        startup, after resizing, and while dragging, so it can never end
        up partly hidden off the edge."""
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, min(x, screen_w - width))
        y = max(0, min(y, screen_h - height))
        return x, y

    def _compute_layout(self, logical_size):
        """
        Work out every dimension for a given logical size (50-200, DPI-
        independent). The slider value is a size *choice*; px() converts it
        to actual pixels for this specific display, so the same slider
        setting looks the same physical size on any screen.

        Layout left to right: [countdown] [record] [close] [status text].
        The countdown zone is always reserved (blank until a countdown is
        actually running); the status text is only included if turned on
        in Settings — with it off, the popup is just the two buttons.
        """
        logical_size = max(self.MIN_SIZE, min(self.MAX_SIZE, logical_size))
        size = px(logical_size)
        # The close button keeps a comfortable minimum regardless of how
        # small the record button gets — letting it shrink all the way
        # down with a tiny record button made it too small to see or
        # click reliably.
        close_size = max(px(36), int(size * 0.55))
        countdown_size = max(px(30), int(size * 0.5))
        pad = px(14)
        gap = px(10)
        show_text = self.app.cfg.get("floating_show_text", True)
        text_w = max(px(110), int(size * 1.7)) if show_text else 0
        # font_size stays a LOGICAL point size; ui_font() applies DPI
        # scaling itself, so scaling it again here would make it too large.
        font_size = max(9, round(logical_size * 0.1))
        width = (pad*2 + countdown_size + gap + size + gap + close_size
                 + (gap + text_w if show_text else 0))
        height = pad*2 + max(size, close_size, countdown_size)
        return dict(logical_size=logical_size, size=size, close_size=close_size,
                   countdown_size=countdown_size, pad=pad, gap=gap, text_w=text_w,
                   font_size=font_size, width=width, height=height, show_text=show_text)

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.overrideredirect(True)        # no title bar / borders
        self.attributes("-topmost", True)  # always above other windows
        try:
            self.attributes("-alpha", 0.97)  # not universally supported; ignore if not
        except Exception:
            pass
        self.configure(bg=C_BG)

        L = self._compute_layout(int(app.cfg.get("floating_size", 110)))
        size, close_size = L["size"], L["close_size"]
        countdown_size, pad, gap, text_w = L["countdown_size"], L["pad"], L["gap"], L["text_w"]
        width, height, show_text = L["width"], L["height"], L["show_text"]

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        margin = 20
        x = app.cfg.get("floating_x")
        y = app.cfg.get("floating_y")
        if x is None or y is None:
            x = screen_w - width - margin
            y = screen_h - height - margin - 50  # sit above the taskbar
        # A saved position combined with a size that's since grown (e.g.
        # the size slider was used, or the app restarted with a larger
        # saved size) could otherwise sit partly off-screen — keep it
        # fully visible regardless.
        x, y = self._clamp_to_screen(x, y, width, height)
        self.geometry(f"{width}x{height}+{x}+{y}")

        self.card = tk.Frame(self, bg=C_SURFACE, highlightbackground=C_BORDER,
                        highlightthickness=1)
        self.card.pack(fill="both", expand=True)
        card = self.card

        row_h = max(size, close_size, countdown_size)

        # Countdown zone — always reserved on the left of the record
        # button, blank until a countdown is actually running (see
        # show_countdown()/clear_countdown()).
        self._countdown_size = countdown_size
        self.countdown_canvas = tk.Canvas(card, width=countdown_size, height=countdown_size,
                                          bg=C_SURFACE, highlightthickness=0)
        self.countdown_canvas.place(x=pad, y=pad + (row_h-countdown_size)//2)

        self.record_btn = RoundButton(card, "record", "", C_GREEN, C_GREEN_H,
                                      self._on_record, size=size, bg=C_SURFACE,
                                      show_label=False, bind_click=False)
        self.record_btn.place(x=pad + countdown_size + gap, y=pad + (row_h-size)//2)

        self.close_btn = RoundButton(card, "close", "", C_GREY, C_RED,
                                     self._close, size=close_size, bg=C_SURFACE,
                                     show_label=False, bind_click=False)
        self.close_btn.place(x=pad + countdown_size + gap + size + gap,
                             y=pad + (row_h-close_size)//2)

        self._text_w = text_w
        self._label_height = height
        self._font_size_logical = L["font_size"]
        self.status_lbl = None
        if show_text:
            self.status_var = tk.StringVar(value="Ready")
            self.status_lbl = tk.Label(card, textvariable=self.status_var,
                                       font=ui_font(L["font_size"], bold=True), bg=C_SURFACE,
                                       fg=C_GREEN, wraplength=text_w-10, justify="left")
            self.status_lbl.place(x=pad + countdown_size + gap + size + gap + close_size + gap,
                                  y=0, width=text_w, height=height)
        else:
            self.status_var = tk.StringVar(value="Ready")  # kept for compatibility, just unused visually

        # ── Drag handling (screen-coordinate based, with click/drag split) ────
        self._press_root = None
        self._press_winpos = None
        self._dragged = False
        self._press_widget = None

        drag_targets = [card, self.record_btn, self.close_btn]
        if self.status_lbl is not None:
            drag_targets.append(self.status_lbl)
        for w in drag_targets:
            w.bind("<ButtonPress-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_motion)
            w.bind("<ButtonRelease-1>", self._on_release)

    # ── Countdown indicator (left of the record button) ──────────────────────
    def show_countdown(self, n):
        """Show a number in the countdown circle — used during the pause
        before auto-typing, so there's a clear visual cue right on the
        popup for how long is left to click into the target window."""
        try:
            c = self.countdown_canvas
            s = self._countdown_size
            c.delete("all")
            c.create_oval(2, 2, s-2, s-2, fill=C_BLUE, outline="")
            fam = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"
            font_sz = max(9, int(s * 0.42))
            c.create_text(s/2, s/2, text=str(n), fill="white",
                          font=(fam, font_sz, "bold"))
        except Exception:
            pass

    def clear_countdown(self):
        try:
            self.countdown_canvas.delete("all")
        except Exception:
            pass

    # ── Drag / click handling ─────────────────────────────────────────────────
    def _on_press(self, event):
        self._press_root = (event.x_root, event.y_root)
        self._press_winpos = (self.winfo_x(), self.winfo_y())
        self._dragged = False
        self._press_widget = event.widget

    def _on_motion(self, event):
        if self._press_root is None:
            return
        dx = event.x_root - self._press_root[0]
        dy = event.y_root - self._press_root[1]
        if not self._dragged and (abs(dx) > self.DRAG_THRESHOLD or abs(dy) > self.DRAG_THRESHOLD):
            self._dragged = True
        if self._dragged:
            new_x = self._press_winpos[0] + dx
            new_y = self._press_winpos[1] + dy
            w = self.winfo_width()
            h = self.winfo_height()
            new_x, new_y = self._clamp_to_screen(new_x, new_y, w, h)
            self.geometry(f"+{new_x}+{new_y}")

    def _on_release(self, event):
        if not self._dragged:
            # A genuine click (didn't move past the threshold) — act on
            # whichever widget the press started on.
            if self._press_widget is self.record_btn:
                self._on_record()
            elif self._press_widget is self.close_btn:
                self._close()
        else:
            # Drag finished — remember the new position for next launch.
            self.app.cfg["floating_x"] = self.winfo_x()
            self.app.cfg["floating_y"] = self.winfo_y()
            save_config(self.app.cfg)
        self._press_root = None
        self._dragged = False
        self._press_widget = None

    def _on_record(self):
        if not self.app._recording:
            # Starting a fresh recording from the floating widget: clear
            # the text box first so each new bit of speech starts clean
            # rather than piling onto whatever was said before — otherwise
            # Copy Text would grab everything said since the widget opened
            # instead of just the latest part.
            self.app._clear_text_for_next_recording()
        self.app._on_record()

    def _close(self):
        log_event("BUTTON", "Floating widget closed")
        self.app.cfg["show_floating"] = False
        self.app.cfg["floating_choice_made"] = True
        save_config(self.app.cfg)
        self.app._close_floating_widget()
        self.app._refresh_floating_toggle_btn()

    # ── Mirrors of the main window's status updates ──────────────────────────
    def set_status(self, msg, colour):
        # Remember the full, untruncated message so that if the widget
        # grows again later, more of the real text can be shown rather
        # than re-truncating an already-shortened string.
        self._last_full_status = msg
        if self.status_lbl is None:
            return   # text turned off in Settings — nothing to update
        self.status_var.set(self._fit_text(msg))
        self.status_lbl.config(fg=colour)

    def _fit_text(self, msg):
        """
        Shorten msg so it fits within the current text box — the same
        'never overflow' rule used for the button labels, adapted for a
        Label (which doesn't clip overflowing text on its own the way a
        Canvas item can be made to).
        """
        font_px = max(7, int(round(getattr(self, "_font_size_logical", 11) * DPI_SCALE)))
        avg_char_w = max(1, font_px * 0.55)
        text_w = getattr(self, "_text_w", 140)
        height = getattr(self, "_label_height", 60)
        chars_per_line = max(6, int(text_w / avg_char_w))
        line_h = max(1, font_px * 1.3)
        max_lines = max(1, int(height / line_h))
        max_chars = chars_per_line * max_lines
        if len(msg) <= max_chars:
            return msg
        return msg[:max(3, max_chars - 3)] + "..."

    def set_recording_state(self, recording):
        if recording:
            self.record_btn.set(icon="stop", colour=C_RED, hover=C_RED_H)
            self.record_btn.pulse_start()
        else:
            self.record_btn.set(icon="record", colour=C_GREEN, hover=C_GREEN_H)
            self.record_btn.pulse_stop()

    def apply_size(self, logical_size):
        """
        Resize everything in place — buttons, close button, countdown
        zone, status text (if shown), the window itself — without
        destroying and rebuilding the widget. Used for live preview while
        the size slider is dragged, and cheap enough to call on every
        slider tick.

        The bottom-right corner stays anchored while resizing (the widget
        grows toward the top-left), matching where it's docked by default,
        so it can't drift off-screen as it gets bigger.
        """
        L = self._compute_layout(logical_size)
        size, close_size = L["size"], L["close_size"]
        countdown_size, pad, gap, text_w = L["countdown_size"], L["pad"], L["gap"], L["text_w"]
        width, height, show_text = L["width"], L["height"], L["show_text"]

        old_x, old_y = self.winfo_x(), self.winfo_y()
        old_w = self.winfo_width() or width
        old_h = self.winfo_height() or height
        new_x = old_x + (old_w - width)
        new_y = old_y + (old_h - height)
        # Growing while anchored to the bottom-right corner keeps it on
        # screen in the normal case, but this is a final safety net for
        # anything unusual (multi-monitor setups, a widget dragged right
        # up against an edge, etc.) — never let it end up partly hidden.
        new_x, new_y = self._clamp_to_screen(new_x, new_y, width, height)
        self.geometry(f"{width}x{height}+{new_x}+{new_y}")

        row_h = max(size, close_size, countdown_size)
        self._countdown_size = countdown_size
        self.countdown_canvas.config(width=countdown_size, height=countdown_size)
        self.countdown_canvas.place(x=pad, y=pad + (row_h-countdown_size)//2)

        self.record_btn.resize(size)
        self.record_btn.place(x=pad + countdown_size + gap, y=pad + (row_h-size)//2)

        self.close_btn.resize(close_size)
        self.close_btn.place(x=pad + countdown_size + gap + size + gap,
                             y=pad + (row_h-close_size)//2)

        self._text_w = text_w
        self._label_height = height
        self._font_size_logical = L["font_size"]

        if show_text and self.status_lbl is not None:
            self.status_lbl.config(font=ui_font(L["font_size"], bold=True),
                                   wraplength=text_w-10)
            self.status_lbl.place(
                x=pad + countdown_size + gap + size + gap + close_size + gap,
                y=0, width=text_w, height=height)
            # Re-fit the FULL original message (not the already-truncated
            # display text) to the new box size, so growing the widget
            # back up can recover text that was previously cut off.
            full_msg = getattr(self, "_last_full_status", self.status_var.get())
            self.set_status(full_msg, self.status_lbl.cget("fg"))
        # Note: turning the text on/off requires a full rebuild (a Label
        # can't be cleanly added/removed via place() alone) — that's
        # handled by closing and reopening the widget when the Settings
        # checkbox changes, not by this live-resize method.


class SimpleApp:
    def __init__(self, root):
        log_event("APP", f"started — version {APP_VERSION}, platform {sys.platform}")
        self.root = root
        self.cfg = load_config()
        self.recorder = AudioRecorder(self.cfg["sample_rate"], self.cfg["channels"])
        self.speaker = Speaker(self.cfg.get("speech_rate", 165))
        self._recording = False
        self._processing = False
        self._seg_results = {}
        self._seg_threads = []
        self._seg_lock = threading.Lock()
        self._seg_index = 0
        self._seg_cut = 0
        self._compact = False
        self._full_height = None   # remembered so "Full view" can restore it
        self._resize_job = None
        self._buttons = []         # list of (key, RoundButton)
        self.floating = None       # the FloatingWidget, if shown
        self._preferred_btn_size = int(self.cfg.get("button_size", 110))
        self._button_size_save_job = None
        self._popup_size_save_job = None

        root.title("Voice to Text")
        root.configure(bg=C_BG)
        root.resizable(True, True)

        # ── Size the window to fit whatever screen it's actually on ──────────
        root.update_idletasks()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        avail_h = screen_h - px(90)
        avail_w = screen_w - px(80)
        win_w = min(px(720), avail_w)
        win_h = min(px(820), avail_h)
        win_w = max(win_w, px(460))
        win_h = max(win_h, px(480))
        # Safety net: never let the floor above push the window past the
        # actual screen size, even if DPI scaling was detected incorrectly.
        win_w = min(win_w, screen_w - 20)
        win_h = min(win_h, screen_h - 20)
        x = (screen_w - win_w) // 2
        y = max(20, (screen_h - win_h) // 2)
        root.geometry(f"{win_w}x{win_h}+{x}+{y}")
        root.minsize(px(460), px(420))
        self._full_height = win_h

        # Outer frame lets us guarantee the button row a spot at the very
        # bottom of the window no matter how tall the text area wants to be.
        outer = tk.Frame(root, bg=C_BG)
        outer.pack(fill="both", expand=True)
        self._outer = outer

        # Two independent size sliders, side by side, below the button
        # row at the very bottom of the window — one for the main
        # window's buttons, one for the floating popup, so they can be
        # set to different sizes rather than being tied together.
        # Packed with side="bottom" BEFORE btn_frame, so this claims the
        # true bottom of the window and the buttons sit just above it.
        sliders_row = tk.Frame(outer, bg=C_BG)
        sliders_row.pack(side="bottom", fill="x", padx=px(24), pady=(0, px(12)))

        self.button_size_var, self.size_value_lbl, self.button_size_slider = \
            self._build_size_card(sliders_row, "Button size", self._preferred_btn_size,
                                  self._on_button_size_slider, side="left",
                                  pad=(0, px(6)))

        popup_initial = int(self.cfg.get("floating_size", 110))
        self.popup_size_var, self.popup_size_value_lbl, self.popup_size_slider = \
            self._build_size_card(sliders_row, "Popup size", popup_initial,
                                  self._on_popup_size_slider, side="left",
                                  pad=(px(6), 0))

        # Button area — packed to the BOTTOM (now sits just above the
        # sliders), so it always keeps its spot even if the window is short.
        self.btn_frame = tk.Frame(outer, bg=C_BG)
        self.btn_frame.pack(side="bottom", pady=px(14))

        # Top bar: wordmark left, compact-toggle + gear right
        top = tk.Frame(outer, bg=C_BG)
        top.pack(side="top", fill="x", padx=px(24), pady=(px(14), 0))

        # Row 1: just the title, so it never has to compete for space
        # with the control buttons and squeeze them out of view.
        title_row = tk.Frame(top, bg=C_BG)
        title_row.pack(side="top", fill="x")
        self.title_lbl = tk.Label(title_row, text="Voice to Text", font=ui_font(17, bold=True),
                         bg=C_BG, fg=C_TEXT)
        self.title_lbl.pack(side="left")

        # Row 2: always its own row underneath the title, so shrinking the
        # window never pushes these out of sight or hides them behind
        # anything — they simply always have this row to themselves.
        controls_row = tk.Frame(top, bg=C_BG)
        controls_row.pack(side="top", fill="x", pady=(px(8), 0))

        def _chip(parent, text, command):
            """A small pill-style button — filled background and a border
            so it reads clearly as a clickable control, not plain text."""
            return tk.Button(parent, text=text, font=ui_font(9, bold=True),
                             bd=1, relief="solid", cursor="hand2",
                             padx=px(10), pady=px(4), command=command)

        gear = _chip(controls_row, "\u2699 Settings", self._open_settings)
        gear.config(bg=C_SURFACE, fg=C_TEXT, activebackground=C_BORDER,
                   highlightbackground=C_BORDER)
        gear.pack(side="right")

        self.compact_btn = _chip(controls_row, "Compact view", self._toggle_compact)
        self.compact_btn.config(bg=C_SURFACE, fg=C_TEXT, activebackground=C_BORDER,
                               highlightbackground=C_BORDER)
        self.compact_btn.pack(side="right", padx=(0, px(8)))

        self.floating_toggle_btn = _chip(controls_row, "", self._toggle_floating_button)
        self.floating_toggle_btn.pack(side="right", padx=(0, px(8)))
        self._refresh_floating_toggle_btn()

        # Status row: spinner + message side by side
        self.status_row = tk.Frame(outer, bg=C_BG)
        self.status_row.pack(side="top", pady=8)
        self.spinner = Spinner(self.status_row, size=px(36), width=max(2, px(5)))
        self.spinner.pack(side="left", padx=(0, 10))
        self.spinner.stop()
        self.status_var = tk.StringVar(value="Press the green button and start talking")
        self.status_lbl = tk.Label(self.status_row, textvariable=self.status_var, font=ui_font(14, bold=True),
                                    bg=C_BG, fg=C_GREEN, wraplength=min(620, win_w-80))
        self.status_lbl.pack(side="left")

        # Text area — white card with a soft hairline border. Fills whatever
        # vertical space remains between the status row and the buttons.
        self.text_frame = tk.Frame(outer, bg=C_BORDER, bd=0)
        self.text_frame.pack(side="top", fill="both", expand=True, padx=px(24), pady=px(6))
        inner = tk.Frame(self.text_frame, bg=C_SURFACE)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.text = tk.Text(inner, font=ui_font(19), wrap="word",
                            fg=C_TEXT, bg=C_SURFACE, bd=0, padx=20, pady=18,
                            insertbackground=C_TEXT, selectbackground="#d8e4dd")
        self.text.pack(fill="both", expand=True)

        self._build_buttons()

        # Recompute button sizing whenever the window is resized, so the
        # layout genuinely adapts instead of being fixed at startup.
        root.bind("<Configure>", self._on_window_configure)

        if not self.cfg.get("groq_api_key"):
            self._set_status("Click the gear (top right) to add your Groq key", C_RED)
            self.root.after(300, self._open_settings_first_run)

        if self.cfg.get("show_floating"):
            self.root.after(30, self._open_floating_widget)

        # Auto-close after inactivity: checked periodically rather than
        # with one long timer, so a change to the setting takes effect on
        # the very next check instead of needing a restart.
        self.root.after(15000, self._check_auto_close)

    # ── Floating widget ──────────────────────────────────────────────────────
    def _open_floating_widget(self):
        if self.floating is not None:
            return
        try:
            self.floating = FloatingWidget(self)
            log_event("APP", "floating widget shown")
        except Exception as e:
            log_error("open_floating_widget", e)

    def _close_floating_widget(self):
        if self.floating is not None:
            try:
                self.floating.destroy()
            except Exception:
                pass
            self.floating = None
            log_event("APP", "floating widget hidden")

    def _toggle_floating_setting(self, show):
        self.cfg["show_floating"] = show
        self.cfg["floating_choice_made"] = True
        save_config(self.cfg)
        if show:
            self._open_floating_widget()
        else:
            self._close_floating_widget()

    def _toggle_floating_button(self):
        """Main-screen toggle (top bar) for the floating widget — quicker
        than going through Settings. On by default; this just flips it."""
        new_state = not self.cfg.get("show_floating", True)
        log_event("BUTTON", f"Floating toggle clicked -> {'on' if new_state else 'off'}")
        self._toggle_floating_setting(new_state)
        self._refresh_floating_toggle_btn()

    def _refresh_floating_toggle_btn(self):
        on = self.cfg.get("show_floating", True)
        if on:
            self.floating_toggle_btn.config(
                text="\u25cf Floating: On", fg="white", bg=C_GREEN,
                activebackground=C_GREEN_H, highlightbackground=C_GREEN)
        else:
            self.floating_toggle_btn.config(
                text="\u25cb Floating: Off", fg=C_TEXT, bg=C_SURFACE,
                activebackground=C_BORDER, highlightbackground=C_BORDER)

    # ── Auto-close after inactivity ───────────────────────────────────────────
    def _check_auto_close(self):
        try:
            if self.cfg.get("auto_close_enabled") and not self._recording and not self._processing:
                minutes = max(2, min(5, int(self.cfg.get("auto_close_minutes", 3))))
                idle = seconds_since_last_activity()
                if idle >= minutes * 60:
                    log_event("APP", f"auto-closing after {minutes} min of no use")
                    self._quit_app()
                    return
        except Exception as e:
            log_error("check_auto_close", e)
        # Check again in 15 seconds — frequent enough to close promptly
        # once the limit is reached, cheap enough to not matter.
        self.root.after(15000, self._check_auto_close)

    def _quit_app(self):
        try:
            if self.floating is not None:
                self.floating.destroy()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    # ── Responsive layout ────────────────────────────────────────────────────
    def _on_window_configure(self, event):
        # Only react to the root window itself resizing, and debounce so we
        # don't recompute on every pixel while the user drags the edge.
        if event.widget is not self.root:
            return
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(120, self._apply_responsive_size)

    def _build_size_card(self, parent, title, initial_value, on_change, side="left", pad=(0, 0)):
        """
        Builds one size-slider card (label + value badge + slider) and
        returns (IntVar, value_label, slider) so the caller can reference
        them later. Used twice — once for the button size, once for the
        popup size — so the two controls look identical but work on
        their own IntVars and callbacks, fully independent of each other.
        """
        card_outer = tk.Frame(parent, bg=C_BG)
        card_outer.pack(side=side, fill="both", expand=True, padx=pad)

        card = tk.Frame(card_outer, bg=C_SURFACE,
                        highlightbackground=C_BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True)

        label_row = tk.Frame(card, bg=C_SURFACE)
        label_row.pack(fill="x", padx=px(14), pady=(px(10), px(2)))
        tk.Label(label_row, text=title, font=ui_font(11, bold=True),
                bg=C_SURFACE, fg=C_TEXT).pack(side="left")
        value_lbl = tk.Label(label_row, text=f"{initial_value} px",
                             font=ui_font(11, bold=True), bg=C_BLUE, fg="white",
                             padx=px(8), pady=px(1))
        value_lbl.pack(side="right")

        var = tk.IntVar(value=initial_value)
        slider = tk.Scale(
            card, from_=FloatingWidget.MIN_SIZE, to=FloatingWidget.MAX_SIZE,
            orient="horizontal", variable=var,
            bg=C_SURFACE, fg=C_TEXT, troughcolor=C_BORDER, highlightthickness=0,
            showvalue=False, sliderlength=px(34), width=px(24),
            sliderrelief="raised", activebackground=C_BLUE, bd=0,
            command=on_change)
        slider.pack(fill="x", padx=px(12), pady=(0, px(10)))

        return var, value_lbl, slider

    def _on_button_size_slider(self, v):
        """Live preview for the main window's buttons — independent of
        the popup's own size slider."""
        n = int(float(v))
        self._preferred_btn_size = n
        self.size_value_lbl.config(text=f"{n} px")
        self._apply_responsive_size()

        if self._button_size_save_job:
            self.root.after_cancel(self._button_size_save_job)
        self._button_size_save_job = self.root.after(
            250, lambda: self._persist_button_size(n))

    def _persist_button_size(self, n):
        self._button_size_save_job = None
        self.cfg["button_size"] = n
        save_config(self.cfg)
        log_event("SETTINGS", f"button size set to {n}px")

    def _on_popup_size_slider(self, v):
        """Live preview for the floating popup — independent of the main
        window's button size slider."""
        n = int(float(v))
        self.popup_size_value_lbl.config(text=f"{n} px")
        if self.floating is not None:
            self.floating.apply_size(n)

        if self._popup_size_save_job:
            self.root.after_cancel(self._popup_size_save_job)
        self._popup_size_save_job = self.root.after(
            250, lambda: self._persist_popup_size(n))

    def _persist_popup_size(self, n):
        self._popup_size_save_job = None
        self.cfg["floating_size"] = n
        save_config(self.cfg)
        log_event("SETTINGS", f"popup size set to {n}px")

    def _apply_responsive_size(self):
        self._resize_job = None
        if not self._buttons:
            return
        win_w = self.root.winfo_width()
        win_h = self.root.winfo_height()
        if win_w < 50 or win_h < 50:
            return  # window not really laid out yet

        n = len(self._buttons)
        gap = px(14)

        # Single row: all buttons share the window's width.
        avail_w = win_w - px(48)
        max_w = (avail_w - (n-1)*gap) / n

        if self._compact:
            # In compact mode the buttons ARE the window, so let them use
            # most of the available height too.
            avail_h = win_h - px(60)
        else:
            # Leave most of the height for the text area, but don't starve
            # the buttons on ordinary laptop screens.
            avail_h = win_h * 0.48

        # A single row can only grow as tall as one button, so height is
        # rarely the limiting factor here — width usually is once there
        # are 3-4 buttons side by side.
        #
        # IMPORTANT: fitting the actual window always wins. Forcing a
        # minimum size regardless of available space is what caused
        # buttons and text to overflow off the window on some screens.
        # Only an absolute tiny floor remains, purely so buttons can never
        # shrink to literally nothing.
        fit_size = min(px(self._preferred_btn_size), max_w, avail_h)
        new_size = int(max(px(40), fit_size))
        self._btn_size_current = new_size

        for _, btn in self._buttons:
            btn.resize(new_size)

        # Each button independently shrinks its own label to fit ("COPY
        # TEXT" needs a smaller font than "RECORD" to fit the same button
        # width), which looks inconsistent side by side. Even them all out
        # by using the smallest size any button actually needed, applied
        # to every button in the row.
        fitted = [btn.get_fitted_font_size() for _, btn in self._buttons]
        fitted = [f for f in fitted if f]
        if fitted:
            uniform_size = min(fitted)
            for _, btn in self._buttons:
                btn.force_label_font(uniform_size)

        wrap = max(200, min(620, win_w - 80))

        # Font sizes now scale with the same responsive button size, so the
        # text actually shrinks and grows with the window instead of just
        # wrapping a fixed-size font into more and more lines (which is
        # what caused text to overflow badly on smaller windows before).
        # These are computed directly in pixels from new_size, which is
        # already DPI-scaled — going through ui_font() again here would
        # double-scale it.
        fam = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"
        status_font_px = max(11, min(22, int(new_size * 0.10)))
        text_font_px   = max(12, min(24, int(new_size * 0.12)))
        title_font_px  = max(14, min(26, int(new_size * 0.13)))

        self.status_lbl.config(wraplength=wrap, font=(fam, status_font_px, "bold"))
        self.text.config(font=(fam, text_font_px))
        if hasattr(self, "title_lbl"):
            self.title_lbl.config(font=(fam, title_font_px, "bold"))

    # ── Compact / full view toggle ───────────────────────────────────────────
    def _toggle_compact(self):
        self._compact = not self._compact
        log_event("BUTTON", f"view toggled -> {'compact' if self._compact else 'full'}")
        if self._compact:
            self._full_height = self.root.winfo_height()
            self.status_row.pack_forget()
            self.text_frame.pack_forget()
            self.compact_btn.config(text="Full view")
            # Shrink the window to just fit the top bar + buttons
            self.root.update_idletasks()
            new_h = self.btn_frame.winfo_reqheight() + \
                    self._outer.winfo_children()[1].winfo_reqheight() + 40
            win_w = self.root.winfo_width()
            self.root.geometry(f"{win_w}x{max(220, new_h)}")
            self.root.minsize(px(460), px(180))
        else:
            self.status_row.pack(side="top", pady=8)
            self.text_frame.pack(side="top", fill="both", expand=True, padx=px(24), pady=px(6))
            self.compact_btn.config(text="Compact view")
            win_w = self.root.winfo_width()
            self.root.geometry(f"{win_w}x{self._full_height or 700}")
            self.root.minsize(px(460), px(420))
        self.root.after(150, self._apply_responsive_size)

    # ── Buttons ───────────────────────────────────────────────────────────────
    def _build_buttons(self):
        for child in self.btn_frame.winfo_children():
            child.destroy()
        self._buttons = []

        specs = [("record", "record", "RECORD", C_GREEN, C_GREEN_H, self._on_record)]
        if self.cfg.get("show_copy", True):
            specs.append(("copy", "copy", "COPY TEXT", C_BLUE, C_BLUE_H, self._on_copy))
        if self.cfg.get("show_read", True):
            specs.append(("read", "read", "READ ALOUD", C_PURPLE, C_PURPLE_H, self._on_read))
        if self.cfg.get("show_clear", True):
            specs.append(("clear", "clear", "START OVER", C_GREY, C_GREY_H, self._on_clear))

        # All buttons sit in a single horizontal line rather than a grid —
        # cleaner and easier to scan at a glance.
        size = getattr(self, "_btn_size_current", px(190))
        for i, (key, icon, label, colour, hover, cmd) in enumerate(specs):
            btn = RoundButton(self.btn_frame, icon, label, colour, hover, cmd, size=size)
            btn.grid(row=0, column=i, padx=10, pady=8)
            self._buttons.append((key, btn))
            if key == "record":
                self.record_btn = btn

        self.root.after(50, self._apply_responsive_size)

    def _open_settings(self):
        log_event("BUTTON", "Settings (gear) clicked")
        SettingsWindow(self.root, self.cfg, on_save=self._after_settings, app=self)

    def _open_settings_first_run(self):
        log_event("APP", "first-run settings popup shown (no key set)")
        SettingsWindow(self.root, self.cfg, on_save=self._after_settings, first_run=True, app=self)

    def _after_settings(self):
        log_event("SETTINGS", "saved — "
                  f"buttons: copy={self.cfg.get('show_copy')} "
                  f"read={self.cfg.get('show_read')} "
                  f"clear={self.cfg.get('show_clear')}, "
                  f"auto_type={self.cfg.get('auto_type')}, "
                  f"floating={self.cfg.get('show_floating')}")
        self._build_buttons()
        if self.cfg.get("show_floating"):
            # Rebuild fresh even if it was already open, so a changed size
            # takes effect immediately rather than needing a manual toggle.
            self._close_floating_widget()
            self._open_floating_widget()
        else:
            self._close_floating_widget()
        self._refresh_floating_toggle_btn()
        if self.cfg.get("groq_api_key"):
            self._set_status("Ready. Press the green button and start talking", C_GREEN)

    # ── Actions ───────────────────────────────────────────────────────────────
    def _on_record(self):
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        log_event("BUTTON", "Record clicked (start)")
        if self._processing:
            log_event("RECORD", "ignored — still processing previous recording")
            return
        if not self.cfg.get("groq_api_key"):
            log_event("RECORD", "blocked — no Groq API key set")
            self._set_status("Please add your Groq key first (gear, top right)", C_RED)
            return
        self._recording = True
        self.recorder.start()

        if self.recorder.open_error:
            # The microphone couldn't be opened at all — almost always a
            # Windows permissions problem, not "no speech". Say so clearly.
            log_event("MIC", f"failed to open — {self.recorder.open_error}")
            self._recording = False
            self.record_btn.pulse_stop()
            self.record_btn.set(icon="record", label="RECORD", colour=C_GREEN, hover=C_GREEN_H)
            if self.floating is not None:
                self.floating.set_recording_state(False)
            self._set_status(
                "Can't reach the microphone — check the gear for Microphone help",
                C_RED)
            return

        log_event("RECORD", "started listening")
        self.record_btn.set(icon="stop", label="STOP", colour=C_RED, hover=C_RED_H)
        self.record_btn.pulse_start()
        if self.floating is not None:
            self.floating.set_recording_state(True)
        self._set_status("Listening... press STOP when you are done", C_RED)

        # ── Streaming setup ───────────────────────────────────────────────────
        # As he talks, we cut the audio at natural pauses and transcribe each
        # piece in the background. By the time he presses STOP, most of the
        # words are already written, so the final wait is short.
        self._seg_results = {}     # index -> transcribed text
        self._seg_threads = []
        self._seg_lock = threading.Lock()
        self._seg_index = 0
        self._seg_cut = 0          # frame position where the next segment starts
        self._seg_errors = []      # exceptions from failed segment transcriptions
        # Dedicated lock guarding _seg_cut itself. Without this, the
        # background segmenter and the final "leftover" cut in _process can
        # both read the same stale cut point at once and each transcribe the
        # same stretch of audio — the cause of words randomly appearing
        # twice in the output. Every read-then-update of _seg_cut must go
        # through this lock as one atomic step.
        self._seg_cut_lock = threading.Lock()
        threading.Thread(target=self._segmenter_loop, daemon=True).start()

    @safe_thread
    def _segmenter_loop(self):
        rate = self.cfg["sample_rate"]
        fps = rate / 1024.0                       # frames per second (~15.6)
        min_seg = int(fps * 6)                    # don't cut before ~6s
        max_seg = int(fps * 18)                   # force a cut by ~18s
        while self._recording:
            time.sleep(0.4)
            with self._seg_cut_lock:
                if not self._recording:
                    break   # stopped while we were sleeping — don't cut
                n = self.recorder.frame_count()
                grown = n - self._seg_cut
                if grown < min_seg:
                    continue
                # Cut at a natural pause, or force a cut if the segment is long
                if self.recorder.recent_is_pause() or grown >= max_seg:
                    self._launch_segment(self._seg_cut, n)
                    self._seg_cut = n

    def _launch_segment(self, start, end):
        idx = self._seg_index
        self._seg_index += 1
        raw = self.recorder.slice_bytes(start, end)

        def work():
            try:
                text = transcribe_bytes(raw, self.cfg,
                                        self.cfg["sample_rate"], self.cfg["channels"])
                with self._seg_lock:
                    self._seg_results[idx] = text
            except Exception as e:
                # Don't fail silently: a network/VPN block would otherwise
                # just look like "no speech detected", which is misleading.
                log_error(f"segment:{idx}", e)
                log_event("TRANSCRIBE", f"segment {idx} failed: {type(e).__name__}: {e}")
                with self._seg_lock:
                    self._seg_results[idx] = ""
                    self._seg_errors.append(e)
        t = threading.Thread(target=work, daemon=True)
        t.start()
        self._seg_threads.append(t)

    def _stop_recording(self):
        self._recording = False
        self.recorder.stop()
        self.record_btn.pulse_stop()
        self.record_btn.set(icon="record", label="RECORD", colour=C_GREEN, hover=C_GREEN_H)
        if self.floating is not None:
            self.floating.set_recording_state(False)
        dur = self.recorder.duration()
        log_event("BUTTON", f"Record clicked (stop) — recorded {dur:.1f}s")

        if dur < 0.4:
            log_event("RECORD", "too short, discarded")
            self._set_status("That was too quick — please try again", C_MUTED)
            return
        kind = self.recorder.silence_kind()
        log_event("RECORD", f"silence check: {kind}")
        if kind == "blocked":
            self._set_status(
                "No sound reached the app — check the gear for Microphone help",
                C_RED)
            return
        if kind == "quiet":
            self._set_status("I didn't hear anything — please try again", C_MUTED)
            return

        self._processing = True
        self._spin_start(C_BLUE)
        self._set_status("Finishing your words...", C_BLUE)
        threading.Thread(target=self._process, daemon=True).start()

    def _process(self):
        try:
            # Transcribe the final leftover segment (from last cut to the end).
            # Guarded by the same lock as the background segmenter so the two
            # can never grab overlapping audio ranges (that race was the
            # cause of words occasionally appearing twice).
            with self._seg_cut_lock:
                total = self.recorder.frame_count()
                if total > self._seg_cut:
                    self._launch_segment(self._seg_cut, total)
                    self._seg_cut = total

            # Wait for all segment transcriptions to finish
            for t in list(self._seg_threads):
                t.join(timeout=30)

            # Stitch the segments back together in order
            with self._seg_lock:
                parts = [self._seg_results.get(i, "")
                         for i in range(self._seg_index)]
            raw = " ".join(p for p in parts if p).strip()
            raw = re.sub(r"\s{2,}", " ", raw)
            log_event("TRANSCRIBE", f"{self._seg_index} segment(s) -> "
                                    f"{len(raw)} chars: \"{raw[:80]}"
                                    f"{'...' if len(raw) > 80 else ''}\"")

            if not raw or is_probable_hallucination(raw):
                if self._seg_errors:
                    # The segments didn't come back empty because of silence —
                    # they failed. Give the real reason instead of implying
                    # nothing was heard.
                    msg = classify_api_error(self._seg_errors[0])
                    log_event("TRANSCRIBE", f"all segments failed — {self._seg_errors[0]}")
                    self._set_status(
                        msg or "Couldn't reach Groq — please check your connection",
                        C_RED)
                else:
                    log_event("TRANSCRIBE", "empty or hallucination-filtered — discarded")
                    self._set_status("I didn't catch any words — please try again", C_MUTED)
                return

            self._spin_start(C_PURPLE)
            self._set_status("Tidying it up...", C_PURPLE)
            provider = self.cfg.get("ai_provider", "groq") if self.cfg.get("ai_cleanup") else "none"
            log_event("CLEANUP", f"provider={provider}")
            cleaned = ai_cleanup(raw, self.cfg)
            log_event("CLEANUP", f"result: {len(cleaned)} chars: \"{cleaned[:80]}"
                                 f"{'...' if len(cleaned) > 80 else ''}\"")
            if not cleaned.strip() or is_probable_hallucination(cleaned):
                log_event("CLEANUP", "empty or hallucination-filtered after cleanup — discarded")
                self._set_status("I didn't catch that clearly — please try again", C_MUTED)
                return

            self._append_text(cleaned)

            if self.cfg.get("auto_type", True):
                log_event("TYPE", "auto-type enabled — starting countdown")
                self._countdown_and_type(cleaned)
            else:
                log_event("DONE", "text ready (auto-type off)")
                self._set_status("Done! You can record more, copy, or read it aloud", C_GREEN)
        except Exception as e:
            log_error("_process", e)
            log_event("ERROR", f"_process failed: {type(e).__name__}: {e}")
            msg = classify_api_error(e) or "Something went wrong — please try again"
            self._set_status(msg, C_RED if classify_api_error(e) else C_MUTED)
        finally:
            self._processing = False
            self._spin_stop()

    def _countdown_and_type(self, text):
        """Countdown (configurable, 3-15s) so the user can click where the
        words should go, then the words get typed/pasted at the cursor.
        Also shown as a number in the floating popup's countdown circle,
        to the left of the record button, so there's a clear visual cue
        right on the popup itself."""
        delay = int(self.cfg.get("type_delay", 5))
        delay = max(3, min(15, delay))
        for remaining in range(delay, 0, -1):
            self._set_status(
                f"Click where the words should go...  typing in {remaining}",
                C_BLUE)
            if self.floating is not None:
                self.floating.show_countdown(remaining)
            time.sleep(1)
        if self.floating is not None:
            self.floating.clear_countdown()
        self._set_status("Typing your words...", C_BLUE)
        self._type_at_cursor(text + " ")

    def _type_at_cursor(self, text):
        """
        Insert text at the cursor. On Windows this uses direct Unicode
        character injection, which works correctly regardless of whatever
        keyboard layout is active — that's the general fix for symbols
        coming out wrong, rather than trying to detect and match a specific
        layout. If that's unavailable for any reason, or on other
        platforms, it falls back to clipboard + paste.
        """
        if sys.platform.startswith("win"):
            try:
                type_unicode_windows(text)
                log_event("TYPE", "typed via Unicode injection (layout-independent)")
                self._set_status("Done! Your words have been typed", C_GREEN)
                return
            except Exception as e:
                log_error("type_unicode_windows", e)
                log_event("TYPE", f"Unicode injection failed, trying paste instead: {e}")

        try:
            self._paste_text(text)
            log_event("TYPE", "typed via clipboard paste")
            self._set_status("Done! Your words have been typed", C_GREEN)
        except Exception as e:
            log_error("countdown_and_type", e)
            log_event("TYPE", f"failed: {type(e).__name__}: {e}")
            self._set_status("Couldn't type there — use the COPY button instead", C_MUTED)

    def _paste_text(self, text):
        """
        Insert text at the cursor via clipboard + paste. Used as the
        fallback on Windows, and as the primary method elsewhere.
        """
        # Remember whatever was on the clipboard so we can restore it after,
        # so this doesn't clobber something the person had copied earlier.
        old_clip = None
        try:
            old_clip = self.root.clipboard_get()
        except Exception:
            old_clip = None

        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()          # flush to the OS clipboard
        time.sleep(0.15)            # give the OS a moment to register it

        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.1)

        # Restore the previous clipboard content shortly after, without
        # blocking the UI.
        def _restore():
            try:
                if old_clip is not None:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(old_clip)
            except Exception:
                pass
        self.root.after(800, _restore)

    def _on_copy(self):
        log_event("BUTTON", "Copy Text clicked")
        text = self._get_text()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._flash("Copied! You can now paste it anywhere", C_BLUE)

    def _on_read(self):
        log_event("BUTTON", "Read Aloud clicked")
        text = self._get_text()
        if text:
            self.speaker.speak(text)
            self._flash("Reading aloud...", C_PURPLE)
        else:
            self._flash("There is no text to read yet", C_MUTED)

    def _on_clear(self):
        log_event("BUTTON", "Start Over clicked")
        self.speaker.stop()
        self.text.delete("1.0", "end")
        self._set_status("Press the green button and start talking", C_GREEN)

    def _clear_text_for_next_recording(self):
        """
        Used by the floating widget: wipes the text box right before a new
        recording starts, so each bit of speech starts fresh instead of
        piling onto whatever was said before. Quieter than Start Over —
        no status message change, since a new recording is about to begin
        anyway.
        """
        self.text.delete("1.0", "end")
        log_event("FLOATING", "cleared text box for next recording")

    # ── Helpers (thread-safe via root.after) ──────────────────────────────────
    def _get_text(self):
        return self.text.get("1.0", "end").strip()

    def _append_text(self, new):
        def do():
            existing = self._get_text()
            if existing:
                self.text.insert("end", " " + new)
            else:
                self.text.insert("end", new)
        self.root.after(0, do)

    def _set_status(self, msg, colour=C_GREEN):
        def do():
            self.status_var.set(msg)
            self.status_lbl.config(fg=colour)
            if self.floating is not None:
                self.floating.set_status(msg, colour)
        self.root.after(0, do)

    def _spin_start(self, colour=C_BLUE):
        self.root.after(0, lambda: self.spinner.start(colour))

    def _spin_stop(self):
        self.root.after(0, self.spinner.stop)

    def _flash(self, msg, colour):
        self._set_status(msg, colour)
        self.root.after(2500, lambda: self._set_status(
            "Press the green button and start talking", C_GREEN))

# ─── Setup wizard (optional, terminal) ────────────────────────────────────────
def setup_wizard():
    print("\n=== Voice to Text — Setup ===\n")
    print("Get a free Groq API key at https://console.groq.com\n")
    cfg = load_config()
    key = input("Paste your Groq API key: ").strip()
    if key: cfg["groq_api_key"] = key
    save_config(cfg)
    print("\nSaved. Now run the app normally.\n")


def ensure_desktop_shortcut():
    """On Windows, create a Desktop shortcut to this app on first run (once)."""
    if not sys.platform.startswith("win"):
        return
    try:
        marker = CONFIG_DIR / ".shortcut_done"
        if marker.exists():
            return
        desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
        if not desktop.exists():
            return
        # If running as a frozen .exe, point at the exe; else at the script
        if getattr(sys, "frozen", False):
            target = sys.executable
        else:
            target = os.path.abspath(__file__)
        lnk = desktop / "Voice to Text.lnk"
        ico = ""
        # Look for the icon next to the exe/script
        here = Path(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))))
        cand = here / "voicetotext.ico"
        if cand.exists():
            ico = str(cand)
        ps = (
            f"$s=(New-Object -COM WScript.Shell).CreateShortcut('{lnk}');"
            f"$s.TargetPath='{target}';"
            + (f"$s.IconLocation='{ico}';" if ico else "")
            + "$s.Save()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=10)
        marker.write_text("done")
    except Exception:
        pass


def _autostart_target():
    """The path to register for Windows startup — the .exe itself if
    running as a built app, otherwise this script run via pythonw (no
    console window) for testing."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(pythonw) if pythonw.exists() else sys.executable
    script = os.path.abspath(__file__)
    return f'"{interpreter}" "{script}"'

def set_autostart(enabled):
    """Turn 'start automatically when Windows starts' on or off, via the
    standard per-user registry Run key — no admin rights needed, and it's
    easy to toggle from Settings without touching the Startup folder."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, "VoiceToText", 0, winreg.REG_SZ, _autostart_target())
            else:
                try:
                    winreg.DeleteValue(key, "VoiceToText")
                except FileNotFoundError:
                    pass
        log_event("APP", f"auto-launch on startup set to {enabled}")
        return True
    except Exception as e:
        log_error("set_autostart", e)
        return False

def get_autostart_state():
    """Check what Windows actually has registered, in case it was changed
    outside the app (e.g. via Task Manager's Startup tab)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, "VoiceToText")
            return True
    except Exception:
        return False


def main():
    if "--setup" in sys.argv:
        setup_wizard()
        return
    try:
        ensure_desktop_shortcut()
        root = tk.Tk()

        # Work out the real display scaling now that a window exists, and
        # store it globally so every subsequently-built widget (fonts,
        # button sizes, paddings — all computed via ui_font()/px()) scales
        # to match. 96 DPI is Windows' 100% baseline; winfo_fpixels('1i')
        # returns however many pixels actually make up one inch on this
        # specific display right now, whatever its scaling is set to.
        #
        # Windows' own DPI reporting is not always reliable — some setups
        # (particularly frozen .exe builds) can end up with the display
        # scaling applied twice, making everything oversized. If the
        # person has set a manual correction in Settings, it overrides
        # the automatic detection entirely.
        global DPI_SCALE
        cfg_preview = load_config()
        override = cfg_preview.get("scale_override")
        if override:
            DPI_SCALE = max(0.4, min(3.0, float(override)))
            log_event("APP", f"DPI scale: using manual override {DPI_SCALE:.2f}")
        else:
            try:
                measured = root.winfo_fpixels("1i") / 96.0
                DPI_SCALE = max(0.75, min(3.0, measured))
            except Exception:
                DPI_SCALE = 1.0
            log_event("APP", f"DPI scale detected: {DPI_SCALE:.2f}")

        root.withdraw()   # avoid a flash of unscaled content while building
        SimpleApp(root)
        root.deiconify()
        root.mainloop()
    except Exception as e:
        log_error("startup", e)
        # Show a simple dialog so a startup crash isn't silent
        try:
            import tkinter.messagebox as mb
            mb.showerror("Voice to Text",
                         "The app had a problem starting.\n\n"
                         "An error report was saved. Please send it to Konrad.\n\n"
                         f"Location:\n{CRASH_LOG}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()

# ─────────────────────────────────────────────────────────────────────────────
# BUILDING A STANDALONE .EXE (run on the Windows laptop):
#
#   1. Install Python from python.org (tick "Add Python to PATH")
#   2. Open Command Prompt and run:
#         pip install pyinstaller pyaudio groq requests pyttsx3
#   3. Then build:
#         pyinstaller --onefile --noconsole --name "VoiceToText" voice_simple_win.py
#   4. The .exe appears in the "dist" folder. Double-click to run.
#
# Note: pyttsx3 on Windows uses the built-in SAPI5 voice (no install needed).
# ─────────────────────────────────────────────────────────────────────────────
