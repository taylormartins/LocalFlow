"""Make the pip-installed CUDA runtime DLLs visible to CTranslate2.

CTranslate2 links against cuBLAS and cuDNN 9 at load time. On Windows those DLLs
ship inside the ``nvidia-*-cu12`` wheels under ``site-packages/nvidia/*/bin``,
which is not on the DLL search path by default. Importing this module before
``faster_whisper`` registers those directories so the GPU backend loads cleanly
without a system-wide CUDA Toolkit install.

Import for side effects, as early as possible::

    import localflow._cuda  # noqa: F401
    from faster_whisper import WhisperModel
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_registered = False


def _nvidia_bin_dirs() -> list[Path]:
    try:
        import nvidia
    except ImportError:
        return []

    roots = [Path(p) for p in getattr(nvidia, "__path__", [])]
    dirs: list[Path] = []
    for root in roots:
        for pkg in sorted(root.iterdir()) if root.is_dir() else []:
            # Linux wheels use lib/, Windows wheels use bin/.
            for sub in ("bin", "lib"):
                candidate = pkg / sub
                if candidate.is_dir():
                    dirs.append(candidate)
    return dirs


def register() -> list[Path]:
    """Add bundled CUDA DLL directories to the loader search path. Idempotent."""
    global _registered
    dirs = _nvidia_bin_dirs()
    if _registered:
        return dirs

    for d in dirs:
        if sys.platform == "win32":
            # add_dll_directory only affects LoadLibraryEx-style resolution; some
            # transitive loads still consult PATH, so set both.
            os.add_dll_directory(str(d))
        if str(d) not in os.environ.get("PATH", ""):
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")

    _registered = True
    return dirs


register()
