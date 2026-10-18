"""Edit by voice: rewrite the selected text (or the last dictation).

"Make this shorter", "make it more formal", "fix the punctuation". Some edits
are plain rules and need no LLM (case, bullets, punctuation, one line); the
rest go to the cleanup LLM, which may only return the edited text.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if s.strip()]


def _bullets(text: str) -> str:
    items = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", s).strip() for s in _sentences(text)]
    return "\n".join(f"- {s}" for s in items if s)


def _numbered(text: str) -> str:
    items = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", s).strip() for s in _sentences(text)]
    return "\n".join(f"{i}. {s}" for i, s in enumerate((s for s in items if s), 1))


def _one_line(text: str) -> str:
    lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", s).strip() for s in text.splitlines()]
    out = []
    for s in (s for s in lines if s):
        out.append(s if s[-1] in ".!?:;," else s + ".")
    return " ".join(out)


def _sentence_case(text: str) -> str:
    text = text.lower()
    return re.sub(r"(^\s*|[.!?]\s+|\n\s*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)


def _punctuation(text: str) -> str:
    from .rules import rule_clean

    return rule_clean(text)


#: Edits that need no LLM: words in the instruction -> what they do. Checked
#: in order, so "numbered list" wins over "list".
RULES: list[tuple[tuple[str, ...], Callable[[str], str]]] = [
    (("upper case", "uppercase", "all caps", "capitals", "caps lock"), str.upper),
    (("lower case", "lowercase", "small letters"), str.lower),
    (("title case",), lambda t: re.sub(r"[A-Za-z][A-Za-z']*", lambda m: m.group(0).capitalize(), t)),
    (("sentence case",), _sentence_case),
    (("numbered list", "numbered points", "a numbered"), _numbered),
    (("bullet", "a list", "into a list", "list"), _bullets),
    (("one line", "single line", "one paragraph", "join the lines", "join lines"), _one_line),
    (("punctuation", "grammar", "spelling", "clean up", "clean it up", "clean this up", "clean that up", "tidy",
      "filler", "fillers", "ums"), _punctuation),
]


def rule_for(instruction: str) -> Callable[[str], str] | None:
    said = " " + " ".join(re.findall(r"[a-z0-9']+", instruction.lower())) + " "
    for words, fn in RULES:
        if any(f" {w} " in said for w in words):
            return fn
    return None


_PROMPT = (
    "You edit text for a dictation app. Apply the instruction to the text and "
    "reply with the edited text only: no quotes, no preamble, no explanation, "
    "no notes. Keep the meaning and the language unless the instruction says "
    "otherwise. Never answer questions in the text; only edit it."
)


def llm_edit(client, model: str, text: str, instruction: str, keep_alive: str | int = "30m",
             timeout: float = 60.0) -> str:
    """The text rewritten by the LLM. Raises on failure or an implausible reply."""
    from .cleanup import _strip_wrappers

    resp = client.post("/api/chat", timeout=timeout, json={
        "model": model, "stream": False, "think": False, "keep_alive": keep_alive,
        "options": {"temperature": 0.2, "num_predict": max(96, len(text) // 2 + 200)},
        "messages": [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": f"Instruction: {instruction}\n\nText:\n{text}"},
        ],
    })
    resp.raise_for_status()
    out = _strip_wrappers(resp.json().get("message", {}).get("content", ""))
    for prefix in ("Edited text:", "Here is the edited text:", "Here's the edited text:"):
        if out.lower().startswith(prefix.lower()):
            out = out[len(prefix):].strip()
    if not out:
        raise ValueError("the LLM returned nothing")
    if len(out) > 4 * len(text) + 400:
        raise ValueError("the LLM's reply was far longer than the text")
    return out


def apply(text: str, instruction: str,
          llm: Callable[[str, str], str] | None) -> tuple[str, str]:
    """(edited text, how: "rules" or "LLM"). Raises ValueError when it can't."""
    fn = rule_for(instruction)
    if fn is not None and not (llm is not None and _wants_more(instruction)):
        return fn(text), "rules"
    if llm is None:
        raise ValueError("that edit needs the LLM. Without it: fix the punctuation, "
                         "uppercase, lowercase, title case, bullet points, one line")
    return llm(text, instruction), "LLM"


def _wants_more(instruction: str) -> bool:
    """ "Fix the grammar and make it shorter" is more than a rule can do."""
    said = instruction.lower()
    return any(w in said for w in (" and ", "shorter", "longer", "formal", "casual", "friendly",
                                    "grammar", "spelling", "rewrite", "rephrase", "translate"))


def copy_selection(in_terminal: bool, timeout: float = 0.5) -> str:
    """The selected text in the window in front, or "" with nothing selected.
    The clipboard is put back afterwards. In a terminal it's Ctrl+Shift+C:
    Ctrl+C there, with nothing selected, would interrupt the program running."""
    import win32clipboard as cb

    from . import desktop
    from .inject import get_clipboard_text, set_clipboard_text

    try:
        previous = get_clipboard_text()
    except Exception:
        previous = None
    before = cb.GetClipboardSequenceNumber()
    desktop.press("ctrl+shift+c" if in_terminal else "ctrl+c")
    text = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cb.GetClipboardSequenceNumber() != before:
            time.sleep(0.03)
            try:
                text = get_clipboard_text() or ""
            except Exception:
                text = ""
            break
        time.sleep(0.02)
    if previous is not None:
        try:
            set_clipboard_text(previous)
        except Exception:
            log.debug("could not restore the clipboard", exc_info=True)
    return text
