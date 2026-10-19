# Camera mode: design notes

**Status (2026-10-18):**

| Part | State |
|---|---|
| 1. Attention gating: which screen you're looking at; the start phrase gated; command mode held | **Built** (`camera.py`, Settings ▸ Camera, `tests/camera_mode.py`) |
| Camera pill: an eye and "Screen N" on the screen you're looking at | **Built** (`overlay._draw_cam`) |
| "camera on / off", "calibrate camera" by voice (and on the command sheet) | **Built** |
| Everything else below | Plan |

Built so far: head pose plus iris position from MediaPipe's face
landmarker, a per-camera, per-screen calibration (nearest calibrated
direction), and a camera-to-screen mapping (`camera.cameras`, "0:1"). It
has not yet been tried against a real face at the time of writing: the
first real calibration will show how well one webcam separates screens.

Voice runs the PC today. The next step is the rest of the body: where you
look, what your hands do, and a way to stop everything instantly. This is
the plan for adding webcams (and, later, an eye tracker) as a second input,
written so it can be built in small, separately useful steps.

Why both: voice is strong at intent, vocabulary and state changes ("close
this", "open the terminal") and weak at spatial targeting, gating and fast
interrupts (speech-to-text plus parsing is 300-1500 ms). Vision is the
reverse: landmark models on the CPU give presence, direction and hand
shape in a few milliseconds a frame, with no audio latency. Each covers the
other's blind spots.

## Principles (the same as the rest of Golden Flow)

- **Local only.** Frames are processed in memory and dropped. Nothing is
  recorded or saved to disk, sent anywhere, or kept beyond the last second.
  The tray and the pill show plainly when the camera is on.
- **Off by default, one switch.** Camera mode is its own toggle, like
  always-on listening. Every part of it (gaze gating, gestures, the kill
  gesture) can be turned on and off separately.
- **Algorithms first.** Landmark models and geometry, no LLM. The LLM stays
  an optional layer for requests, as it is now.
- **Voice stays the main channel.** The camera adds *where* and *how much*
  (which window, how far to scroll) and *stop*. It doesn't replace the
  commands.
- **It must never surprise you.** A gesture that's misread is worse than no
  gesture: continuous controls need to be armed, and anything destructive
  still needs a word.

## What it would add

### 1. Attention gating (gaze gating)

Only act while you're looking at the screen.

- Face and head pose from a face-landmark model (MediaPipe Face Landmarker,
  478 points, CPU, ~5 ms a frame at 640x480).
- "Attending" = a face is found, roughly frontal (yaw and pitch within about
  25°), and looking at the screen (eye landmarks, see 6).
- Uses:
  - The start phrase only counts while you're attending. That cuts false
    starts from TV, calls and people in the room, and could let the start
    phrase get shorter.
  - Command mode pauses when you look away (talking to someone) and resumes
    when you look back, instead of timing out.
  - Optional: dictation pauses while you look away.
- Hysteresis: about 300 ms to switch on, about 1 s to switch off, so a
  glance doesn't toggle it.
- **Several screens.** One camera sees head turns, but two screens to the
  same side are hard to tell apart by gaze alone. A camera on each screen
  you use solves it: whichever camera you face most squarely, after
  calibration, names the screen. `camera.cameras` maps each camera to the
  screen it sits on.
- **Presence** (next): nobody at the desk for N minutes pauses media and
  locks the PC (optional); sitting back down wakes the screen and says
  what happened while you were away. A face appearing is a better "I'm
  back" than moving the mouse.
- The long-term promise: no wake word at all. While you're looking at a
  screen, speech can be heard as a command or dictation directly; looking
  away (at a person, a phone) gates it off. That needs a very reliable
  gaze signal first, so the start phrase stays until it has one.

### 2. Spatial window addressing

"Close **that**", "move **this** to monitor 2", "switch to the one on the
**left**", said while looking or pointing.

- Head pose plus gaze gives a point on the desk's screen layout.
  `desktop.monitors()` already numbers the screens and `desktop.windows()`
  lists the windows in z-order. Hit-testing that point gives the window.
- A webcam's gaze is rough (about 2-4°, roughly a fist at arm's length), so
  it targets a **monitor, a half or quarter, or a big window** reliably, not
  a button. Buttons stay with the numbered labels.
