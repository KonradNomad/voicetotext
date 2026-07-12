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

APP_VERSION = "2.5.0"

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
    "speech_rate":       165,   # words per minute for read-aloud
}

def load_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
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
GENTLE_PROMPT = """You are a gentle transcription editor. Clean up this dictated text so it reads clearly.

STRICT RULES — these are absolute:
1. NEVER add words, sentences, ideas, or information that are not in the original. This is the most important rule.
2. If the text is empty, blank, or just noise, return nothing at all.
3. Only fix: grammar, spelling, punctuation, capitalisation, and obvious speech-recognition errors.
4. Remove filler words (um, uh, er) and false starts, but keep the speaker's own words and meaning exactly.
5. Do not summarise, expand, rephrase for style, or "improve" the content. Keep it faithful.
6. Do not add greetings, sign-offs, or pleasantries like "thank you" or "hello" unless the speaker clearly said them.
7. Return ONLY the cleaned text, nothing else — no quotes, no commentary.

If you are unsure whether something was said, leave it out. Faithfulness matters more than polish."""

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

    def start(self):
        self._frames, self._recording = [], True
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(format=self.fmt, channels=self.channels,
                                     rate=self.rate, input=True, frames_per_buffer=self.chunk)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._recording:
            try:
                self._frames.append(self._stream.read(self.chunk, exception_on_overflow=False))
            except Exception:
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
        # RMS
        sq = sum(s*s for s in samples) / count
        rms = int(sq ** 0.5)
        return rms, peak

    def is_mostly_silent(self):
        if not self._frames:
            return True
        rms, peak = self._rms_and_peak()
        return rms < 120 and peak < 900

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
    client = Groq(api_key=cfg["groq_api_key"])
    kwargs = dict(file=("audio.wav", open(audio_path,"rb")),
                  model=cfg["model"], response_format="json", temperature=0)
    if cfg.get("language"):
        kwargs["language"] = cfg["language"]
    return client.audio.transcriptions.create(**kwargs).text.strip()

# ─── AI cleanup ───────────────────────────────────────────────────────────────
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
        return out.strip('"').strip()
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

def ui_font(size, bold=False):
    """Segoe UI on Windows, fallback elsewhere."""
    fam = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"
    return (fam, size, "bold") if bold else (fam, size)

BTN_FONT    = ui_font(19, bold=True)
TEXT_FONT   = ui_font(19)
STATUS_FONT = ui_font(14, bold=True)

