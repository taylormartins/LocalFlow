"""Camera mode, part one: attention. Which screen you're looking at, if any.

One or more webcams watch your face (MediaPipe's face landmarker, on the
CPU). Head pose plus where the irises sit in the eyes gives a gaze
direction for each camera. Calibration (look at each screen in turn) records
that direction per screen per camera, so later the nearest one says which
screen you're looking at. With a camera on each screen, looking left or
right between screens is clear; with one camera, screens far apart still
are.

What uses it (both switchable, see CameraConfig):
- the start phrase counts only while you're looking at a screen, so the TV
  or someone in the room can't start a dictation;
- command mode doesn't time out while you're looking at a screen.

Frames never leave this module: each is read, turned into a few numbers and
dropped. Nothing is saved but the calibration (directions, no images).
See docs/CAMERA_MODE.md for the rest of the plan.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

DATA = PROJECT_ROOT / "data"
FACE_MODEL = DATA / "face_landmarker.task"
FACE_MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
                  "face_landmarker/float16/1/face_landmarker.task")
CALIBRATION = DATA / "camera_calibration.json"
#: While this file exists (calibration is running in its own process), the
#: watcher lets go of the cameras: a webcam can only be open once.
LOCK = DATA / "camera.lock"

#: Uncalibrated: a face this square to a camera counts as looking at a screen.
FACING_YAW, FACING_PITCH = 30.0, 25.0
#: Calibrated: a gaze within this many degrees of a screen's calibrated
#: direction (or within its own spread, if wider) is looking at it.
NEAR = 14.0
#: Hysteresis: looking at a screen counts after ON s, looking away after OFF s.
ON, OFF = 0.3, 1.0
#: Eye-in-head offset (iris position in the eye, -0.5..0.5) to degrees.
EYE_GAIN_X, EYE_GAIN_Y = 60.0, 40.0


def available() -> str:
    """"" if camera mode can run here, else why not."""
    try:
        import cv2  # noqa: F401
        import mediapipe  # noqa: F401
    except ImportError:
        return "camera support isn't installed (Settings > Camera > Install)"
    return ""


def ensure_model() -> None:
    if FACE_MODEL.exists():
        return
    import httpx

    DATA.mkdir(parents=True, exist_ok=True)
    log.info("camera: downloading the face model (%s)", FACE_MODEL.name)
    tmp = FACE_MODEL.with_suffix(".part")
    with httpx.stream("GET", FACE_MODEL_URL, follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)
    tmp.replace(FACE_MODEL)


def parse_cameras(lines: list[str]) -> dict[int, int]:
    """config "0:1" (camera 0 sits on screen 1) -> {0: 1}. A bare "0" is
    camera 0 on screen 1."""
    out = {}
    for line in lines:
        cam, _, mon = str(line).partition(":")
        try:
            out[int(cam.strip())] = int(mon.strip() or 1)
        except ValueError:
            log.warning("camera: %r isn't 'camera:screen' (like 0:1)", line)
    return out


# -- from landmarks to a direction ---------------------------------------------

def head_angles(matrix) -> tuple[float, float]:
    """Yaw (left/right) and pitch (up/down), degrees, from the face's 4x4
    transformation matrix. Signs don't matter much: calibration compares a
    gaze with recorded gazes, not with a fixed straight-ahead."""
    r = [[float(matrix[i][j]) for j in range(3)] for i in range(3)]
    yaw = math.degrees(math.atan2(-r[2][0], math.hypot(r[2][1], r[2][2])))
    pitch = math.degrees(math.atan2(r[2][1], r[2][2]))
    return yaw, pitch


#: Landmarks: eye corners (outer, inner), lids (upper, lower), iris centres.
_LEFT = (33, 133, 159, 145, 468)
_RIGHT = (263, 362, 386, 374, 473)


def eye_offset(points) -> tuple[float, float]:
    """Where the irises sit in the eyes, averaged: x -0.5 (towards the outer
    corner) .. 0.5, y -0.5 (up) .. 0.5. ``points``: (x, y) per landmark."""
    xs, ys = [], []
    for outer, inner, upper, lower, iris in (_LEFT, _RIGHT):
        (ox, oy), (ix, iy) = points[outer], points[inner]
        (_, uy), (_, ly) = points[upper], points[lower]
        cx, cy = points[iris]
        width = math.hypot(ix - ox, iy - oy) or 1e-6
        # Along the eye, from its middle; the outer corner is on opposite
        # sides for the two eyes, so measure towards the image's right.
        along = ((cx - (ox + ix) / 2) * (ix - ox) + (cy - (oy + iy) / 2) * (iy - oy)) / width ** 2
        xs.append(along if ix > ox else -along)
        height = (ly - uy) or 1e-6
        ys.append((cy - (uy + ly) / 2) / height)
    return sum(xs) / 2, sum(ys) / 2


def gaze(yaw: float, pitch: float, eye: tuple[float, float]) -> tuple[float, float]:
    """Head plus eyes, in degrees: roughly where the face is pointed and
    looking. Calibration absorbs the scale and the signs."""
    return yaw + EYE_GAIN_X * eye[0], pitch + EYE_GAIN_Y * eye[1]


# -- deciding ------------------------------------------------------------------

@dataclass
class Sample:
    camera: int
    face: bool
    gx: float = 0.0
    gy: float = 0.0


class Calibration:
    """Per camera, per screen: the mean gaze when looking at it and its spread."""

    def __init__(self, data: dict | None = None):
        # {camera: {screen: (gx, gy, spread)}}
        self.data: dict[int, dict[int, tuple[float, float, float]]] = {}
        for cam, screens in (data or {}).items():
            self.data[int(cam)] = {int(s): tuple(v) for s, v in screens.items()}

    @classmethod
    def load(cls) -> Calibration:
        try:
            return cls(json.loads(CALIBRATION.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            log.warning("camera: could not read %s: %s", CALIBRATION.name, exc)
            return cls()

    def save(self) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        CALIBRATION.write_text(json.dumps(
            {str(c): {str(s): list(v) for s, v in d.items()} for c, d in self.data.items()},
            indent=1), encoding="utf-8")

    def nearest(self, s: Sample) -> tuple[int, float] | None:
        """(screen, how far in units of "near enough") for this camera's sample."""
        screens = self.data.get(s.camera)
        if not screens or not s.face:
            return None
        best = None
        for screen, (mx, my, spread) in screens.items():
            d = math.hypot(s.gx - mx, s.gy - my) / max(NEAR, 2.0 * spread)
            if best is None or d < best[1]:
                best = (screen, d)
        return best


