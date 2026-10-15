"""Voice commands: what can be said, how it's understood, what it does.

Understanding is algorithmic first. Every command is a handful of spoken
patterns ("switch to {name}", "minimize [the] [window]"), matched word by
word with the same fuzzy alignment as the always-on phrases, so a slightly
misheard word still counts but a different sentence doesn't. Nothing here
needs an LLM.

When the cleanup LLM is available, it is an optional second layer in command
mode: a request the patterns don't cover ("put Spotify away", "reply that I'm
running late") is turned into steps chosen from this same list, and only from
it. The LLM can't invent an action, and a plan that closes or sends
something waits for a spoken "yes" first.

Commands run on one worker thread, in order: UI Automation is COM, and
COM objects belong to the thread that made them.
"""

from __future__ import annotations

import itertools
import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import desktop
from .wake import _aligned, normalise

log = logging.getLogger(__name__)

#: Every fixed word of a pattern must be at least this close to what was heard.
ACCEPT = 0.8
#: A spoken name must fit a window, app or button at least this well.
NAME_ACCEPT = 0.72
#: Said around a command without changing it.
LEAD = {"okay", "ok", "please", "now", "and", "then", "so", "um", "uh", "hey", "can", "you",
        "could", "would", "just"}
TRAIL = {"please", "thanks", "now", "for", "me"}
#: What Whisper "hears" in a click, a cue or a breath: never a command.
NOISE = {"you", "thank you", "thanks", "bye", "okay", "thank you for watching", "so", "uh", "um"}


class CommandError(Exception):
    """A command that was understood but couldn't be carried out."""


@dataclass
class Ctx:
    """What an action may touch, supplied by the listener."""
    #: Text LocalFlow pasted last, when, and into which window (scratch that).
    last_text: str = ""
    last_at: float = 0.0
    last_target: object = None
    paste: Callable[[str], None] | None = None
    in_target: Callable[[object, Callable[[], None]], None] | None = None


@dataclass
class Command:
    name: str
    patterns: list[str]
    #: action(arg, ctx) -> what happened, for the pill ("Switched to Chrome").
    action: Callable[[str, Ctx], str]
    help: str
    #: Acts on the window just dictated into: run there after a dictation.
    keys: bool = False
    #: Closes or sends something: an LLM plan with this waits for "yes".
    risky: bool = False
    #: The slot is free text (typed as is), not a name to look up.
    text_slot: bool = False


@dataclass
class Parsed:
    command: Command
    arg: str
    score: float

    def __str__(self) -> str:
        return f"{self.command.name}({self.arg})" if self.arg else self.command.name


# -- actions ---------------------------------------------------------------

def _keys(chord: str, label: str, times: int = 1, delay: float = 0.0
          ) -> Callable[[str, Ctx], str]:
    def run(arg: str, ctx: Ctx) -> str:
        if delay:
            time.sleep(delay)
        desktop.press(chord, times)
        return label
    return run


def _scratch(arg: str, ctx: Ctx) -> str:
    if not ctx.last_text or time.time() - ctx.last_at > 120:
        raise CommandError("nothing recent to scratch")
    n = len(ctx.last_text.replace("\r\n", "\n"))
    text, ctx.last_text = ctx.last_text, ""

    def wipe() -> None:
        desktop.press("backspace", n)

    if ctx.in_target is not None:
        ctx.in_target(ctx.last_target, wipe)
    else:
        wipe()
    return f"Deleted {len(text.split())} words"


def _switch(arg: str, ctx: Ctx) -> str:
    w, score = desktop.find_window(arg)
    if w is None or score < NAME_ACCEPT:
        raise CommandError(f"no window called {arg}")
    if not desktop.focus(w):
        raise CommandError(f"{w.label} wouldn't come forward")
    return f"Switched to {_shown(w, arg)}"


