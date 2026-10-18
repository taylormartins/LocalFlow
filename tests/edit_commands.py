"""Edit commands ("make this shorter", "fix the punctuation") with the
selection, the keys and the LLM faked: no window, no model, any PC.

    uv run python tests/edit_commands.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localflow import commands as cmds
from localflow import desktop, edit
from localflow.commands import BY_NAME, CommandError, Ctx, Grammar, undo_step

failures = 0


def expect(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + (f": {detail}" if not ok else ""))


keys, pasted = [], []
selection = {"text": ""}
desktop.press = lambda chord, times=1: keys.append((chord, times))
desktop.foreground = lambda: 0
edit.copy_selection = lambda in_terminal, timeout=0.5: selection["text"]
cmds._focus_hwnd = lambda hwnd: None


def run(said, llm=None, last=""):
    keys.clear()
    pasted.clear()
    p = Grammar().parse(said.split())
    ctx = Ctx(paste=pasted.append, last_text=last, last_at=time.time(), llm_edit=llm)
    try:
        return p, ctx, p.command.action(p.arg, ctx)
    except CommandError as exc:
        return p, ctx, f"error: {exc}"


print("rules, no LLM")
selection["text"] = "buy milk. call mum. book the dentist."
p, ctx, msg = run("turn this into bullet points")
expect("bullet points", pasted == ["- buy milk.\n- call mum.\n- book the dentist."], f"{pasted} {msg}")
expect("...pasted over the selection (no backspaces)", not any(k == "backspace" for k, _ in keys),
       str(keys))
p, ctx, msg = run("make it uppercase")
expect("uppercase", pasted == ["BUY MILK. CALL MUM. BOOK THE DENTIST."], str(pasted))
selection["text"] = "um so i think we should, uh, go tomorrow"
p, ctx, msg = run("fix the punctuation")
expect("fix the punctuation (rules)", pasted and pasted[0].startswith("I think") and "um" not in pasted[0],
       f"{pasted} {msg}")
p, ctx, msg = run("make this shorter")
expect("an edit rules can't do says it needs the LLM", msg.startswith("error:") and "LLM" in msg
       and not pasted, msg)

print("with the LLM")
seen = []


def llm(text, instruction):
    seen.append((text, instruction))
    return "Let's go tomorrow."


p, ctx, msg = run("make this shorter", llm=llm)
expect("goes to the LLM with the selection", seen[-1] == ("um so i think we should, uh, go tomorrow",
                                                         "make it shorter"), str(seen))
expect("...and its answer is pasted", pasted == ["Let's go tomorrow."], str(pasted))
p, ctx, msg = run("fix the grammar", llm=llm)
expect("'fix the grammar' prefers the LLM when there is one", len(seen) == 2, str(seen))

print("nothing selected: the last dictation")
selection["text"] = ""
p, ctx, msg = run("make it uppercase", last="see you soon")
expect("deletes the last dictation, then pastes the edit",
       keys == [("backspace", 12)] and pasted == ["SEE YOU SOON"], f"{keys} {pasted}")
step = undo_step(p, ctx, 0, 0, 0, None, msg)
keys.clear()
pasted.clear()
step.run()
expect("'undo last command' puts the original back",
       keys == [("backspace", 12)] and pasted == ["see you soon"], f"{keys} {pasted}")
p, ctx, msg = run("make it uppercase")
expect("no selection and no recent dictation: says so", "select some text" in msg, msg)
selection["text"] = "whole line copied by a code editor\n"
p, ctx, msg = run("make it uppercase", last="hi there")
expect("a code editor's copied line counts as nothing selected", pasted == ["HI THERE"], str(pasted))

print("edit mode's own instruction")
selection["text"] = "Bonjour"
p = cmds.Parsed(BY_NAME["edit_said"], "translate it to English", 1.0)
pasted.clear()
p.command.action(p.arg, Ctx(paste=pasted.append, llm_edit=llm))
expect("said word for word", seen[-1] == ("Bonjour", "translate it to English"), str(seen[-1]))

print("\nall passed" if not failures else f"\n{failures} FAILED")