def decide(samples: list[Sample], cal: Calibration, mounts: dict[int, int]) -> int | None:
    """The screen being looked at now (1-based), 0 for "a screen, but which
    isn't clear", or None for looking away or no face."""
    calibrated = [(cal.nearest(s), s) for s in samples if s.face and cal.data.get(s.camera)]
    hits = [(n, s) for n, s in calibrated if n is not None]
    if hits:
        (screen, d), _ = min(hits, key=lambda h: h[0][1])
        return screen if d <= 1.0 else None
    # Uncalibrated: facing a camera squarely means its own screen.
    for s in samples:
        if s.face and abs(s.gx) <= FACING_YAW and abs(s.gy) <= FACING_PITCH:
            return mounts.get(s.camera, 0) if abs(s.gx) <= FACING_YAW / 2 else 0
    return None


class Attention:
    """Looking or not, with hysteresis, from a stream of decisions."""

    def __init__(self):
        self.looking = False
        self.screen: int | None = None
        self._since = 0.0  # when the raw decision last flipped
        self._raw = False
        self.last_looking = 0.0  # monotonic time it last counted as looking

    def update(self, screen: int | None, now: float) -> bool:
        """Feed a decision; True if ``looking`` changed."""
        raw = screen is not None
        if raw != self._raw:
            self._raw, self._since = raw, now
        changed = False
        if raw and not self.looking and now - self._since >= ON:
            self.looking, changed = True, True
        elif not raw and self.looking and now - self._since >= OFF:
            self.looking, changed = False, True
        if self.looking:
            self.last_looking = now
            if raw:
                self.screen = screen
        else:
            self.screen = None
        return changed