def _open(arg: str, ctx: Ctx) -> str:
    site = re.fullmatch(r"(.+?) ?(?:dot|\.) ?(com|org|net|io|co uk|dev|ai|gov|edu)", arg)
    if site:
        domain = site.group(1).replace(" ", "") + "." + site.group(2).replace(" ", ".")
        desktop.open_url("https://" + domain)
        return f"Opened {domain}"
    w, wscore = desktop.find_window(arg)
    app, ascore = desktop.find_app(arg)
    if w is not None and wscore >= max(NAME_ACCEPT, ascore - 0.05):
        if desktop.focus(w):
            return f"Switched to {_shown(w, arg)}"
    if app is None or ascore < NAME_ACCEPT:
        raise CommandError(f"no app called {arg}")
    before = desktop.foreground()
    desktop.launch(app)
    # Wait for it to come up, so a following step ("...and type hello")
    # lands in the new window rather than the old one.
    deadline = time.monotonic() + 6.0
    while desktop.foreground() == before and time.monotonic() < deadline:
        time.sleep(0.1)
    return f"Opened {app.name}"


def _window_op(how: int, label: str) -> Callable[[str, Ctx], str]:
    def run(arg: str, ctx: Ctx) -> str:
        if arg:
            w, score = desktop.find_window(arg)
            if w is None or score < NAME_ACCEPT:
                raise CommandError(f"no window called {arg}")
            hwnd, what = w.hwnd, _shown(w, arg)
        else:
            hwnd, what = desktop.foreground(), "window"
        if not hwnd:
            raise CommandError("no window in front")
        if how == -1:
            desktop.close(hwnd)
        else:
            desktop.show(hwnd, how)
        return f"{label} {what}"
    return run


def _click(arg: str, ctx: Ctx) -> str:
    hwnd = desktop.foreground()
    if not hwnd:
        raise CommandError("no window in front")
    items = desktop.controls(hwnd)
    c, score = desktop.find_control(arg, items)
    if c is None or score < NAME_ACCEPT:
        raise CommandError(f"no button called {arg}")
    desktop.activate_control(c)
    return f"Clicked {c.name[:30]}"


def _type(arg: str, ctx: Ctx) -> str:
    if ctx.paste is None:
        raise CommandError("can't type here")
    ctx.paste(arg)
    return f"Typed {len(arg.split())} words"


def _scroll(notches: int, label: str) -> Callable[[str, Ctx], str]:
    def run(arg: str, ctx: Ctx) -> str:
        desktop.scroll(notches)
        return label
    return run


def _shown(w: desktop.Win, spoken: str = "") -> str:
    """Name for the pill: the program's, if that's what was matched, else
    what was said (found in the title). Store apps live in a frame host
    whose title is the app's name."""
    if w.exe == "applicationframehost":
        return w.title[:30]
    if spoken and desktop.name_score(spoken, w.names()) < NAME_ACCEPT:
        return spoken
    return w.label


def _help(arg: str, ctx: Ctx) -> str:
    return "Say: switch to..., open..., click..., scroll down, new tab, minimize..."


