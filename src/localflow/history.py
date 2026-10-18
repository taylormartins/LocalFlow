"""Dictation history: every dictation saved locally, searchable, re-pastable.

Each finished dictation is appended to history.jsonl next to config.toml
(one JSON object per line, newest last, trimmed to history.max_items). It
never leaves this PC, and it isn't in the log. history.enabled = false
stops the saving; Clear all in the window deletes the file's contents.

The window (`localflow history`, tray ▸ Dictation history..., or "show
history" in command mode) runs as its own process, like the phrase setup.
Pick an entry and press Enter (or double-click) to paste it into the window
you were in before the history opened; Ctrl+C copies it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import PROJECT_ROOT, Config

log = logging.getLogger(__name__)

STORE = PROJECT_ROOT / "history.jsonl"
_lock = threading.Lock()


@dataclass
class Entry:
    at: float
    text: str
    seconds: float = 0.0  # audio length
    source: str = "desktop"


def add(entry: Entry, max_items: int = 500, path: Path | None = None) -> None:
    path = path or STORE
    with _lock:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            # Trim now and then rather than on every write.
            if path.stat().st_size > 400 * max_items:
                entries = load(path)[-max_items:]
                _write(entries, path)
        except OSError as exc:
            log.warning("could not save to history: %s", exc)


def load(path: Path | None = None) -> list[Entry]:
    path = path or STORE
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            d = json.loads(line)
            out.append(Entry(float(d["at"]), str(d["text"]), float(d.get("seconds", 0)),
                             str(d.get("source", "desktop"))))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def _write(entries: list[Entry], path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(asdict(e), ensure_ascii=False) + "\n" for e in entries),
                   encoding="utf-8")
    tmp.replace(path)


def remove(at: float, path: Path | None = None) -> None:
    path = path or STORE
    with _lock:
        _write([e for e in load(path) if e.at != at], path)


def clear(path: Path | None = None) -> None:
    path = path or STORE
    with _lock:
        _write([], path)


def search(entries: list[Entry], query: str) -> list[Entry]:
    """Newest first; every word of the query must appear (any order)."""
    words = query.lower().split()
    hits = [e for e in entries if all(w in e.text.lower() for w in words)]
    return list(reversed(hits))


def when(at: float) -> str:
    t = time.localtime(at)
    today = time.localtime()
    if t[:3] == today[:3]:
        return time.strftime("%H:%M", t)
    if t.tm_year == today.tm_year:
        return time.strftime("%d %b %H:%M", t)
    return time.strftime("%d %b %Y", t)


# -- the window ----------------------------------------------------------------

def main(cfg: Config) -> int:
    import tkinter as tk
    from tkinter import ttk

    from . import window
    from .inject import Injector, set_clipboard_text

    before = window.current()  # where Enter pastes to
    entries = load()
    shown: list[Entry] = []

    from .icon import set_app_identity, window_setup

    set_app_identity()
    root = tk.Tk()
    window_setup(root, "dictation history")
    root.geometry("760x520")
    root.minsize(480, 320)
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72)
    except tk.TclError:
        pass

    top = ttk.Frame(root, padding=(10, 10, 10, 4))
    top.pack(fill="x")
    ttk.Label(top, text="Search").pack(side="left")
    query = tk.StringVar()
    box = ttk.Entry(top, textvariable=query)
    box.pack(side="left", fill="x", expand=True, padx=(8, 0))
    count = ttk.Label(top, text="")
    count.pack(side="left", padx=(8, 0))

    mid = ttk.PanedWindow(root, orient="vertical")
    mid.pack(fill="both", expand=True, padx=10)
    lst_frame = ttk.Frame(mid)
    lst = tk.Listbox(lst_frame, activestyle="none", font=("Segoe UI", 10), exportselection=False)
    sb = ttk.Scrollbar(lst_frame, command=lst.yview)
    lst.config(yscrollcommand=sb.set)
    lst.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    preview = tk.Text(mid, height=7, wrap="word", font=("Segoe UI", 10), relief="flat",
                      background="#f6f6f8")
    mid.add(lst_frame, weight=3)
    mid.add(preview, weight=1)

    bottom = ttk.Frame(root, padding=10)
    bottom.pack(fill="x")
    status = ttk.Label(bottom, text="Enter or double-click: paste  ·  Ctrl+C: copy  ·  Del: delete")
    status.pack(side="left")

    def refresh(*_):
        shown[:] = search(entries, query.get())
        lst.delete(0, "end")
        for e in shown:
            first = " ".join(e.text.split())
            lst.insert("end", f"{when(e.at):>13}   {first[:110]}")
        count.config(text=f"{len(shown)} of {len(entries)}")
        if shown:
            lst.selection_set(0)
            show_preview()
        else:
            preview.delete("1.0", "end")

    def picked() -> Entry | None:
        sel = lst.curselection()
        return shown[sel[0]] if sel else None

    def show_preview(*_):
        e = picked()
        preview.delete("1.0", "end")
        if e:
            preview.insert("1.0", e.text)

    def copy(*_):
        e = picked()
        if e:
            set_clipboard_text(e.text)
            status.config(text="Copied.")

    def paste(*_):
        e = picked()
        if not e:
            return
        if before is None or not window.alive(before):
            set_clipboard_text(e.text)
            status.config(text="The window you were in is gone - copied instead.")
            return
        root.withdraw()
        root.update()
        try:
            if not window.activate(before):
                raise RuntimeError("that window wouldn't come forward")
            time.sleep(0.1)
            Injector(cfg.inject).send(e.text)
        except Exception as exc:
            set_clipboard_text(e.text)
            root.deiconify()
            status.config(text=f"Couldn't paste ({exc}) - copied instead.")
            return
        root.destroy()

    def delete(*_):
        e = picked()
        if e:
            remove(e.at)
            entries[:] = [x for x in entries if x.at != e.at]
            refresh()

    def clear_all():
        from tkinter import messagebox

        if messagebox.askyesno("Clear history", "Delete every saved dictation?", parent=root):
            clear()
            entries.clear()
            refresh()

    ttk.Button(bottom, text="Clear all", command=clear_all).pack(side="right")
    ttk.Button(bottom, text="Copy", command=copy).pack(side="right", padx=6)
    ttk.Button(bottom, text="Paste", command=paste).pack(side="right")

    query.trace_add("write", refresh)
    lst.bind("<<ListboxSelect>>", show_preview)
    lst.bind("<Double-Button-1>", paste)
    for w in (root, lst, box):
        w.bind("<Return>", paste)
    root.bind("<Control-c>", copy)
    lst.bind("<Delete>", delete)
    root.bind("<Escape>", lambda _: root.destroy())
    box.bind("<Down>", lambda _: (lst.focus_set(), "break"))

    refresh()
    if not entries:
        status.config(text="No dictations saved yet." + (
            "" if cfg.history.enabled else " (history.enabled is off in config.toml)"))
    root.after(50, lambda: (root.lift(), root.focus_force(), box.focus_set()))
    root.mainloop()
    return 0
