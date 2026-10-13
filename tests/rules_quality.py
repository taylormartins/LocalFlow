"""Suite for the algorithmic (no-LLM) cleanup in rules.py.

Inputs come in two shapes: unpunctuated lowercase (worst case) and the way
Whisper actually writes things, with its own commas and capitals. Most cases
pin the exact output; a rule change that alters any of them should be a
deliberate decision.

    uv run python tests/rules_quality.py

No GPU, no Ollama. Exit code is non-zero on any failure.
"""

import sys

from localflow.rules import rule_clean

# (input, expected output)
EXACT: list[tuple[str, str]] = [
    # --- filler ---------------------------------------------------------
    ("um so I was thinking we could uh add a new enemy type to the second level you know",
     "I was thinking we could add a new enemy type to the second level."),
    ("Um, so, I was thinking we could, uh, add a new enemy type, you know.",
     "I was thinking we could add a new enemy type."),
    ("So, um, I think we should ship it.", "I think we should ship it."),
    ("It's running at like sixty frames per second now.",
     "It's running at sixty frames per second now."),
    ("I'd like two coffees please.", "I'd like two coffees please."),
    ("I like it, you know, a lot.", "I like it, a lot."),
    ("You know, I never liked that design.", "I never liked that design."),
    ("Do you know what I mean?", "Do you know what I mean?"),
    ("Mm-hmm, that works.", "Mm-hmm, that works."),
    # --- stutters -------------------------------------------------------
    ("The bug is in the the collision code.", "The bug is in the collision code."),
    ("I think, I think we should go.", "I think we should go."),
    ("we need to we need to fix this", "We need to fix this."),
    ("W- we should leave.", "We should leave."),
    ("I had had enough.", "I had had enough."),
    ("He said that that was fine.", "He said that that was fine."),
    # --- self-correction ------------------------------------------------
    ("send it Tuesday no wait Wednesday", "Send it Wednesday."),
    ("Send it Tuesday, no wait, Wednesday.", "Send it Wednesday."),
    ("the bug is in the the collision code actually no it's in the movement code",
     "The bug is in the movement code."),
    ("Meet me at five, I mean six.", "Meet me at six."),
    ("What I mean is the menu is slow.", "What I mean is the menu is slow."),
    ("The meeting is on Monday. Scratch that. The meeting is on Tuesday.",
     "The meeting is on Tuesday."),
    ("I actually liked the old version.", "I actually liked the old version."),
    ("The deadline is Friday, actually no, Thursday, so plan for that.",
     "The deadline is Thursday, so plan for that."),
    ("Book the flight for Monday. Actually no, Tuesday.", "Book the flight for Tuesday."),
    ("we should use react no wait vue for this", "We should use vue for this."),
    ("The meeting is at 3 PM, sorry, I mean 4 PM, in room 2.",
     "The meeting is at 4 PM, in room 2."),
    ("Set it to 50 percent, or rather 60 percent.", "Set it to 60 percent."),
    ("Call John, I mean Jon with no h.", "Call Jon with no h."),
    ("the file is in downloads no wait it is in documents", "The file is in documents."),
    ("Can you check whether the server is up, I mean the database server?",
     "Can you check whether the database server is up?"),
    # Whisper's actual output for a synthesized disfluent clip:
    ("Um so I was thinking we could uh send the build on Tuesday no wait Wednesday comma "
     "and then follow up with the the team new paragraph thanks.",
     "I was thinking we could send the build on Wednesday, and then follow up with the team."
     "\n\nThanks."),
    ("Sorry, I mean it.", "Sorry, I mean it."),
    ("That is, is it not, a good idea.", "That is, is it not, a good idea."),
    ("Use a semicolon here.", "Use a semicolon here."),
    ("Put a comma after the name.", "Put a comma after the name."),
    ("uh huh", "Uh huh."),
    ("Okay, sounds good.", "Okay, sounds good."),
    ("No, I don't think so.", "No, I don't think so."),
    # --- spoken commands ------------------------------------------------
    ("send it Tuesday comma then follow up on Friday new paragraph thanks",
     "Send it Tuesday, then follow up on Friday.\n\nThanks."),
    ("Dear Sam, new line, the build is green period", "Dear Sam,\nThe build is green."),
    ("the trial period ends soon", "The trial period ends soon."),
    ("is it ready question mark", "Is it ready?"),
    ("he said open quote ship it close quote", 'He said "ship it"'),
    # --- casing, questions, contractions --------------------------------
    ("can you hear what I'm saying to you", "Can you hear what I'm saying to you?"),
    ("hey can you write me a function that reverses a string",
     "Hey can you write me a function that reverses a string?"),
    ("tell me when the build finishes and then push the branch",
     "Tell me when the build finishes and then push the branch."),
    ("i think im done but i dont know", "I think I'm done but I don't know."),
    ("its fine", "Its fine."),  # "its" is a real word; not guessed
    ("open the settings menu. turn off vsync", "Open the settings menu. Turn off vsync."),
    ("Already clean sentence.", "Already clean sentence."),
    ("", ""),
]

# Glossary: terms spelled as configured.
TERMS = ["VSync", "Unreal Engine", "LocalFlow"]
EXACT_WITH_TERMS: list[tuple[str, str]] = [
    ("turn off vsync in unreal engine", "Turn off VSync in Unreal Engine."),
    ("localflow is running", "LocalFlow is running."),
]

# Never-lose-words: no content word in these may disappear.
KEEP_ALL = [
    "Like I said, the period of testing is over.",
    "I mean it, this time we ship.",
    "We need to talk about the rate limits and the billing period.",
    "No, wait for the build to finish first.",
]


def main() -> int:
    failures = 0
    for cases, terms in ((EXACT, None), (EXACT_WITH_TERMS, TERMS)):
        for raw, want in cases:
            got = rule_clean(raw, terms)
            if got != want:
                failures += 1
                print(f"FAIL  {raw!r}\n      want {want!r}\n      got  {got!r}")
    for raw in KEEP_ALL:
        got = rule_clean(raw).lower()
        lost = [w for w in raw.lower().replace(",", " ").replace(".", " ").split() if w not in got]
        if lost:
            failures += 1
            print(f"FAIL  {raw!r} lost {lost} -> {got!r}")
    # Idempotent: cleaning clean text changes nothing.
    for raw, want in EXACT:
        again = rule_clean(want)
        if again != want:
            failures += 1
            print(f"FAIL  not idempotent: {want!r} -> {again!r}")
    total = len(EXACT) * 2 + len(EXACT_WITH_TERMS) + len(KEEP_ALL)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
