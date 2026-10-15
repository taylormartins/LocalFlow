"""Getting the Whisper model onto the machine, including on locked-down networks.

The normal path downloads once from Hugging Face into the HF cache. Corporate
networks break that in three distinct ways, and each has its own way out:

  * TLS inspection (Zscaler, Netskope, ...): the proxy re-signs traffic with a
    corporate root that lives in the Windows certificate store but not in
    certifi's bundle. The download is retried trusting the OS store
    (truststore), so "allowed but inspected" just works.
  * huggingface.co blocked outright (HTTP 403 from the proxy): download the
    model on another machine (`localflow fetch-model --zip`) and sideload it
    (`localflow import-model`, or `install.cmd -ModelZip`). Sideloaded models
    live in models/<name>/ and are used in preference to the cache.
  * An internal mirror (Artifactory, Nexus): set whisper.hf_endpoint.
  * No other PC to download on: the small CPU models are mirrored on
    LocalFlow's own GitHub (a release, and hidden git refs), tried
    automatically when Hugging Face fails.

Nothing here changes a machine where the plain download already works: the
OS trust store is only tried after a certificate failure.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

MODELS_DIR = PROJECT_ROOT / "models"
HF_DEFAULT = "https://huggingface.co"
OFFLINE_HELP = "see 'Locked-down networks' in the README"


class ModelUnavailable(RuntimeError):
    """The model is not on disk and could not be downloaded. str() is user-facing."""


def apply_endpoint(endpoint: str) -> None:
    """Point huggingface_hub at a mirror. Must run before it is imported."""
    if endpoint:
        os.environ["HF_ENDPOINT"] = endpoint.rstrip("/")


def endpoint() -> str:
    return os.environ.get("HF_ENDPOINT", HF_DEFAULT).rstrip("/")


def repo_id(name: str) -> str:
    if "/" in name:
        return name
    from faster_whisper.utils import _MODELS

    return _MODELS.get(name, name)


def sideloaded(name: str) -> str | None:
    """A model folder to load directly: a path in config, or models/<name>/."""
    for candidate in (Path(name), MODELS_DIR / name):
        try:
            if (candidate / "model.bin").is_file():
                return str(candidate)
        except OSError:
            pass
    return None


_system_certs = False


def use_system_certs() -> None:
    """Trust the Windows certificate store (where IT installs proxy roots)."""
    global _system_certs
    if _system_certs:
        return
    import truststore

    truststore.inject_into_ssl()
    try:
        # huggingface_hub keeps one HTTP client; drop it so the next request
        # builds a fresh one on the new SSL context.
        from huggingface_hub.utils import close_session

        close_session()
    except Exception:
        pass
    _system_certs = True
    log.info("using the Windows certificate store for downloads")


def _is_cert_error(exc: BaseException) -> bool:
    seen = 0
    while exc is not None and seen < 10:
        text = f"{type(exc).__name__} {exc}"
        if "CERTIFICATE_VERIFY_FAILED" in text or "SSLCertVerificationError" in text:
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def download(name: str, output_dir: str | None = None, cache_dir: str | None = None) -> str:
    """Download a model: Hugging Face first, then LocalFlow's copies on GitHub.

    Raises ModelUnavailable with a diagnosis of what blocked it.
    """
    try:
        return _download_hf(name, output_dir, cache_dir)
    except ModelUnavailable as hf_error:
        if output_dir is not None or name not in GITHUB_MODELS:
            raise
        log.warning("%s - trying LocalFlow's own copy instead", str(hf_error).split(" Until it")[0])
        failures = []
        for how, fetch in (("GitHub release", from_github), ("git", from_git)):
            for attempt in (1, 2):  # once more, for a dropped connection
                log.info("trying LocalFlow's own copy of %s (%s)", name, how)
                try:
                    return fetch(name)
                except Exception as exc:
                    log.warning("%s download failed: %s: %s", how, type(exc).__name__, exc)
                    if attempt == 2 or isinstance(exc, ValueError):  # checksum: don't retry
                        failures.append(f"{how}: {type(exc).__name__}: {exc}")
                        break
                    time.sleep(3)
        raise ModelUnavailable(
            f"{hf_error} LocalFlow's own copies failed too ({'; '.join(failures)})."
        ) from None


# Small CPU models re-published as LocalFlow release assets. Networks that
# block huggingface.co (it's an "AI" site to most filters) usually allow
# GitHub, or the clone would have failed. The weights are MIT-licensed; each
# zip carries a NOTICE.txt. The hash pins exactly what was uploaded.
GITHUB_RELEASE = "https://github.com/taylormartins/LocalFlow/releases/download/models-v1"
GITHUB_MODELS: dict[str, tuple[str, int]] = {
    # name: (sha256 of the zip, size in bytes)
    "tiny.en": ("90991c5ccd3804806e27aec848112a83fc144f0a12846fc3407c3f075abacf96", 78092516),
    "base.en": ("6a86c52bbb428e6d315a98597f2c51feea326d4f94fc2bad1771c7bd2d511255", 147771432),
    "small.en": ("3a928b21340d8beb3c05d10d01f7a789112e204775a0e9d0cd63a1395e5f7066", 486100731),
}


def from_github(name: str) -> str:
    """Download a model zip from the release, verify it, install to models/."""
    import hashlib

    import httpx

    size = GITHUB_MODELS[name][1]
    url = f"{GITHUB_RELEASE}/localflow-model-{name}.zip"
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MODELS_DIR / f".{name}.zip.part"
    for attempt in (1, 2):
        try:
            digest = hashlib.sha256()
            got, next_report = 0, 0.1
            with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
                r.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                        digest.update(chunk)
                        got += len(chunk)
                        if got / size >= next_report:
                            log.info("  %s: %3.0f%% of %.0f MB", name, 100 * got / size, size / 1e6)
                            next_report += 0.1
            break
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            if attempt == 1 and _is_cert_error(exc) and not _system_certs:
                use_system_certs()
                continue
            raise
    return _install_verified(tmp, name, digest.hexdigest(), url)


def _install_verified(zip_path: Path, name: str, actual_sha: str, origin: str) -> str:
    try:
        if actual_sha != GITHUB_MODELS[name][0]:
            raise ValueError(f"{origin} failed its checksum - not the file LocalFlow published")
        _, dest = import_model(zip_path, name)
    finally:
        zip_path.unlink(missing_ok=True)
    log.info("installed %s at %s", name, dest)
    return str(dest)


# The same zips, split into <50 MB chunks and committed under hidden refs
# (refs/localflow/models-v1/<name>) in the main repo. A normal clone never
# fetches them. This only needs github.com, the one host `git clone` proved
# reachable; release assets come from release-assets.githubusercontent.com,
# which some filters block separately.
GIT_REMOTE = "https://github.com/taylormartins/LocalFlow.git"
GIT_REF = "refs/localflow/models-v1/{name}"


def from_git(name: str) -> str:
    import hashlib
    import subprocess

    if name not in GITHUB_MODELS:
        raise KeyError(name)
    if not shutil.which("git"):
        raise FileNotFoundError("git is not installed")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    repo = Path(tempfile.mkdtemp(prefix=".git-", dir=MODELS_DIR))
    tmp = MODELS_DIR / f".{name}.zip.part"
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, **flags
        ).stdout

    try:
        git("init", "--bare", "-q")
        log.info("  fetching %s with git (%.0f MB)", name, GITHUB_MODELS[name][1] / 1e6)
        git("fetch", "--depth", "1", "-q", GIT_REMOTE, GIT_REF.format(name=name))
        parts = sorted(git("ls-tree", "--name-only", "FETCH_HEAD").decode().split())
        digest = hashlib.sha256()
        with tmp.open("wb") as fh:
            for part in parts:
                data = git("cat-file", "blob", f"FETCH_HEAD:{part}")
                fh.write(data)
                digest.update(data)
    except subprocess.CalledProcessError as exc:
        tmp.unlink(missing_ok=True)
        detail = (exc.stderr or b"").decode(errors="replace").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else str(exc)) from None
    finally:
        # Git marks pack files read-only, which stops a plain rmtree on Windows.
        shutil.rmtree(repo, onexc=lambda f, path, _: (os.chmod(path, 0o666), f(path)))
    return _install_verified(tmp, name, digest.hexdigest(), f"git {GIT_REF.format(name=name)}")


def _download_hf(name: str, output_dir: str | None, cache_dir: str | None) -> str:
    """Download from Hugging Face, retrying through the OS trust store on a cert failure."""
    import warnings

    from faster_whisper.utils import download_model

    # faster-whisper still passes an argument huggingface_hub has deprecated.
    warnings.filterwarnings("ignore", message=".*local_dir_use_symlinks.*")
    kwargs = {"output_dir": output_dir, "cache_dir": cache_dir or None}
    try:
        return download_model(name, **kwargs)
    except Exception as exc:
        if not (_is_cert_error(exc) and not _system_certs):
            raise ModelUnavailable(_explain(name, exc)) from exc
        log.warning("certificate check failed (%s); retrying with the Windows store", exc)
    use_system_certs()
    try:
        return download_model(name, **kwargs)
    except Exception as exc:
        raise ModelUnavailable(_explain(name, exc)) from exc


def _explain(name: str, exc: BaseException) -> str:
    kind, detail = diagnose(name)
    if kind == "ok":
        # The endpoint answers, so the failure was something else (disk, an
        # unknown model name...). Pass the original error through.
        detail = f"{type(exc).__name__}: {exc}"
    return f"Whisper model '{name}' could not be downloaded: {detail}. Until it is present, transcription is unavailable; {OFFLINE_HELP}."


def diagnose(name: str) -> tuple[str, str]:
    """Probe the model endpoint and say what is in the way.

    Returns (kind, message), kind one of: ok, proxy, cert, http, network, timeout.
    """
    import httpx

    url = f"{endpoint()}/api/models/{repo_id(name)}"
    try:
        r = httpx.get(url, timeout=10)
    except httpx.TimeoutException:
        return "timeout", f"{endpoint()} timed out"
    except Exception as exc:
        if _is_cert_error(exc):
            if not _system_certs:
                use_system_certs()
                return diagnose(name)
            return "cert", (
                f"TLS certificate not trusted for {endpoint()} - a proxy is intercepting "
                "HTTPS and its root certificate is not in the Windows store"
            )
        return "network", f"cannot reach {endpoint()} ({type(exc).__name__}: {exc})"

    server = r.headers.get("server", "")
    if r.status_code == 200:
        return "ok", f"{endpoint()} reachable"
    if r.status_code in (403, 407) or "html" in r.headers.get("content-type", ""):
        who = server or "a network proxy"
        return "proxy", (
            f"blocked by {who} (HTTP {r.status_code}). The connection works, "
            "but the network refuses this site"
        )
    if r.status_code in (401, 404):
        return "http", f"model {repo_id(name)} not found at {endpoint()} (HTTP {r.status_code})"
    return "http", f"HTTP {r.status_code} from {url}"


# -- small CPU models, shared -------------------------------------------------

_cpu_models: dict[str, object] = {}
_cpu_lock = __import__("threading").Lock()


def cpu_whisper(name: str):
    """A small Whisper model on the CPU, loaded once and shared: always-on
    listens with it and the live preview on a GPU-less PC reads with it, so
    the two never cost two copies of the weights."""
    return small_whisper(name, "cpu")[0]


def small_whisper(name: str, device: str = "cpu"):
    """(model, device actually used). On "cuda" it falls back to the CPU if
    the GPU can't take it. CPU: int8, up to 4 threads - it only runs during
    bursts of speech, and more threads there mean a quicker start cue."""
    import os

    with _cpu_lock:
        key = (name, device)
        if key not in _cpu_models:
            from faster_whisper import WhisperModel

            if device == "cuda":
                kw = {"device": "cuda", "compute_type": "float16"}
            else:
                kw = {"device": "cpu", "compute_type": "int8",
                      "cpu_threads": 2}
            try:
                folder = sideloaded(name)
                if folder:
                    model = WhisperModel(folder, **kw)
                else:
                    try:
                        model = WhisperModel(name, local_files_only=True, **kw)
                    except Exception as exc:
                        if device == "cuda" and "local" not in type(exc).__name__.lower():
                            raise
                        model = WhisperModel(download(name), **kw)
            except Exception as exc:
                if device != "cuda":
                    raise
                log.warning("%s on the GPU failed (%s); using the CPU", name, exc)
                _cpu_lock.release()
                try:
                    return small_whisper(name, "cpu")
                finally:
                    _cpu_lock.acquire()
            _cpu_models[key] = model
            log.info("loaded %s on the %s (shared)", name, device.upper())
        return _cpu_models[key], device


# -- sideloading --------------------------------------------------------------

NOTICE = """\
Whisper speech recognition model, packaged for LocalFlow.

