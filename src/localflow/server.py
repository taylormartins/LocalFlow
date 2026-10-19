"""HTTP API + PWA host for phone clients.

Note on HTTPS: iOS Safari only grants microphone access in a secure context.
Plain http://<lan-ip>:8123 will load the page but ``getUserMedia`` will be
undefined. Front this with ``tailscale serve`` (see README) to get a trusted
HTTPS origin without managing certificates.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .engine import Engine

log = logging.getLogger(__name__)

PWA_DIR = Path(__file__).parent / "pwa"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def create_app(engine: Engine) -> FastAPI:
    cfg = engine.cfg.server
    from . import APP_NAME

    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)

    def require_token(
        authorization: str | None = Header(default=None),
        x_localflow_token: str | None = Header(default=None),
    ) -> None:
        if not cfg.token:
            return
        supplied = x_localflow_token
        if not supplied and authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        # Constant-time compare so the token cannot be recovered by timing.
        if not supplied or not secrets.compare_digest(supplied, cfg.token):
            raise HTTPException(status_code=401, detail="invalid or missing token")

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "state": engine.state.value,
            "whisper_model": engine.cfg.whisper.model,
            "whisper_device": engine.transcriber.device_in_use,
            "whisper_loaded": engine.transcriber.loaded,
            "cleanup_enabled": engine.cfg.cleanup.enabled,
            "cleanup_model": engine.cfg.cleanup.model,
            "auth_required": bool(cfg.token),
        }

    @app.post("/api/transcribe", dependencies=[Depends(require_token)])
    async def transcribe(
        audio: UploadFile = File(...),
        cleanup: bool = Form(default=True),
    ) -> JSONResponse:
        data = await audio.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty upload")
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="recording too large")

        import anyio

        try:
            # The pipeline is blocking and GPU-bound; keep the event loop free.
            result = await anyio.to_thread.run_sync(
                lambda: engine.process_upload(data, cleanup=cleanup, source="phone")
            )
        except Exception as exc:
            log.exception("phone transcription failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return JSONResponse(
            {
                "text": result.text,
                "raw": result.raw,
                "audio_seconds": round(result.audio_seconds, 2),
                "transcribe_seconds": round(result.transcribe_seconds, 2),
                "cleanup_seconds": round(result.cleanup_seconds, 2),
            }
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(PWA_DIR / "index.html")

    if PWA_DIR.is_dir():
        app.mount("/", StaticFiles(directory=PWA_DIR, html=True), name="pwa")

    return app


def serve(engine: Engine) -> None:
    """Run uvicorn in the foreground. Call from a worker thread in the tray app."""
    import uvicorn

    cfg = engine.cfg.server
    uvicorn.run(
        create_app(engine),
        host=cfg.host,
        port=cfg.port,
        log_level="warning",
        access_log=False,
    )