# Patterns: [optional] words, (a|b) choices, and {slot} at the end.
COMMANDS: list[Command] = [
    # Keys, aimed at the window just dictated into when said after a dictation.
    Command("submit", ["submit", "summit", "send it", "send", "press enter", "hit enter",
                       "enter"],
            # A moment for the app to take in a paste first: Ctrl+V is handled
            # asynchronously, and an Enter overtaking it sends nothing.
            _keys("enter", "Enter", delay=0.15), "press Enter", keys=True, risky=True),
    Command("new_line", ["new line", "next line", "line break"], _keys("shift+enter", "New line"),
            "new line without sending (Shift+Enter)", keys=True),
    Command("scratch", ["scratch that", "delete that", "undo that", "strike that"], _scratch,
            "delete the text LocalFlow just pasted", keys=True),
    Command("tab", ["[press] tab"], _keys("tab", "Tab"), "press Tab", keys=True),
    Command("shift_tab", ["[press] shift tab"], _keys("shift+tab", "Shift+Tab"),
            "press Shift+Tab", keys=True),
    Command("escape", ["[press] escape", "cancel that"], _keys("escape", "Escape"),
            "press Escape", keys=True),
    Command("backspace", ["[press] backspace"], _keys("backspace", "Backspace"),
            "press Backspace", keys=True),
    Command("delete_word", ["delete (word|last word)"], _keys("ctrl+backspace", "Deleted word"),
            "delete the word before the cursor", keys=True),
    Command("undo", ["undo"], _keys("ctrl+z", "Undo"), "undo (Ctrl+Z)", keys=True),
    Command("redo", ["redo"], _keys("ctrl+y", "Redo"), "redo (Ctrl+Y)", keys=True),
    Command("select_all", ["select all"], _keys("ctrl+a", "Selected all"), "select all",
            keys=True),
    Command("copy", ["copy [that]"], _keys("ctrl+c", "Copied"), "copy", keys=True),
    Command("cut", ["cut [that]"], _keys("ctrl+x", "Cut"), "cut", keys=True),
    Command("paste", ["paste [that]"], _keys("ctrl+v", "Pasted"), "paste", keys=True),
    Command("save", ["save [that|this|it|file]"], _keys("ctrl+s", "Saved"), "save (Ctrl+S)",
            keys=True),
    Command("find", ["find"], _keys("ctrl+f", "Find"), "open Find (Ctrl+F)"),
    # Moving around.
    Command("scroll_down", ["scroll down [a bit|a little|more]"], _scroll(-5, "Scrolled down"), "scroll down"),
    Command("scroll_up", ["scroll up [a bit|a little|more]"], _scroll(5, "Scrolled up"), "scroll up"),
    Command("page_down", ["page down"], _keys("pagedown", "Page down"), "page down"),
    Command("page_up", ["page up"], _keys("pageup", "Page up"), "page up"),
    Command("top", ["(go to|jump to) [the] top"], _keys("ctrl+home", "Top"), "go to the top"),
    Command("bottom", ["(go to|jump to) [the] bottom"], _keys("ctrl+end", "Bottom"),
            "go to the bottom"),
    # Tabs and pages.
    Command("new_tab", ["new tab", "open [a] new tab"], _keys("ctrl+t", "New tab"),
            "open a new tab"),
    Command("close_tab", ["close [the|this] tab"], _keys("ctrl+w", "Closed tab"), "close the tab",
            risky=True),
    Command("next_tab", ["next tab"], _keys("ctrl+tab", "Next tab"), "next tab"),
    Command("previous_tab", ["(previous|last) tab"], _keys("ctrl+shift+tab", "Previous tab"),
            "previous tab"),
    Command("reopen_tab", ["reopen [the] tab", "undo close tab"], _keys("ctrl+shift+t",
            "Reopened tab"), "reopen the last closed tab"),
    Command("back", ["go back"], _keys("alt+left", "Back"), "go back a page"),
    Command("forward", ["go forward"], _keys("alt+right", "Forward"), "go forward a page"),
    Command("refresh", ["refresh [the] [page]", "reload [the] [page]"], _keys("f5", "Refreshed"),
            "reload the page"),
    # Windows and apps.
    Command("switch", ["switch to {name}", "go to {name}", "show [me] {name}",
                       "bring up {name}", "focus {name}"], _switch,
            "bring an open window to the front, by app name or title"),
    Command("open", ["open {name}", "launch {name}", "start {name}"], _open,
            "open an app (or switch to it if it's open), or a website like 'youtube dot com'"),
    Command("minimize", ["minimize [the] [window]", "minimize {name}", "hide {name}"],
            _window_op(desktop.SW_MINIMIZE, "Minimized"), "minimize a window"),
    Command("maximize", ["maximize [the] [window]", "maximize {name}"],
            _window_op(desktop.SW_MAXIMIZE, "Maximized"), "maximize a window"),
    Command("restore", ["restore [the] [window]", "restore {name}"],
            _window_op(desktop.SW_RESTORE, "Restored"), "restore a window"),
    Command("close_window", ["close [the|this] window", "close {name}"],
            _window_op(-1, "Closed"), "close a window (it may ask to save)", risky=True),
    Command("switch_window", ["switch (window|windows)", "last window", "previous window"],
            _keys("alt+tab", "Switched window"), "go to the previous window (Alt+Tab)"),
    Command("desktop", ["show [the] desktop"], _keys("win+d", "Desktop"), "show the desktop"),
    Command("snap_left", ["snap [window] left", "snap [it] left"], _keys("win+left",
            "Snapped left"), "snap the window to the left half"),
    Command("snap_right", ["snap [window] right", "snap [it] right"], _keys("win+right",
            "Snapped right"), "snap the window to the right half"),
    # Buttons and text.
    Command("click", ["click [on] {name}", "press {name}", "tap {name}"], _click,
            "click a button, link, tab or menu item by its name"),
    Command("type", ["type {text}"], _type, "type some text", text_slot=True),
    Command("help", ["what can i say", "help", "show commands"], _help, "list commands"),
]
BY_NAME = {c.name: c for c in COMMANDS}