Source: https://huggingface.co/{repo} (CTranslate2 conversion for faster-whisper)
Original model: OpenAI Whisper, https://github.com/openai/whisper

Both are released under the MIT License:

Copyright (c) 2022 OpenAI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

def export_zip(name: str, out: Path) -> Path:
    """Download a model and pack it as <name>/... in a zip for another machine."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / name
        download(name, output_dir=str(folder))
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
            for f in sorted(folder.iterdir()):
                if f.is_file():
                    zf.write(f, f"{name}/{f.name}")
            zf.writestr(f"{name}/NOTICE.txt", NOTICE.format(repo=repo_id(name)))
    return out


def _name_from(folder: Path) -> str:
    # A folder saved from the Hugging Face website is usually named after the
    # repo ("faster-whisper-small.en"); LocalFlow's own zips use the bare name.
    return folder.name.removeprefix("faster-whisper-")


def import_model(source: Path, name: str = "") -> tuple[str, Path]:
    """Install a model from a zip or a folder into models/<name>/."""
    if not source.exists():
        raise FileNotFoundError(source)

    with tempfile.TemporaryDirectory() as tmp:
        if source.is_file():
            with zipfile.ZipFile(source) as zf:
                zf.extractall(tmp)
            root = Path(tmp)
        else:
            root = source
        hits = sorted(root.rglob("model.bin"), key=lambda p: len(p.parts))
        if not hits:
            raise ValueError(f"no model.bin in {source} - not a faster-whisper model")
        folder = hits[0].parent
        if not name:
            if source.is_file() and folder == root:  # zip with the files at its top
                name = _name_from(Path(source.stem.removeprefix("localflow-model-")))
            else:
                name = _name_from(folder)
        dest = MODELS_DIR / name
        if dest.resolve() != folder.resolve():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(folder, dest)
    return name, dest
