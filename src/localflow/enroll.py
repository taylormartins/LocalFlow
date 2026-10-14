"""Setup window for always-on phrases: type them, then record each 3 times.

Runs as its own process (`localflow enroll`, or tray > Always-on phrases), so
its Tk window never shares a thread or an interpreter with the tray or the
overlay. Exit code 0 means new phrases were saved.

Recording a take, designed so the take is clean:
  Space (or click) -> amber "Get ready..." for READY_DELAY, during which the
  audio is thrown away: that's where the key/mouse click lands -> green
  "Speak now" -> Space (or click) again -> the last END_TRIM is dropped too
  (the second click), silence is trimmed, and the take is transcribed so you
  see exactly what the listening model heard.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import numpy as np

from .config import Config
from .wake import (
    RATE, TAKES, WAKE_DIR, Detector, Phrase, Phrases, expand, find, make_phrase, normalise,
    read_wav, trim,
)

log = logging.getLogger(__name__)

READY_DELAY = 0.7
END_TRIM = 0.25
MIN_TAKE = 0.4

BG, PANEL, TEXT, DIM = "#1e1f24", "#2a2c33", "#e8e8ea", "#9a9ca5"
AMBER, GREEN, RED, BLUE = "#f0a830", "#3ecf6e", "#e5534b", "#4c8dff"


class Take:
    def __init__(self):
        self.audio: np.ndarray | None = None
        self.heard = ""
        self.score = 0.0
        self.checking = False


class Enroll:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.saved = False
        self.phrases = Phrases.load()
        self.detector = Detector(cfg.always_on.model, Phrases())
        self.takes = {k: [Take() for _ in range(TAKES)] for k in ("start", "stop")}
        self.target = ("start", 0)
        self.state = "idle"  # idle | ready | live | busy
        self._blocks: list[np.ndarray] = []
        self._live_from = 0
        self._stream = None
        self._rate = RATE
        self._load_existing()
        threading.Thread(target=self.detector.load, daemon=True).start()

        try:  # sharp text on a scaled display instead of a blurry bitmap stretch
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = root = tk.Tk()
        root.title("LocalFlow - always-on phrases")
        root.configure(bg=BG)
        root.resizable(False, False)
        font = ("Segoe UI", 10)
        tk.Label(root, text="Always-on phrases", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 14)).pack(anchor="w", padx=18, pady=(14, 0))
        tk.Label(
            root, bg=BG, fg=DIM, font=font, justify="left",
            text="Say the start phrase to begin dictating, the stop phrase to finish. Pick phrases\n"
                 "you'd never say by accident. Record each one 3 times, the way you'll really say it.",
        ).pack(anchor="w", padx=18, pady=(2, 8))

        self.entries: dict[str, tk.Entry] = {}
        self.alts: dict[str, tk.Entry] = {}
        self.slots: dict[str, list[tuple[tk.Canvas, tk.Label]]] = {}
        self.accepted: dict[str, tk.Label] = {}
        for which, title, example in (
            ("start", "Start phrase", "e.g. okay notebook, take this down"),
            ("stop", "Stop phrase", "e.g. that's all for now notebook"),
        ):
            self._phrase_panel(which, title, example, font)

        self.status = tk.Label(root, bg=BG, fg=TEXT, font=("Segoe UI", 11), justify="left")
        self.status.pack(anchor="w", padx=18, pady=(10, 4))
        bar = tk.Frame(root, bg=BG)
        bar.pack(fill="x", padx=18, pady=(4, 16))
        self.button = tk.Button(
            bar, text="Record", width=22, height=2, relief="flat", bg=PANEL, fg=TEXT,
            activebackground=PANEL, font=("Segoe UI Semibold", 11), takefocus=0,
            command=self.press,
        )
        self.button.pack(side="left")
        self.save_btn = tk.Button(bar, text="Save", width=10, relief="flat", bg=BLUE, fg="white",
                                  takefocus=0, command=self.save, font=font)
        self.save_btn.pack(side="right")
        tk.Button(bar, text="Cancel", width=10, relief="flat", bg=PANEL, fg=TEXT, takefocus=0,
                  command=root.destroy, font=font).pack(side="right", padx=8)

        # Space records - unless you're typing in a box.
        root.bind_all("<KeyPress-space>", self._space)
        root.bind_all("<Return>", lambda e: root.focus_set())
        root.bind("<Button-1>", lambda e: root.focus_set() if e.widget is root else None)
        self._next_target()
        self._refresh()
        root.after(50, self._tick)

    # -- layout --------------------------------------------------------------

    def _phrase_panel(self, which: str, title: str, example: str, font) -> None:
        panel = tk.Frame(self.root, bg=PANEL, padx=12, pady=10)
        panel.pack(fill="x", padx=18, pady=6)
        top = tk.Frame(panel, bg=PANEL)
        top.pack(fill="x")
        tk.Label(top, text=title, bg=PANEL, fg=TEXT, width=12, anchor="w",
                 font=("Segoe UI Semibold", 11)).pack(side="left")
        e = tk.Entry(top, width=44, font=("Segoe UI", 11), bg=BG, fg=TEXT,
                     insertbackground=TEXT, relief="flat")
        e.insert(0, getattr(self.phrases, which).text)
        e.pack(side="left", padx=(4, 0), ipady=3)
        e.bind("<KeyRelease>", lambda ev: self._refresh())
        if not e.get():
            tk.Label(top, text=example, bg=PANEL, fg=DIM, font=("Segoe UI", 9)).pack(side="left", padx=8)
        self.entries[which] = e

        alt = tk.Frame(panel, bg=PANEL)
        alt.pack(fill="x", pady=(6, 0))
        tk.Label(alt, text="Other spellings", bg=PANEL, fg=DIM, width=12, anchor="w",
                 font=font).pack(side="left")
        a = tk.Entry(alt, width=44, font=font, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat")
        # Show typed alternatives as the words that differ from the phrase.
        words = normalise(getattr(self.phrases, which).text)
        typed = []
        for v in getattr(self.phrases, which).variants:
            if v in self._heard_variants(which):
                continue
            vw = v.split()
            diff = [w for w, p in zip(vw, words) if w != p] if len(vw) == len(words) else vw
            typed.append(" ".join(diff) or v)
        a.insert(0, ", ".join(dict.fromkeys(typed)))
        a.pack(side="left", padx=(4, 0), ipady=2)
        a.bind("<KeyRelease>", lambda ev: self._refresh())
        tk.Label(alt, text="comma-separated: other spellings of a word in it, or the whole phrase", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=8)
        self.alts[which] = a

        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill="x", pady=(8, 0))
        slots = []
        for i in range(TAKES):
            cell = tk.Frame(row, bg=PANEL)
            cell.pack(side="left", padx=(0, 10))
            c = tk.Canvas(cell, width=170, height=38, bg=BG, highlightthickness=2,
                          highlightbackground=BG, cursor="hand2")
            c.pack()
            c.bind("<Button-1>", lambda ev, w=which, n=i: self._select(w, n))
            lbl = tk.Label(cell, bg=PANEL, fg=DIM, font=("Segoe UI", 9), width=26, anchor="w")
            lbl.pack(anchor="w")
            slots.append((c, lbl))
        self.slots[which] = slots
        acc = tk.Label(panel, bg=PANEL, fg=DIM, font=("Segoe UI", 9), anchor="w", justify="left",
                       wraplength=560)
        acc.pack(fill="x", pady=(6, 0))
        self.accepted[which] = acc

    # -- state ---------------------------------------------------------------

    def _load_existing(self) -> None:
        for which in ("start", "stop"):
            p: Phrase = getattr(self.phrases, which)
            for i, name in enumerate(p.takes[:TAKES]):
                try:
                    t = self.takes[which][i]
                    t.audio = read_wav(WAKE_DIR / name)
                    t.heard = p.heard[i] if i < len(p.heard) else ""
                    t.score = 1.0  # accepted when it was saved
                except Exception:
                    pass

    def _heard_variants(self, which: str) -> set[str]:
        return {" ".join(normalise(t.heard)) for t in self.takes[which] if t.heard}

    def _variants(self, which: str) -> list[list[str]]:
        phrase = self.entries[which].get()
        typed = [normalise(phrase)] + [expand(a, phrase) for a in self.alts[which].get().split(",")]
        return [v for v in typed if v]

    def _select(self, which: str, n: int) -> None:
        if self.state in ("idle",):
            self.target = (which, n)
            self._refresh()

    def _next_target(self) -> None:
        for which in ("start", "stop"):
            for i, t in enumerate(self.takes[which]):
                if t.audio is None:
                    self.target = (which, i)
                    return

    def _complete(self) -> bool:
        if self.state != "idle":
            return False
        return all(
            t.audio is not None and not t.checking for k in ("start", "stop") for t in self.takes[k]
        ) and all(self.entries[k].get().strip() for k in ("start", "stop"))

    # -- recording -----------------------------------------------------------

    def _space(self, event) -> str | None:
        if isinstance(event.widget, tk.Entry):
            return None  # typing a space in a phrase
        self.press()
        return "break"

    def press(self) -> None:
        which, n = self.target
        if self.state == "idle":
            if not self.entries[which].get().strip():
                self._say(f"Type the {which} phrase first.", RED)
                return
            self.root.focus_set()
            self._open()
            self.state = "ready"
            self._ready_at = time.monotonic() + READY_DELAY
        elif self.state == "live":
            self._close_take()
        elif self.state == "ready":
            # Pressed again during "Get ready": treat as a cancel.
            self._close_stream()
            self.state = "idle"
        self._refresh()

    def _open(self) -> None:
        import sounddevice as sd

        from .audio import resolve_candidates

        self._blocks = []
        for device in resolve_candidates(self.cfg.audio.device) or [None]:
            rate = int(device.default_samplerate) if device else RATE
            try:
                self._stream = sd.InputStream(
                    device=device.index if device else None, channels=1, samplerate=rate,
                    dtype="float32", callback=lambda d, *_: self._blocks.append(d[:, 0].copy()),
                )
                self._stream.start()
                self._rate = rate
                return
            except Exception as exc:
                log.warning("could not open %s: %s", device, exc)
        self._say("Could not open the microphone.", RED)

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _close_take(self) -> None:
        self._close_stream()
        audio = np.concatenate(self._blocks) if self._blocks else np.zeros(0, np.float32)
        audio = audio[self._live_from : max(self._live_from, len(audio) - int(END_TRIM * self._rate))]
        if self._rate != RATE and audio.size:
            import soxr

            audio = soxr.resample(audio, self._rate, RATE)
        from .audio import Recorder

        audio, _ = Recorder(self.cfg.audio).apply_gain(audio.astype(np.float32))
        audio = trim(audio)
        which, n = self.target
        if audio.size < MIN_TAKE * RATE or float(np.abs(audio).max(initial=0)) < 0.01:
            self.state = "idle"
            self._say("That take was too short or too quiet - try again (Mic boost helps a quiet mic).", RED)
            return
        take = self.takes[which][n]
        take.audio, take.heard, take.checking = audio, "", True
        self.state = "idle"
        threading.Thread(target=self._check, args=(which, n, audio), daemon=True).start()
        self._next_target()

    def _check(self, which: str, n: int, audio: np.ndarray) -> None:
        take = self.takes[which][n]
        try:
            words = self.detector.words(audio)
            take.heard = " ".join(w.text for w in words)
            m = find(normalise(take.heard), self._variants(which)) if take.heard else None
            take.score = m.score if m else 0.0
        except Exception as exc:
            take.heard, take.score = f"(could not check: {exc})", 0.0
        take.checking = False

    # -- ui loop -------------------------------------------------------------

    def _tick(self) -> None:
        if self.state == "ready" and time.monotonic() >= self._ready_at:
            # Everything before this point is the key/click; start the take here.
            self._live_from = sum(len(b) for b in self._blocks)
            self.state = "live"
            self._refresh()
        elif self.state == "live":
            self._draw_live()
        busy = any(t.checking for k in self.takes for t in self.takes[k])
        if busy or self.state != "idle" or getattr(self, "_was_busy", False):
            now = time.monotonic()
            if now - getattr(self, "_drawn", 0.0) > 0.25:  # canvases are not free
                self._drawn = now
                self._refresh(redraw=self.state != "live")
        self._was_busy = busy
        self.root.after(50, self._tick)

    def _say(self, text: str, colour: str = TEXT) -> None:
        self.status.configure(text=text, fg=colour)

    def _refresh(self, redraw: bool = True) -> None:
        which, n = self.target
        phrase = self.entries[which].get().strip() if hasattr(self, "entries") else ""
        btn = {"idle": ("Record  (Space)", PANEL, TEXT), "ready": ("Get ready...", AMBER, "black"),
               "live": ("Speak now - Space to stop", GREEN, "black")}[self.state if self.state != "busy" else "idle"]
        self.button.configure(text=btn[0], bg=btn[1], fg=btn[2], activebackground=btn[1])
        if self.state == "ready":
            self._say(f"Get ready to say: “{phrase}”", AMBER)
        elif self.state == "live":
            self._say(f"Say it now: “{phrase}”   then press Space", GREEN)
        elif self._complete():
            self._say("All takes recorded. Check what was heard below each take, then Save.\n"
                      "Click a take to select it and record it again.", TEXT)
        else:
            self._say(f"{which.capitalize()} phrase, take {n + 1} of {TAKES}: press Space or click Record.")
        self.save_btn.configure(state="normal" if self._complete() else "disabled")
        if redraw:
            for k in ("start", "stop"):
                for i, (canvas, label) in enumerate(self.slots[k]):
                    self._draw_take(k, i, canvas, label)
                spellings = sorted({" ".join(v) for v in self._variants(k)} | self._heard_variants(k))
                self.accepted[k].configure(
                    text="Accepted spellings: " + ("; ".join(spellings) if spellings else "-"))

    def _draw_take(self, which: str, i: int, c: tk.Canvas, label: tk.Label) -> None:
        take = self.takes[which][i]
        selected = (which, i) == self.target
        c.configure(highlightbackground=BLUE if selected else BG)
        if self.state == "live" and selected:
            return
        c.delete("all")
        if take.audio is None:
            c.create_text(85, 19, text=f"take {i + 1}", fill=DIM, font=("Segoe UI", 9))
            label.configure(text="", fg=DIM)
            return
        self._wave(c, take.audio, GREEN if take.score >= 0.75 else AMBER)
        if take.checking:
            label.configure(text="checking...", fg=DIM)
        elif take.score >= 0.75:
            label.configure(text=f"✓ heard: {take.heard}"[:40], fg=GREEN)
        else:
            label.configure(text=f"⚠ heard: {take.heard or 'nothing'}"[:40], fg=AMBER)

    def _wave(self, c: tk.Canvas, audio: np.ndarray, colour: str) -> None:
        w, h = 170, 38
        if audio.size == 0:
            return
        cols = np.array_split(np.abs(audio), w)
        peak = max(float(np.abs(audio).max()), 1e-4)
        for x, col in enumerate(cols):
            v = float(col.max()) / peak if col.size else 0.0
            c.create_line(x, h / 2 - v * (h / 2 - 2), x, h / 2 + v * (h / 2 - 2), fill=colour)

    def _draw_live(self) -> None:
        which, n = self.target
        c = self.slots[which][n][0]
        blocks = self._blocks
        if not blocks:
            return
        tail = np.concatenate(blocks[-40:])
        c.delete("all")
        self._wave(c, tail, GREEN)

    # -- save ----------------------------------------------------------------

    def save(self) -> None:
        if not self._complete():
            return
        out = Phrases()
        for which in ("start", "stop"):
            takes = self.takes[which]
            setattr(out, which, make_phrase(
                which,
                self.entries[which].get(),
                [v.strip() for v in self.alts[which].get().split(",") if v.strip()],
                [t.audio for t in takes],
                [t.heard for t in takes],
            ))
        out.save()
        self.saved = True
        self.root.destroy()

    def run(self) -> bool:
        self.root.mainloop()
        self._close_stream()
        return self.saved


def main(cfg: Config) -> int:
    return 0 if Enroll(cfg).run() else 1


if __name__ == "__main__":
    from .config import load

    sys.exit(main(load()[0]))