- While a deictic word ("that", "this", "there", "here") is being said, the
  pill can outline the window that would be picked, so a wrong pick shows
  before it happens.

### 3. Fast interrupts and the emergency stop

The one feature that has to be both instant and unmissable.

- **Cover the camera = master kill.** Frame brightness and variance drop to
  near zero within a frame or two (no model needed, under 50 ms).
  Held for about 0.3 s it:
  - stops any dictation without pasting;
  - cancels queued and running commands (`Runner`), and a pending LLM plan;
  - releases any held keys or mouse buttons;
  - hides the labels, the grid and the lists;
  - pauses listening until you say the start phrase or uncover and nod.
- **Open palm toward the camera = stop** (softer): cancels the current
  dictation or command, like "cancel".
- Both bypass the speech pipeline entirely, so they work when speech
  recognition is busy or confused. That's the point of them.
- They're logged ("kill gesture"), with a sound, so a false trigger is
  visible and can be tuned.
- **"Shh" (a finger to the lips)**: mute the mic at once and, once it
  exists, stop speech output; pause media. The face and hand landmarks
  together make it unambiguous (index fingertip at the mouth).

### 4. Continuous controls (where voice is clumsy)

"Scroll down a bit... more... more" is slow. Hands do amounts well.

- MediaPipe Hand Landmarker: 21 points a hand, ~5-8 ms on the CPU.
- **Pinch and slide** (vertical: volume, brightness, a window's opacity;
  horizontal: scrub a video or a track): thumb-index pinch arms a slider; moving the pinched
  hand up and down scrolls (or sets the volume or brightness) in proportion;
  releasing ends it. The pinch is the "clutch", so a moving hand alone does
  nothing.
- **Open hand tilt**: rotating an open hand (the wrist-to-middle-finger
  angle) zooms in and out (Ctrl+wheel), or rotates where an app supports
  it.
- **Point**: the index finger ray, smoothed (One Euro filter), moves a
  highlight between windows or monitors; "click" or a pinch acts on it.
  Fine pointing stays with the labels and the grid, since a webcam hand ray
  wobbles.
- Voice can name the control and the hand gives the amount: "volume" then a
  pinch-slide, "zoom" then a tilt.

### 5. Multimodal fusion ("put that there")

Join a spoken command with what the eyes or hand were doing *when the
word was said*.

- Whisper's word timestamps already tell when "that" and "there" were
  spoken. The camera keeps a short ring buffer (about 2 s) of gaze, point
  and gesture events with timestamps.
- At each deictic word, look up the target at that moment (±150 ms), not
  at the end of the sentence. People look at the target as they say the
  word and have often looked away by the end.
- Fusion is one extra step before `Grammar.parse` hands over the command:
  a slot of "that" or "this" becomes the window under the gaze or point at
  that word's time. No change to the commands themselves.
- When there's no confident target, it asks ("which window?") or shows the
  window list, the same as an unknown name now.

### 6. Eye tracking

- **Webcam first**: iris landmarks from the face model plus head pose, and
  a 9-point calibration (look at dots; about 20 seconds), stored per screen
  layout. Good enough for gating, monitors and big windows (see 2).
- **A dedicated tracker later** (Tobii Eye Tracker 5 and similar): about
  0.5-1° accuracy, which makes "look at a button and say click" realistic.
  A `GazeSource` interface keeps webcam and tracker interchangeable.