# -- the camera thread -----------------------------------------------------------

class Watcher:
    """Reads the cameras on a thread and keeps ``attention`` up to date."""

    def __init__(self, cfg, on_change: Callable[[bool, int | None], None] | None = None):
        self.cfg = cfg  # CameraConfig
        self.on_change = on_change or (lambda looking, screen: None)
        self.attention = Attention()
        self.calibration = Calibration.load()
        self.running = False
        self.error = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # What the listener asks.
    def looking(self) -> bool | None:
        """True / False while running; None when off (nothing to go by)."""
        if not self.running or self.error:
            return None
        return self.attention.looking

    def looked_within(self, seconds: float) -> bool | None:
        if not self.running or self.error:
            return None
        return self.attention.looking or time.monotonic() - self.attention.last_looking <= seconds

    def status(self) -> str:
        if not self.running:
            return "Camera: off"
        if self.error:
            return f"Camera: {self.error}"
        if LOCK.exists():
            return "Camera: calibrating"
        if not self.attention.looking:
            return "Camera: looking away"
        s = self.attention.screen
        return f"Camera: looking at screen {s}" if s else "Camera: looking at the screens"

    def start(self) -> None:
        if self.running:
            return
        why = available()
        if why:
            self.error = why
            self.running = True
            log.warning("camera: %s", why)
            return
        self.error = ""
        self.calibration = Calibration.load()
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="localflow-camera")
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _open(self):
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        ensure_model()
        cams = {}
        for index in parse_cameras(self.cfg.cameras):
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                log.warning("camera: camera %d didn't open", index)
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            options = vision.FaceLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(FACE_MODEL)),
                running_mode=vision.RunningMode.VIDEO, num_faces=1,
                output_facial_transformation_matrixes=True)
            cams[index] = (cap, vision.FaceLandmarker.create_from_options(options))
        return cams, mp

    def _close(self, cams) -> None:
        for cap, marker in cams.values():
            try:
                cap.release()
                marker.close()
            except Exception:
                pass
        cams.clear()

    def _run(self) -> None:
        cams: dict = {}
        mp = None
        start = time.monotonic()
        try:
            while not self._stop.is_set():
                if LOCK.exists():  # calibration has the cameras
                    if cams:
                        self._close(cams)
                        log.info("camera: paused for calibration")
                    self._stop.wait(1.0)
                    continue
                if not cams:
                    cams, mp = self._open()
                    self.calibration = Calibration.load()
                    if not cams:
                        self.error = "no camera opened"
                        self._stop.wait(5.0)
                        continue
                    self.error = ""
                    log.info("camera: watching with %d camera(s)", len(cams))
                t0 = time.monotonic()
                samples = [read(index, cap, marker, mp, int((t0 - start) * 1000))
                           for index, (cap, marker) in cams.items()]
                screen = decide([s for s in samples if s], self.calibration,
                                parse_cameras(self.cfg.cameras))
                if self.attention.update(screen, t0):
                    log.info("camera: %s", "looking at the screens" if self.attention.looking
                             else "looking away")
                    self.on_change(self.attention.looking, self.attention.screen)
                period = 1.0 / max(1.0, float(self.cfg.fps))
                self._stop.wait(max(0.0, period - (time.monotonic() - t0)))
        except Exception as exc:
            self.error = "stopped (see the log)"
            log.warning("camera: stopped: %s", exc, exc_info=True)
        finally:
            self._close(cams)