#: Said in command mode to leave it (the stop phrase works too).
EXIT = ["exit command mode", "exit commands", "stop commands", "leave command mode",
        "exit", "done", "that is all", "go to sleep"]
#: Said after the start phrase (or after a dictation) to enter command mode.
ENTER = ["command mode", "commands", "take commands", "control mode"]
#: Answers to "say yes to ..." for a risky plan.
YES = ["yes", "yes please", "yeah", "do it", "confirm", "go ahead", "okay do it"]
NO = ["no", "cancel", "do not", "stop", "never mind", "nope"]


def _expand(pattern: str) -> list[tuple[list[str], str | None]]:
    """"minimize [the] [window]" -> every word list it allows, plus its slot."""
    slot = None
    m = re.search(r"\{(\w+)\}\s*$", pattern)
    if m:
        slot, pattern = m.group(1), pattern[: m.start()]
    parts = re.findall(r"\[[^\]]*\]|\([^)]*\)|[^\s\[\]()]+", pattern)
    options = []
    for p in parts:
        if p.startswith("["):
            options.append([""] + p[1:-1].split("|"))
        elif p.startswith("("):
            options.append(p[1:-1].split("|"))
        else:
            options.append([p])
    out = []
    for combo in itertools.product(*options):
        words = normalise(" ".join(c for c in combo if c))
        if words:
            out.append((words, slot))
    return out


@dataclass
class Grammar:
    """Spoken patterns -> commands. Extra spellings come from the config."""
    extra: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.forms: list[tuple[Command, list[str], str | None]] = []
        for c in COMMANDS:
            for p in c.patterns + self.extra.get(c.name, []):
                for words, slot in _expand(p):
                    self.forms.append((c, words, slot))
        self.enter = [normalise(s) for s in ENTER]
        self.exit = [normalise(s) for s in EXIT]
        self.yes = [normalise(s) for s in YES]
        self.no = [normalise(s) for s in NO]

    def parse(self, words: list[str]) -> Parsed | None:
        """The command these words are, if they are one in full.

        The whole utterance has to be the command, give or take a lead-in
        ("okay", "please") and a slot's free words: "switch to Chrome" is a
        command, "I'll switch to Chrome later" is not.
        """
        words = trim(words)
        if not words:
            return None
        best: Parsed | None = None
        for c, fixed, slot in self.forms:
            n = len(fixed)
            if slot is None:
                if abs(len(words) - n) > 1:
                    continue
                score = _aligned(words, fixed)
                arg = ""
            else:
                score, arg = 0.0, ""
                for k in (n - 1, n, n + 1):
                    if 1 <= k < len(words):
                        s = _aligned(words[:k], fixed)
                        if s > score:
                            rest = words[k:]
                            if c.text_slot is False and len(rest) > 1 and rest[0] in ("the", "my", "a"):
                                rest = rest[1:]
                            score, arg = s, " ".join(rest)
            if score >= ACCEPT and (best is None or score > best.score
                                    or (score == best.score and not arg and best.arg)):
                best = Parsed(c, arg, score)
        return best

    def is_one(self, words: list[str], forms: list[list[str]]) -> bool:
        words = trim(words)
        return bool(words) and any(
            abs(len(words) - len(f)) <= 1 and _aligned(words, f) >= ACCEPT for f in forms)


def trim(words: list[str]) -> list[str]:
    words = list(words)
    while words and words[0] in LEAD:
        words.pop(0)
    while len(words) > 1 and words[-1] in TRAIL:
        words.pop()
    return words


def is_noise(words: list[str]) -> bool:
    return not words or " ".join(words) in NOISE


def resolvable(p: Parsed) -> bool:
    """Whether a command's target exists, without doing anything: lets a
    dictation that merely starts like a command ("open the file and...")
    stay a dictation."""
    name = p.command.name
    if name in ("switch", "minimize", "maximize", "restore", "close_window") and p.arg:
        _, score = desktop.find_window(p.arg)
        return score >= NAME_ACCEPT
    if name == "open":
        _, a = desktop.find_app(p.arg)
        _, w = desktop.find_window(p.arg)
        return max(a, w) >= NAME_ACCEPT
    return name not in ("click", "type")


