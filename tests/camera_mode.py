"""Camera mode, part one (attention): the geometry, the screen decision, the
hysteresis and the listener's use of it, without a camera. If camera
support is installed, the real face model also runs on a blank picture
(no face) to prove the pipeline loads; no camera is opened.

    uv run python tests/camera_mode.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np

from localflow import camera, commands
from localflow.always_on import RATE, Listener
from localflow.camera import Attention, Calibration, Sample, decide
from localflow.config import Config
from localflow.engine import Engine

failures = 0


def expect(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + (f": {detail}" if not ok else ""))


print("head and eyes")
yaw, pitch = camera.head_angles(np.eye(4))
expect("facing the camera: 0, 0", abs(yaw) < 1e-6 and abs(pitch) < 1e-6, f"{yaw} {pitch}")
a = math.radians(30)
turned = [[math.cos(a), 0, math.sin(a), 0], [0, 1, 0, 0], [-math.sin(a), 0, math.cos(a), 0],
          [0, 0, 0, 1]]
yaw, pitch = camera.head_angles(turned)
expect("turned 30 degrees: yaw 30, pitch 0", abs(yaw - 30) < 0.5 and abs(pitch) < 0.5,
       f"{yaw:.1f} {pitch:.1f}")


def face(shift_x=0.0, shift_y=0.0):
    """Landmarks for two eyes 40 px wide, irises shifted by a fraction of an eye."""
    pts = [(0.0, 0.0)] * 478
    for outer, inner, upper, lower, iris, cx in ((33, 133, 159, 145, 468, 200),
                                                 (263, 362, 386, 374, 473, 300)):
        side = -1 if cx == 200 else 1  # outer corners face away from the nose
        pts[outer], pts[inner] = (cx + side * 20, 100), (cx - side * 20, 100)
        pts[upper], pts[lower] = (cx, 92), (cx, 108)
        pts[iris] = (cx + shift_x * 40, 100 + shift_y * 16)
    return pts


ex, ey = camera.eye_offset(face())
expect("irises in the middle: 0, 0", abs(ex) < 1e-6 and abs(ey) < 1e-6, f"{ex} {ey}")
ex, ey = camera.eye_offset(face(0.2, 0.25))
expect("both irises moved right and down: the same direction for both eyes",
       abs(abs(ex) - 0.2) < 1e-6 and abs(ey - 0.25) < 1e-6, f"{ex} {ey}")

print("which screen")
cal = Calibration({"0": {"1": [0, 0, 2], "2": [35, 2, 3], "3": [70, 4, 3]}})
mounts = {0: 1}
expect("near screen 2's direction: screen 2", decide([Sample(0, True, 33, 0)], cal, mounts) == 2)
expect("far right of screen 3's: screen 3", decide([Sample(0, True, 76, 6)], cal, mounts) == 3)
expect("way off (looking down at the desk): away",
       decide([Sample(0, True, 0, 45)], cal, mounts) is None)
expect("no face: away", decide([Sample(0, False)], cal, mounts) is None)
two = Calibration({"0": {"1": [0, 0, 2], "2": [40, 0, 3]}, "1": {"2": [0, 0, 2], "3": [40, 0, 3]}})
expect("two cameras: the one that sees it best decides",
       decide([Sample(0, True, 60, 0), Sample(1, True, 39, 1)], two, {0: 1, 1: 2}) == 3)
none = Calibration()
expect("not calibrated: square to the camera is its screen",
       decide([Sample(0, True, 3, 2)], none, {0: 2}) == 2)
expect("...turned a bit: a screen, but which isn't clear (0)",
       decide([Sample(0, True, 22, 2)], none, {0: 2}) == 0)
expect("...turned right away: away", decide([Sample(0, True, 50, 0)], none, {0: 2}) is None)

print("looking and looking away (hysteresis)")
att = Attention()
att.update(1, 0.0)
expect("not straight away", not att.looking)
att.update(1, 0.35)
expect("after 0.3 s: looking, at screen 1", att.looking and att.screen == 1)
att.update(None, 1.0)
att.update(None, 1.5)
expect("a half-second glance away: still looking", att.looking)
att.update(1, 1.6)
att.update(None, 2.0)
att.update(None, 3.1)
expect("away for over a second: not looking", not att.looking and att.screen is None)

print("the listener")
commands.LEARNED = Path(__file__).parent / "_unused_learned.json"
cfg = Config()
cfg.history.enabled = False
cfg.cleanup.method = "rules"
lis = Listener(Engine(cfg), cfg, inject=False)


class FakeCamera:
    def __init__(self, looking):
        self.state = looking

    def looking(self):
        return self.state

    def looked_within(self, seconds):
        return self.state


expect("no camera: the start phrase always counts", not lis._looking_away())
lis.camera = FakeCamera(False)
expect("looking away: the start phrase is ignored", lis._looking_away())
cfg.camera.gate_start = False
expect("...unless that's switched off", not lis._looking_away())
cfg.camera.gate_start = True
lis.camera = FakeCamera(None)
expect("camera on but not running (None): counts", not lis._looking_away())

lis.commanding = True
lis._mode_until = 0
lis.ring.end = 10 * RATE
lis.camera = FakeCamera(True)
lis.ingest(np.zeros(1600, np.float32))
expect("command mode past its timeout while looking at a screen: stays open", lis.commanding)
lis.camera = FakeCamera(False)
lis._mode_until = 0
lis.ingest(np.zeros(1600, np.float32))
expect("...looking away: it times out", not lis.commanding)

print("the face model (no camera opened)")
why = camera.available()
if why:
    print(f"  skip  {why}")
else:
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    camera.ensure_model()
    marker = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(camera.FACE_MODEL)),
        running_mode=vision.RunningMode.VIDEO, num_faces=1,
        output_facial_transformation_matrixes=True))

    class Blank:
        def read(self):
            return True, np.full((480, 640, 3), 128, np.uint8)

    s = camera.read(0, Blank(), marker, mp, 0)
    expect("a blank picture: read, no face", s is not None and not s.face, str(s))
    marker.close()

print("\nall passed" if not failures else f"\n{failures} FAILED")
