"""The Ollama watcher against a fake Ollama: up, down, hung, model missing,
and the cleaner skipping the LLM while it's out. No real Ollama is touched.

    uv run python tests/ollama_health.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import httpx

from localflow.cleanup import Cleaner
from localflow.config import CleanupConfig
from localflow.health import DOWN, HUNG, MISSING, OK, OllamaHealth

failures = 0


def expect(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + (f": {detail}" if not ok else ""))


fake = {"up": True, "models": ["qwen2.5:7b"], "generates": True}
calls = []


def handle(request: httpx.Request) -> httpx.Response:
    calls.append(request.url.path)
    if not fake["up"]:
        raise httpx.ConnectError("refused")
    if request.url.path == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": n} for n in fake["models"]]})
    if not fake["generates"]:
        raise httpx.ReadTimeout("timed out")
    if request.url.path == "/api/chat":
        return httpx.Response(200, json={"message": {"content": "Hello there, how are you?"}})
    return httpx.Response(200, json={"response": "ok"})


cfg = CleanupConfig()
cfg.method, cfg.model = "llm", "qwen2.5:7b"
cfg.skip_if_clean = False
cleaner = Cleaner(cfg)
cleaner._client = httpx.Client(base_url="http://fake", transport=httpx.MockTransport(handle))
changes = []
health = OllamaHealth(cleaner, on_change=lambda s, m: changes.append((s, m)))

print("states")
expect("up and generating: ok", health.check() == OK and not changes)
fake["up"] = False
expect("not running: down", health.check() == DOWN and cleaner.llm_down)
expect("...said once, with how to fix it", len(changes) == 1 and "Restart Ollama" in changes[0][1],
       str(changes))
health.check()
expect("...and not said again while it stays down", len(changes) == 1)
fake["up"] = True
expect("back: ok, and said so", health.check() == OK and not cleaner.llm_down
       and changes[-1][0] == OK, str(changes))
fake["models"] = []
expect("model gone (drive unplugged): missing", health.check() == MISSING)
fake["models"] = ["qwen2.5:7b:latest"]
health.check()
fake["generates"] = False
expect("answers /api/tags but won't generate: hung", health.check() == HUNG)
expect("a routine (light) check doesn't generate", health.probe(full=False) == OK)

print("the cleaner while it's out")
calls.clear()
t = time.perf_counter()
out = cleaner.clean("um so hello there how are you")
expect("skips the LLM: no call, rules instead", "/api/chat" not in calls and out
       and cleaner.last_outcome == "failed", f"{calls} {out!r}")
expect("...instantly", time.perf_counter() - t < 0.5)
fake["generates"] = True
health.check()
woke = []
health.suspect = lambda: woke.append(1)
cleaner.on_failure = health.suspect
fake["generates"] = False
cleaner.clean("um so hello there how are you")
expect("a failed cleanup wakes the watcher", woke == [1], str(woke))

print("orphaned model runners (they kept 5-6 GB each after the server went)")
from localflow.health import orphaned_runners  # noqa: E402

home = Path("C:/Users/me/AppData/Local/Programs/Ollama")
paths = {10: home / "lib/ollama/llama-server.exe", 11: home / "lib/ollama/llama-server.exe",
         12: Path("C:/Tools/llama/llama-server.exe"), 13: home / "lib/ollama/llama-server.exe"}
procs = [(5, 1, "ollama.exe"), (10, 5, "llama-server.exe"),   # the live server's runner
         (11, 999, "llama-server.exe"),                       # its server is gone
         (12, 999, "llama-server.exe"),                       # someone else's llama-server
         (13, 7, "llama-server.exe"), (7, 1, "explorer.exe")]  # parent PID reused
got = orphaned_runners(procs, lambda pid: str(paths.get(pid, "")), home)
expect("only Ollama's runners whose server is gone", got == [11, 13], str(got))
expect("none when every runner has its server",
       orphaned_runners(procs[:2], lambda pid: str(paths[pid]), home) == [])

print("\nall passed" if not failures else f"\n{failures} FAILED")