def describe(p: Parsed) -> str:
    """For the pill: "close window chrome", "type I'm running late"."""
    return f"{p.command.name.replace('_', ' ')} {p.arg}".strip()


# -- the LLM layer -----------------------------------------------------------

_PROMPT = """You turn a spoken request into steps for a Windows voice-control tool.
Reply with JSON only, in this shape: {{"steps": [{{"command": "<name>", "arg": "<text>"}}]}}
Use only these commands (arg is "" unless the command takes one):
{commands}
Windows open now (program: title):
{windows}
Rules: at most 4 steps. Use the exact command names above. For "switch", "open",
"minimize", "maximize", "restore", "close_window" the arg is an app name or a
window title from the list. For "click" the arg is the visible name of a
button. For "type" the arg is the exact text to type. If the request can't be
done with these commands, reply {{"steps": []}}."""


def _describe() -> str:
    lines = []
    for c in COMMANDS:
        if c.name == "help":
            continue
        takes = "arg: text" if c.text_slot else ("arg: name" if any("{" in p for p in c.patterns)
                                                 else "")
        lines.append(f"- {c.name}: {c.help}" + (f" ({takes})" if takes else ""))
    return "\n".join(lines)


def plan_with_llm(client, model: str, request: str, timeout: float = 15.0) -> list[Parsed]:
    """Steps for a request the patterns didn't cover, chosen by the LLM from
    COMMANDS only. Anything unknown or malformed is dropped, not guessed at."""
    wins = desktop.windows()[:20]
    window_list = "\n".join(f"- {w.exe}: {w.title[:60]}" for w in wins) or "- (none)"
    resp = client.post(
        "/api/chat",
        json={
            "model": model, "stream": False, "format": "json", "think": False,
            "options": {"temperature": 0.0, "num_predict": 200},
            "messages": [
                {"role": "system", "content": _PROMPT.format(commands=_describe(),
                                                             windows=window_list)},
                {"role": "user", "content": request},
            ],
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    content = resp.json().get("message", {}).get("content", "")
    try:
        steps = json.loads(content).get("steps", [])
    except (ValueError, AttributeError):
        log.info("commands: LLM reply wasn't JSON")
        return []
    out = []
    for step in steps[:4] if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        c = BY_NAME.get(str(step.get("command", "")))
        arg = str(step.get("arg", "") or "").strip()
        takes_arg = c is not None and any("{" in p for p in c.patterns)
        if c is None or c.name == "help" or (takes_arg and not arg and c.name in
                                              ("switch", "open", "click", "type")):
            log.info("commands: LLM step %r dropped", step.get("command"))
            continue
        out.append(Parsed(c, arg if takes_arg else "", 1.0))
    return out


# -- running them ------------------------------------------------------------

class Runner:
    """One worker thread runs every command, in order (COM likes one thread)."""

    def __init__(self, on_done: Callable[[Parsed, str, bool], None]):
        self.on_done = on_done
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def submit(self, p: Parsed, ctx: Ctx, wrap: Callable[[Callable[[], None]], None] | None = None
               ) -> None:
        """Run ``p`` soon. ``wrap`` runs it inside something (the target window)."""
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="localflow-commands")
            self._thread.start()
        self._jobs.put((p, ctx, wrap))

    def _loop(self) -> None:
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass
        while True:
            p, ctx, wrap = self._jobs.get()
            result: list[str] = []
            try:
                def go() -> None:
                    result.append(p.command.action(p.arg, ctx))

                if wrap is not None:
                    wrap(go)
                else:
                    go()
                log.info("commands: %s done", p.command.name)
                self.on_done(p, result[0] if result else "", True)
            except CommandError as exc:
                log.info("commands: %s not done: %s", p.command.name, exc)
                self.on_done(p, str(exc)[:1].upper() + str(exc)[1:], False)
            except Exception as exc:
                log.warning("commands: %s failed: %s", p.command.name, exc)
                self.on_done(p, f"{p.command.name} failed", False)
