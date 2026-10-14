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

Nothing here changes a machine where the plain download already works: the
OS trust store is only tried after a certificate failure.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
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
    """Download a model, retrying through the OS trust store on a cert failure.

    Raises ModelUnavailable with a diagnosis of what blocked it.
    """
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


# -- sideloading --------------------------------------------------------------

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