# ─── Rounded button (canvas-drawn, hover + pulse ring) ────────────────────────
class RoundButton(tk.Canvas):
    """A modern rounded square button drawn on a canvas.
    Supports hover colour, label updates, colour changes, and a pulsing
    ring (used while recording)."""
    RADIUS = 26

    def __init__(self, parent, label, colour, hover, command,
                 size=200, font=None, bg=None):
        super().__init__(parent, width=size, height=size,
                         bg=bg or C_BG, highlightthickness=0, cursor="hand2")
        self._size = size
        self._colour = colour
        self._hover = hover
        self._command = command
        self._label_text = label
        self._font = font or BTN_FONT
        self._pulse_on = False
        self._pulse_step = 0

        self._draw(self._colour)
        self.bind("<Enter>", lambda e: self._draw(self._hover))
        self.bind("<Leave>", lambda e: self._draw(self._colour))
        self.bind("<Button-1>", lambda e: self._command())

    def _rounded_rect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0+r,y0, x1-r,y0, x1,y0, x1,y0+r, x1,y1-r, x1,y1,
               x1-r,y1, x0+r,y1, x0,y1, x0,y1-r, x0,y0+r, x0,y0]
        return self.create_polygon(pts, smooth=True, **kw)

    def _draw(self, fill):
        self.delete("all")
        s = self._size
        pad = 10  # leave room for the pulse ring
        # Pulse ring (drawn first, behind the button)
        if self._pulse_on:
            ring_pad = pad - 6 + (self._pulse_step % 3) * 2
            self._rounded_rect(ring_pad, ring_pad, s-ring_pad, s-ring_pad,
                               self.RADIUS+6, fill="", outline=self._colour,
                               width=3)
        self._rounded_rect(pad, pad, s-pad, s-pad, self.RADIUS, fill=fill, outline="")
        self.create_text(s/2, s/2, text=self._label_text, font=self._font,
                         fill="white", justify="center")
        self._current_fill = fill

    def set(self, label=None, colour=None, hover=None):
        if label is not None:  self._label_text = label
        if colour is not None: self._colour = colour
        if hover is not None:  self._hover = hover
        self._draw(self._colour)

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
        self.after(350, self._pulse)


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
    def __init__(self, parent, cfg, on_save, first_run=False):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=C_BG)
        self.cfg = cfg
        self.on_save = on_save
        self.transient(parent)
        self.grab_set()

        # ── Size to fit the screen, same fix as the main window ──────────────
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = min(480, screen_w - 80)
        win_h = min(600, screen_h - 100)
        win_w = max(win_w, 360)
        win_h = max(win_h, 420)
        x = (screen_w - win_w) // 2
        y = max(20, (screen_h - win_h) // 2)
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.minsize(340, 360)

        outer = tk.Frame(self, bg=C_BG)
        outer.pack(fill="both", expand=True)

        # ── Button bar: fixed at the very bottom, ALWAYS visible ─────────────
        # Packed first with side="bottom" so it keeps its spot no matter how
        # tall the scrollable content above becomes.
        btns = tk.Frame(outer, bg=C_BG)
        btns.pack(side="bottom", fill="x", pady=12)
        tk.Button(btns, text="Cancel", font=ui_font(11),
                  command=self.destroy).pack(side="right", padx=10)
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

        self.autotype_var = tk.BooleanVar(value=cfg.get("auto_type", True))
        tk.Checkbutton(body, text="Type the words where I click (after recording)",
                       variable=self.autotype_var, bg=C_BG,
                       font=ui_font(11)).pack(anchor="w", padx=20, pady=2)
        tk.Label(body, text="After it finishes, you get 5 seconds to click where\nthe words should go, then they type themselves.",
                 font=ui_font(9), bg=C_BG, fg=C_MUTED,
                 justify="left").pack(anchor="w", padx=30, pady=(0, 16))

        if first_run:
            # Big friendly first-run save button right in the flow too,
            # in case the user doesn't scroll down to the bottom bar.
            tk.Button(body, text="Save and start using the app",
                     font=ui_font(12, bold=True), bg=C_GREEN, fg="white",
                     padx=16, pady=8, command=self._save).pack(padx=20, pady=(0, 20))

    def _toggle_key(self):
        self.key_entry.config(show="" if self.show_var.get() else "*")

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

    def _save(self):
        self.cfg["groq_api_key"] = self.key_var.get().strip()
        self.cfg["show_copy"]  = self.copy_var.get()
        self.cfg["show_read"]  = self.read_var.get()
        self.cfg["show_clear"] = self.clear_var.get()
        self.cfg["auto_type"]  = self.autotype_var.get()
        save_config(self.cfg)
        self.on_save()
        self.destroy()

# ─── Main window ──────────────────────────────────────────────────────────────
class SimpleApp:
    def __init__(self, root):
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

        root.title("Voice to Text")
        root.configure(bg=C_BG)

        # ── Size the window to fit whatever screen it's actually on ──────────
        # Fixed pixel sizes broke on smaller laptop screens (buttons and the
        # Save button ended up rendered off-screen). This adapts instead.
        root.update_idletasks()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        # Leave room for the taskbar / window chrome
        avail_h = screen_h - 90
        avail_w = screen_w - 80
        win_w = min(720, avail_w)
        win_h = min(820, avail_h)
        win_w = max(win_w, 480)   # never go below a sane minimum
        win_h = max(win_h, 560)
        x = (screen_w - win_w) // 2
        y = max(20, (screen_h - win_h) // 2)
        root.geometry(f"{win_w}x{win_h}+{x}+{y}")
        root.minsize(460, 520)

        # Shrink the button size a little on very small screens so a full
        # 2x2 grid always fits without needing to scroll.
        self._btn_size = 200 if win_h >= 700 else (170 if win_h >= 600 else 150)

        # Outer frame lets us guarantee the button row a spot at the very
        # bottom of the window no matter how tall the text area wants to be.
        outer = tk.Frame(root, bg=C_BG)
        outer.pack(fill="both", expand=True)

        # Button area — packed to the BOTTOM first, so it always keeps its
        # spot even if the window is short. The text area fills what's left.
        self.btn_frame = tk.Frame(outer, bg=C_BG)
        self.btn_frame.pack(side="bottom", pady=14)

        # Top bar: wordmark left, gear right
        top = tk.Frame(outer, bg=C_BG)
        top.pack(side="top", fill="x", padx=24, pady=(14, 0))
        title = tk.Label(top, text="Voice to Text", font=ui_font(17, bold=True),
                         bg=C_BG, fg=C_TEXT)
        title.pack(side="left")
        gear = tk.Button(top, text="\u2699", font=ui_font(16), bd=0,
                         bg=C_BG, fg=C_MUTED, activebackground=C_BG,
                         activeforeground=C_TEXT,
                         cursor="hand2", command=self._open_settings)
        gear.pack(side="right")

        # Status row: spinner + message side by side
        status_row = tk.Frame(outer, bg=C_BG)
        status_row.pack(side="top", pady=8)
        self.spinner = Spinner(status_row, size=36, width=5)
        self.spinner.pack(side="left", padx=(0, 10))
        self.spinner.stop()
        self.status_var = tk.StringVar(value="Press the green button and start talking")
        self.status_lbl = tk.Label(status_row, textvariable=self.status_var, font=STATUS_FONT,
                                    bg=C_BG, fg=C_GREEN, wraplength=min(620, win_w-80))
        self.status_lbl.pack(side="left")

        # Text area — white card with a soft hairline border. Fills whatever
        # vertical space remains between the status row and the buttons.
        text_frame = tk.Frame(outer, bg=C_BORDER, bd=0)
        text_frame.pack(side="top", fill="both", expand=True, padx=24, pady=6)
        inner = tk.Frame(text_frame, bg=C_SURFACE)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.text = tk.Text(inner, font=TEXT_FONT, wrap="word",
                            fg=C_TEXT, bg=C_SURFACE, bd=0, padx=20, pady=18,
                            insertbackground=C_TEXT, selectbackground="#d8e4dd")
        self.text.pack(fill="both", expand=True)

        self._build_buttons()

        if not self.cfg.get("groq_api_key"):
            self._set_status("Click the gear (top right) to add your Groq key", C_RED)
            # First run with no key yet: pop the key entry straight up so
            # there's no hunting for the gear icon.
            self.root.after(300, self._open_settings_first_run)

    # ── Buttons ───────────────────────────────────────────────────────────────
    def _build_buttons(self):
        for child in self.btn_frame.winfo_children():
            child.destroy()

        specs = [("record", "\u25cf  RECORD", C_GREEN, C_GREEN_H, self._on_record)]
        if self.cfg.get("show_copy", True):
            specs.append(("copy", "COPY\nTEXT", C_BLUE, C_BLUE_H, self._on_copy))
        if self.cfg.get("show_read", True):
            specs.append(("read", "READ\nALOUD", C_PURPLE, C_PURPLE_H, self._on_read))
        if self.cfg.get("show_clear", True):
            specs.append(("clear", "START\nOVER", C_GREY, C_GREY_H, self._on_clear))

        cols = 1 if len(specs) == 1 else 2
        for i, (key, label, colour, hover, cmd) in enumerate(specs):
            btn = RoundButton(self.btn_frame, label, colour, hover, cmd, size=self._btn_size)
            r, c = divmod(i, cols)
            btn.grid(row=r, column=c, padx=8, pady=8)
            if key == "record":
                self.record_btn = btn

    def _open_settings(self):
        SettingsWindow(self.root, self.cfg, on_save=self._after_settings)

    def _open_settings_first_run(self):
        SettingsWindow(self.root, self.cfg, on_save=self._after_settings, first_run=True)

    def _after_settings(self):
        self._build_buttons()
        if self.cfg.get("groq_api_key"):
            self._set_status("Ready. Press the green button and start talking", C_GREEN)

    # ── Actions ───────────────────────────────────────────────────────────────
    def _on_record(self):
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        if self._processing:
            return
        if not self.cfg.get("groq_api_key"):
            self._set_status("Please add your Groq key first (gear, top right)", C_RED)
            return
        self._recording = True
        self.recorder.start()
        self.record_btn.set(label="\u25a0  STOP", colour=C_RED, hover=C_RED_H)
        self.record_btn.pulse_start()
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
        threading.Thread(target=self._segmenter_loop, daemon=True).start()

    @safe_thread
    def _segmenter_loop(self):
        rate = self.cfg["sample_rate"]
        fps = rate / 1024.0                       # frames per second (~15.6)
        min_seg = int(fps * 6)                    # don't cut before ~6s
        max_seg = int(fps * 18)                   # force a cut by ~18s
        while self._recording:
            time.sleep(0.4)
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

        @safe_thread
        def work():
            text = transcribe_bytes(raw, self.cfg,
                                    self.cfg["sample_rate"], self.cfg["channels"])
            with self._seg_lock:
                self._seg_results[idx] = text
        t = threading.Thread(target=work, daemon=True)
        t.start()
        self._seg_threads.append(t)

    def _stop_recording(self):
        self._recording = False
        self.recorder.stop()
        self.record_btn.pulse_stop()
        self.record_btn.set(label="\u25cf  RECORD", colour=C_GREEN, hover=C_GREEN_H)

        if self.recorder.duration() < 0.4 or self.recorder.is_mostly_silent():
            self._set_status("I didn't hear anything — please try again", C_MUTED)
            return

        self._processing = True
        self._spin_start(C_BLUE)
        self._set_status("Finishing your words...", C_BLUE)
        threading.Thread(target=self._process, daemon=True).start()

    def _process(self):
        try:
            # Transcribe the final leftover segment (from last cut to the end)
            total = self.recorder.frame_count()
            if total > self._seg_cut:
                self._launch_segment(self._seg_cut, total)

            # Wait for all segment transcriptions to finish
            for t in list(self._seg_threads):
                t.join(timeout=30)

            # Stitch the segments back together in order
            with self._seg_lock:
                parts = [self._seg_results.get(i, "")
                         for i in range(self._seg_index)]
            raw = " ".join(p for p in parts if p).strip()
            raw = re.sub(r"\s{2,}", " ", raw)

            if not raw or is_probable_hallucination(raw):
                self._set_status("I didn't catch any words — please try again", C_MUTED)
                return

            self._spin_start(C_PURPLE)
            self._set_status("Tidying it up...", C_PURPLE)
            cleaned = ai_cleanup(raw, self.cfg)
            if not cleaned.strip() or is_probable_hallucination(cleaned):
                self._set_status("I didn't catch that clearly — please try again", C_MUTED)
                return

            self._append_text(cleaned)

            if self.cfg.get("auto_type", True):
                self._countdown_and_type(cleaned)
            else:
                self._set_status("Done! You can record more, copy, or read it aloud", C_GREEN)
        except Exception as e:
            log_error("_process", e)
            msg = "Something went wrong — please try again"
            if "api" in str(e).lower() or "key" in str(e).lower() or "401" in str(e):
                msg = "There may be a problem with the Groq key (check the gear)"
            self._set_status(msg, C_MUTED)
        finally:
            self._processing = False
            self._spin_stop()

    def _countdown_and_type(self, text):
        """5-second countdown so the user can click where the words should go,
        then the words type themselves at the cursor."""
        for remaining in range(5, 0, -1):
            self._set_status(
                f"Click where the words should go...  typing in {remaining}",
                C_BLUE)
            time.sleep(1)
        self._set_status("Typing your words...", C_BLUE)
        try:
            pyautogui.write(text + " ", interval=0.01)
            self._set_status("Done! Your words have been typed", C_GREEN)
        except Exception as e:
            print(f"Type failed: {e}")
            self._set_status("Couldn't type there — use the COPY button instead", C_MUTED)

    def _on_copy(self):
        text = self._get_text()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._flash("Copied! You can now paste it anywhere", C_BLUE)

    def _on_read(self):
        text = self._get_text()
        if text:
            self.speaker.speak(text)
            self._flash("Reading aloud...", C_PURPLE)
        else:
            self._flash("There is no text to read yet", C_MUTED)

    def _on_clear(self):
        self.speaker.stop()
        self.text.delete("1.0", "end")
        self._set_status("Press the green button and start talking", C_GREEN)

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
        self.root.after(0, lambda: (self.status_var.set(msg),
                                    self.status_lbl.config(fg=colour)))

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


def main():
    if "--setup" in sys.argv:
        setup_wizard()
        return
    try:
        ensure_desktop_shortcut()
        root = tk.Tk()
        SimpleApp(root)
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