def read(index: int, cap, marker, mp, ms: int) -> Sample | None:
    """One frame from one camera -> a Sample. The frame goes no further."""
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    import cv2

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    result = marker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ms)
    del frame, rgb
    if not result.face_landmarks:
        return Sample(index, False)
    lm = result.face_landmarks[0]
    yaw, pitch = head_angles(result.facial_transformation_matrixes[0])
    points = [(p.x * w, p.y * h) for p in lm]
    eye = eye_offset(points) if len(points) > 473 else (0.0, 0.0)
    gx, gy = gaze(yaw, pitch, eye)
    return Sample(index, True, gx, gy)


# -- calibration (its own process: `localflow camera-calibrate`) ---------------------

SETTLE, HOLD_S = 0.8, 2.0


def calibrate(cfg) -> str:
    """Look at a dot on each screen in turn; record the gaze per camera per
    screen. Returns a one-line summary. The tray's watcher lets go of the
    cameras while LOCK exists."""
    import ctypes
    import statistics
    import tkinter as tk

    from . import desktop
    from .icon import set_app_identity

    why = available()
    if why:
        return why
    set_app_identity()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Tk in real pixels
    except Exception:
        pass
    DATA.mkdir(parents=True, exist_ok=True)
    LOCK.write_text("calibrating", encoding="utf-8")
    cams: dict = {}
    w = Watcher(cfg)
    try:
        time.sleep(1.5)  # the tray's watcher closes its cameras
        cams, mp = w._open()
        if not cams:
            return "no camera opened - check Settings > Camera"
        root = tk.Tk()
        root.withdraw()
        cal = Calibration()
        cancelled = [False]
        start = time.monotonic()
        for n, (_h, mon, _work) in enumerate(desktop.monitors(), 1):
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.geometry(f"{mon.right - mon.left}x{mon.bottom - mon.top}+{mon.left}+{mon.top}")
            c = tk.Canvas(win, bg="#0a0907", highlightthickness=0)
            c.pack(fill="both", expand=True)
            win.bind("<Escape>", lambda _e: cancelled.__setitem__(0, True))
            win.focus_force()
            cx, cy = (mon.right - mon.left) // 2, (mon.bottom - mon.top) // 2
            c.create_oval(cx - 18, cy - 18, cx + 18, cy + 18, fill="#d4af37", outline="#f3dc95",
                          width=3)
            c.create_text(cx, cy + 60, fill="#f3dc95", font=("Segoe UI", 20),
                          text=f"Look at the dot  -  screen {n}")
            c.create_text(cx, cy + 100, fill="#8b8e98", font=("Segoe UI", 12),
                          text="Turn to it as you would to read it.  Esc cancels.")
            got: dict[int, list[tuple[float, float]]] = {}
            t_end = time.monotonic() + SETTLE + HOLD_S
            while time.monotonic() < t_end and not cancelled[0]:
                root.update()
                now = time.monotonic()
                for index, (cap, marker) in cams.items():
                    s = read(index, cap, marker, mp, int((now - start) * 1000))
                    if s and s.face and now > t_end - HOLD_S:
                        got.setdefault(index, []).append((s.gx, s.gy))
            win.destroy()
            if cancelled[0]:
                return "calibration cancelled"
            for index, pts in got.items():
                if len(pts) < 5:
                    continue
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                spread = math.hypot(statistics.pstdev(xs), statistics.pstdev(ys))
                cal.data.setdefault(index, {})[n] = (statistics.mean(xs), statistics.mean(ys),
                                                     spread)
        root.destroy()
        if not cal.data:
            return "no face seen - is the camera pointing at you?"
        cal.save()
        seen = ", ".join(f"camera {c}: screens {', '.join(map(str, sorted(d)))}"
                         for c, d in sorted(cal.data.items()))
        log.info("camera: calibrated (%s)", seen)
        return f"Calibrated - {seen}"
    finally:
        w._close(cams)
        try:
            LOCK.unlink()
        except OSError:
            pass