- Dwell-to-click is deliberately *not* planned as a default (the "Midas
  touch" problem: everything you look at gets clicked). The voice word or a
  pinch confirms.

### 7. Micro-gestures (discrete, quick, unambiguous)

Short poses checked with simple geometry on the 21 hand landmarks (angles
and distances, normalised by hand size). Each has to be held briefly
(about 300 ms) and be clearly different from resting hands, typing or
reaching for the mouse.

| Gesture | How it's recognised | What it does |
|---|---|---|
| Thumbs up / down | thumb tip above / below the wrist, other fingers curled | "yes" / "no" to a waiting confirmation (an LLM plan that sends something, "go to sleep") |
| Swipe left / right | hand centre moves fast sideways (dx over a threshold within ~300 ms) | back / forward, or the previous / next virtual desktop |
| Peace sign | index and middle out, ring and pinky curled | mute / unmute the mic, or toggle command mode |
| Fist | every fingertip near the palm | show the desktop / minimise all |
| Flick away | back of the hand, fingers flicked outward | dismiss a notification, hide the labels or the command sheet |
| Two L-shapes | both hands, thumb and index at right angles | frame a region: screenshot it, or put a window there |

### Design rules (from building and from a second opinion)

- **No "gorilla arm".** Nothing should need a hand held up for long:
  gestures are short triggers (under 2 s) or can be made with an elbow or
  wrist resting on the desk. Continuous controls use a pinch as a clutch so
  the hand can rest between moves.
- **Look back in time for pointing.** People point slightly before and
  while saying "this". When "this" is transcribed, take where the hand or
  gaze was 200-400 ms before the word's start, from the ring buffer.
- **Keep the vision stack small and local.** Landmark models (MediaPipe;
  YOLO-pose or small ONNX models if needed) at 15-30 fps on the CPU, and
  geometry rules, not a vision LLM on every frame.
- **Escalate only on request.** A single high-resolution frame goes to a
  vision-capable LLM only when asked ("what is this?" while pointing at
  something on the desk), and only to a local model unless the user
  chooses otherwise.

## How it fits the code

```
camera thread (OpenCV capture, 640x480 @ 30 fps)
  -> landmark models (MediaPipe face + hands, CPU)
  -> features: attending?, gaze point, hand pose, pinch, palm, covered?
  -> recognizers, each a small state machine with hysteresis
  -> events into a ring buffer (timestamped, about 2 s)
       |-> kill / stop     -> Listener + Runner directly (fast path)
       |-> attention       -> Listener (gates the start phrase and command mode)
       |-> continuous      -> desktop.scroll / volume / zoom
       |-> gaze and point  -> fusion, looked up at word times
```

- A new `camera.py` (capture and models), `gestures.py` (recognizers),
  `fusion.py` (deictic slots). `always_on.Listener` gets an `attending`
  input and a `kill()` method; `commands.Runner` gets `cancel_all()`.
- Settings gets a **Camera** tab: on/off, which camera, a live preview of
  the landmarks (drawn only in that window), each feature's switch, gaze
  calibration, and the kill gesture's sensitivity.
- Overlay: a small camera glyph on the pill while it's on; the target
  outline for "that".

## Budgets

- **Latency**: kill under 100 ms from covering the lens; stop palm under
  200 ms; sliders under 50 ms a frame so they feel attached to the hand.
- **CPU**: face and hands at 15-30 fps on the CPU is about 10-20% of one
  core on a modern desktop; run the hand model only when a hand is in view
  (the face model can say where hands could be). This PC is short of RAM,
  so the models load only while camera mode is on.
- **GPU**: none needed; it stays free for Whisper and the LLM.

## Testing (with controls, like the rest)

- Recorded clips are **not** kept in the repo (they're faces). Instead:
  - landmark streams saved as numbers (no images) from a few real sessions,
    replayed through the recognizers;
  - synthetic streams for the edge cases (a hand passing through without a
    pinch, a glance away, a dark room);
  - each recognizer test has a control that must *not* trigger (waving
    without pinching never scrolls; turning your head for a second doesn't
    end command mode).
- The kill gesture gets a soak test: hours of ordinary work with it armed,
  and a count of false triggers. The target is zero.

## Build order

1. ~~**Attention gating** for the start phrase and command mode.~~ Built,
   with the camera pill and voice on/off.
2. **Kill switch** (cover the lens) and the open-palm stop: no extra
   model, biggest safety value. Next.
3. **Pinch-slide scrolling and volume**, then **tilt zoom**.
4. **Spatial addressing and "that"/"this"** (gaze plus voice fusion).
5. **Pointing** at windows and monitors.
6. **Eye tracker support** (`GazeSource`), then gaze-and-say clicking of
   buttons.

7. **Micro-gestures** (thumbs up/down for confirmations first: it fits the
   "yes" prompts that exist today), then presence and auto-lock.

## Open questions

- Which camera: the built-in one, or a dedicated one on top of the main
  monitor (better angle for gaze)? Several screens make gaze geometry
  harder; calibration per monitor arrangement is probably needed.
- Glasses and low light: how much do they cost the iris landmarks? Measure
  before promising gaze anything.
- Should the kill gesture also mute the mic at the system level, or only
  pause LocalFlow?
- Naming: with camera and gestures in, the app is about more than the flow
  of dictation, which is one more reason to rename it before this lands.
