# DeskGuard

A tiny always-on-top webcam preview for Windows 11. Mount a USB webcam on your
cubicle partition and see who's approaching without turning around.

- 256×144 (16:9) floating preview, top-right by default. Drag to move, drag the edges to resize.
- Wide view: captures 640×360 by default, which uses the full sensor width (4:3 modes cut the sides off).
- Rear-view-mirror image by default (flip left–right), plus flip upside-down / rotate 180° for any mounting.
- Digital zoom and pan (Ctrl + wheel, Ctrl + drag), and the camera driver's own settings dialog (zoom, focus, exposure…).
- It never takes keyboard focus, never shows in the taskbar, and makes no sounds or popups.
- Optional motion detection (off by default) shows a red **MOTION** border.
  **Ignore zones** let you draw over yourself so only the background (people approaching) triggers it.
- **Watch my back** (optional): DeskGuard learns your face, ignores you automatically, and shows a silent
  notification – *"Someone is watching you"* – when another person stays behind you for 5 seconds.
- A Windows 11-style **Settings** window (dark/light, follows Windows), plus a short right-click menu for quick actions.
- Everything stays private: no recording, no screenshots, no audio, no network. Frames exist only in memory;
  your face is stored only as numbers (never a photo) and can be deleted at any time.

---

## 1. Run from source

Requires **Python 3.12 or newer (64-bit)** on Windows 11.

```bat
cd DeskGuard
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

After that, `run.bat` starts it without a console window.

Command-line options:

| Option | Effect |
|---|---|
| `--diag` | Logs RAM and CPU every minute (use for measurements) |
| `--reset` | Restores default settings |

## 2. Build the .exe

```bat
build.bat
```

This creates `.venv`, installs pinned dependencies, checks the bundled face models
(`tools\fetch_models.py` downloads them only if they're missing, verified by SHA-256), runs
the tests and builds `dist\DeskGuard\DeskGuard.exe` plus the installer.

**Install:** copy the whole `dist\DeskGuard` folder anywhere (for example
`%LOCALAPPDATA%\Programs\DeskGuard`) and run `DeskGuard.exe`. No admin rights
are needed. Use **Settings → Window & app → Start with Windows** to launch it at sign-in.

It's a one-folder build on purpose. A one-file exe unpacks itself to a temp
folder on every launch, which means a slower start, an extra process and more
antivirus false positives.

## 3. Using it

| Action | How |
|---|---|
| Move | Drag anywhere on the preview |
| Resize | Drag an edge or corner (minimum 160×100) |
| Hide / show | Hover → **–** button, or left-click the tray icon |
| Exit | Hover → **✕** button, or tray/right-click → Exit |
| Settings | Double-click the preview, or right-click → **Settings…** |
| Quick actions | Right-click the preview or the tray icon |
| Zoom in / out | **Ctrl + mouse wheel** over the preview (zooms at the cursor, 1×–4×) |
| Pan while zoomed | **Ctrl + drag** |
| Ignore yourself | Turn on **Watch my back** (it finds you automatically), or right-click → **Edit ignore zones…**, drag rectangles over where you sit, then **Done**. Right-click a zone to delete it. |

**Status dot** (top-left of the preview, also on the tray icon):

| Colour | Meaning |
|---|---|
| Green | Camera live |
| Amber | Starting or reconnecting |
| Grey | Paused (camera off) |
| Red | Error (the message explains why) |

### Watch my back

1. Right-click the preview → **Watch my back** (or Settings → Watch my back → switch on).
2. The first time, DeskGuard opens **face setup**: sit as usual and look at the camera. It takes about
   5 seconds and asks you to turn your head slightly. Only numbers that describe your face are saved.
3. That's it. The preview shows a dashed green **You** outline around your body area, and a red box
   around anyone else. If someone stays behind you for the set time (default **5 s**), a notification
   slides in at the bottom-right: **"Someone is watching you"**, with **Show preview** and **Dismiss**.
   It is silent, never takes your keyboard, and closes about 6 s after they leave.

How it decides:
- **Faces:** every face is compared with yours. A face that isn't yours counts as "someone behind you".
- **People without a visible face** (optional, on by default): a person-shaped change against the empty
  background, outside your body area, also counts – so someone looking away is still noticed.
- **You:** your body area is worked out from where your face is. It stays put when you look down or turn
  away, so your own movement never triggers an alert (or the MOTION border).
- People walking past are ignored: the person must stay for the whole delay (short gaps are tolerated).
  After an alert, the next one waits for the cooldown (default 1 minute).
- Watch my back keeps working while the preview is hidden. Until your face is set up, the person
  closest to the camera is treated as you.

### Settings window

| Page | Options |
|---|---|
| Watch my back | On/off · Your face (set up, set up again, forget) · Alert after 3/5/10/15 s · Also detect people whose face isn't visible · Face matching relaxed/normal/strict · Don't repeat alerts within 30 s–5 min · Test alert |
| Motion | Motion detection · Sensitivity · Wait between alerts · Ignore zones (edit on preview, clear) · Keep monitoring while hidden · Show the preview on motion |
| Camera | Camera (by name; listing never opens a camera) · Resolution 640×360 (recommended) / 848×480 / 1280×720 / 1920×1080 / 640×480 · Frame rate 10/15 fps · Camera settings… (driver dialog) · Forget saved camera settings · Pause camera · Turn the camera off when hidden |
| View | Mirror (on by default) · Flip upside-down · Rotate 180° · Digital zoom 1–4× · Fill the window |
| Window & app | Always on top · Opacity · Theme (Windows setting / dark / light) · Start with Windows · Reset preview position · Log folder |
| Privacy | What is stored and where · Delete my face data |

The Settings window is created when you open it and destroyed when you close it, so it uses no memory
the rest of the time.

**Right-click menu (quick actions):** status · Hide/Show preview · Pause camera · Watch my back ·
Motion detection · Edit ignore zones… · Mirror image · Settings… · Exit.

**While hidden:**
- *Monitoring off (default):* the camera stays open but no frames are read (≈0.3 % CPU).
- *Turn camera off when hidden (opt-in):* after 1 minute hidden the camera is released (light off, 0 % CPU) and reopened (<1 s) when you show the preview. Off by default because each reopen leaks ≈6 MB inside Windows DirectShow (see *Known limitations*).
- *Monitoring on:* motion detection runs at 5 fps. Motion turns the tray icon red for 5 s.
- *Show preview on motion:* the preview also pops up without taking focus, then hides again after 10 s unless you touch it.

**Wide view.** Most webcams cut the sides off the sensor in 4:3 modes such as
640×480. The 16:9 modes keep the full width; 640×360 costs about the same CPU as
640×480. If your camera doesn't offer a mode, DeskGuard says so and uses the
nearest one it gets. Digital zoom never makes the view wider than 1×; if your
camera has optical zoom, set it to the widest in *Camera settings…*.

**Ignore zones** are stored relative to the camera frame, so they stay on the
same spot in the room when you flip, zoom or pan the preview. Motion inside a
zone is ignored, and the sensitivity applies to the remaining area only. The
zones show as faint dashed outlines while motion detection is on.

**Camera settings…** opens the driver's own property sheet (DirectShow
cameras). For 2 minutes afterwards DeskGuard notes which values you changed
and re-applies them every time the camera opens. Focus, exposure and white
balance are only saved while they are in manual mode, so automatic adjustment
is never frozen into a fixed value.

### Files

| File | Location |
|---|---|
| Settings | `%APPDATA%\DeskGuard\config.json` |
| Face profile | `%APPDATA%\DeskGuard\face.json` – 6 lists of 128 numbers, no image. Delete it (or use Settings → Privacy) to forget your face. |
| Log | `%LOCALAPPDATA%\DeskGuard\deskguard.log` (rotating, max ~1.5 MB) |

If the settings file is corrupt, it's renamed to `config.bad.json` and defaults are used.

---

## 4. How it works

```
Camera worker thread                         GUI thread
  VideoCapture (one instance, DirectShow)      PreviewWindow.paintEvent
                                                 QImage(BGR888) → drawImage
   ├ grab() every camera frame (no decode)       (no colour conversion, no scaling)
   ├ retrieve() only at 10/15 fps
   ├ motion: 160×120 grey diff (if enabled; your body area masked out)
   ├ Watch my back, 3×/s (if enabled): YuNet faces → SFace "is it me?" →
   │   your body area + strangers + still-person check → alert after the delay
   ├ zoom/pan = slice of the frame (no copy), resize to the window's pixel size, flip
   └ ──► latest-frame slot (max 1 frame) ──► frame_ready
```

There are two threads in total. The face models (`resources/models`, ~10 MB,
see `LICENSES.md` there) are loaded only while Watch my back or face setup is
in use, and unloaded when it's turned off. Recognition runs only for a new face
and then every 3 s per face, not on every frame. If the GUI is busy, frames are replaced, never queued.
OpenCV and OpenBLAS are limited to one thread each (`main.py`), and OpenCL is off.
When the requested size is the camera's default, no format property is
changed, so DirectShow builds its capture graph only once. Other sizes (such
as 640×360) change it exactly once per open. Saved hardware settings go
through IAMVideoProcAmp/IAMCameraControl and don't rebuild the graph.

| Module | Responsibility |
|---|---|
| `main.py` | Logging, single-instance lock, error dialogs, OpenCV/OpenBLAS thread and environment settings |
| `deskguard/app.py` | Wiring: window ↔ worker ↔ tray ↔ menu ↔ Settings window ↔ toast; show/hide; stall watchdog |
| `deskguard/faces.py` | Face engine (YuNet + SFace, lazy load/unload), your face profile, guided enrolment |
| `deskguard/presence.py` | Watch my back logic: face tracks, your body area, still-person check, alert timing |
| `deskguard/ui/` | Fluent theme, widgets (toggle switch, cards, progress ring), Settings window, face setup, toast |
| `deskguard/preview_window.py` | Frameless no-focus window, painting, manual drag/resize, hover buttons, zone editing, Ctrl+wheel zoom / Ctrl+drag pan |
| `deskguard/view.py` | Pure geometry: zoom/pan/crop/flip, preview ↔ camera-frame mapping |
| `deskguard/camera_worker.py` | Capture loop, frame pacing, reconnect with backoff, black-image check |
| `deskguard/camera.py` | Opening a camera with backend fallback; camera list; hardware property read/apply, driver dialog |
| `deskguard/motion.py` | Motion detector with ignore-zone mask (pure OpenCV/NumPy, no Qt) |
| `deskguard/config.py` | Settings dataclass, validation, atomic JSON save |
| `deskguard/win32.py` | ctypes/winreg helpers: DirectShow camera names, rounded corners, topmost, autostart, memory stats |
| `deskguard/icons.py` | App and tray icons drawn in code |

**Motion false-positive handling:**
- A running-average background absorbs slow lighting drift.
- Blur plus a per-pixel threshold ignores sensor noise.
- Motion must last 2–3 consecutive frames.
- A whole-scene change (lights switched on, auto-exposure) resets the background instead of alerting.
- A cooldown stops repeat alerts for the same event.

---

## 5. Testing

### Automated tests (no webcam needed)

```bat
python -m pytest -q
```

There are 103 tests across nine files:

| File | What it covers |
|---|---|
| `test_config.py` | Defaults, round-trip, invalid values, corrupt file, 1.1 → 1.2 migration (wide defaults, your own choices kept) |
| `test_view.py` | Zoom/pan/crop/flip geometry, preview ↔ camera-frame round trips, zoom-at-cursor, pan direction |
| `test_motion.py` | Noise, entering person, lights switched, slow drift, single-frame glitch, cooldown, sensitivity, ignore zones |
| `test_camera.py` | A fake camera at 30 fps: 10/15 fps pacing, decoding only needed frames, bounded hand-off, hidden idle, opt-in camera release and monitoring modes, DirectShow property changes kept to a minimum, unplug/replug recovery, retry backoff, pause/resume, resolution fallback message, clean release on stop, driver exceptions, mirror/zoom in the published frame, settings dialog (saved changes, auto-focus drift ignored, re-applied on reopen) |
| `test_presence.py` | Stranger alerts once after the delay, your face never alerts, passers-by ignored, flicker tolerated, cooldown, body area kept when your face disappears, still person without a face, person inside your area ignored, lighting change, no-profile fallback |
| `test_faces.py` | Face profile save/load/forget (numbers only), match thresholds, enrolment rules and timeout, real models load → detect → unload |
| `test_toast.py` | No-focus flags, live seconds, auto-close after the person leaves, buttons |
| `test_settings_window.py` | Every page paints, controls save settings, outside changes show up, theme switch, window destroyed on close, first-time face setup, alert → toast, short quick menu |
| `test_preview.py` | No-focus window flags, painting, drag/resize/minimum size, buttons, full app start → menu → hide/show → settings saved, drawing/deleting ignore zones (with mirror), Ctrl+wheel zoom, Ctrl+drag pan |

### Real-camera measurement

```bat
python tools\measure.py            :: ~5 min: startup, visible, hidden, motion, opt-in release
python tools\measure.py --long 30  :: adds a 30-minute visible soak
```

Runs the real app with your webcam (and a throw-away config) and prints CPU
(% of the whole machine, Task Manager scale) and RAM for each phase.

### Synthetic benchmark (separates DeskGuard's cost from your camera driver's)

```bat
python tools\bench_synthetic.py --seconds 60
python tools\bench_synthetic.py --seconds 60 --motion --fps 15
python tools\bench_synthetic.py --seconds 30 --hidden
```

### Manual checklist on your PC (record the results)

Start with `python main.py --diag` or `DeskGuard.exe --diag`. Resource lines are
written to the log 10 s after start and then every minute. Task Manager's
**Memory** column corresponds to the `private` value.

| Check | How | Result |
|---|---|---|
| RAM after start | Log line at ~10 s | |
| RAM after 30 min | Log line at ~30 min | |
| CPU, preview visible | Average of log lines | |
| CPU, preview hidden | Hide it, wait 3 min | |
| Disconnect recovery | Unplug USB cam → amber "reconnecting" → replug → green within ~10 s | |
| Keyboard focus | Type in Notepad while clicking, dragging and resizing the preview: typing must continue in Notepad | |
| Stays on top | Open or maximise Chrome, VS Code, Outlook | |
| Camera in use | Open the Windows Camera app first, then DeskGuard → clear error message, no crash | |
| Multi-monitor | Move the preview to a second screen, quit, undock, restart → appears on a visible screen | |

---

## 6. Measured performance

**Version 1.3.0** – HP Wide Vision HD Camera, 640×360, 10 fps, preview visible, running from source
(scripted run on this PC, 15–30 s per phase; CPU is % of the whole machine):

| Phase | CPU | Private | Working set |
|---|---|---|---|
| Watch my back off | 0.85 % | 60.9 MB | 100.7 MB |
| Watch my back on (models loaded, 3 checks/s) | 1.25–1.35 % | 77.1 MB | 119.9 MB |
| Turned off again (models unloaded) | 0.87 % | 63.7 MB | 104.0 MB |
| Built `DeskGuard.exe`, Watch my back on, 10 s after start | 1.85 % | 69.5 MB | 113.6 MB |

Face detection takes about 20 ms per check on this CPU, and a recognition about 55 ms, run only for
new faces and then every 3 s per face. Nobody was in front of the camera during this run, so the
recognition cost isn't included; a 30-minute run with Watch my back on is still to be done.

**Earlier versions:**

Measured on version 1.1.0 (Windows 11 Pro, Intel 12th gen, HP Wide Vision HD
Camera, 640×480, 10 fps) with `tools\measure.py`; not yet re-measured for 1.2.0
at 640×360. *Private* is Task Manager's **Memory** column.

| Phase | CPU | Private | Working set |
|---|---|---|---|
| Startup (first 15 s) | 1.1 % | 58.5 MB | 92.9 MB |
| Preview visible, 10 fps | 0.9 % | 58.4 MB | 92.8 MB |
| Visible + motion detection | 1.0 % | 58.3 MB | 94.1 MB |
| Hidden (camera open, default) | 0.34 % | 58.1 MB | 92.7 MB |
| Hidden, camera released (opt-in) | 0.00 % | 54.3 MB | 90.6 MB |
| After 30 min visible (+ one camera reopen beforehand) | 1.0 % | 69.0 MB | 100.4 MB |
| Built `DeskGuard.exe`, 20 s after start | — | 53.6 MB | 89.6 MB |

The 30-minute row ran in the same process right after the opt-in
camera-off test, so it includes one camera reopen (≈6 MB, see *Known
limitations*). The remaining ≈5 MB growth over 30 minutes is allocator and
driver noise; a longer soak is needed to rule out a slow leak.

Version 1.0.0 on the same machine used **435 MB** private memory: OpenBLAS and
OpenCV each started a thread per CPU core with its own buffers. Version 1.1.0
limits them to one thread, which cuts private memory by about 87 %. The camera
also opens in under 1 s (previously 2.5–3.5 s), and keyboard focus stayed with
the foreground app (checked with `GetForegroundWindow`). The one-folder build
shrank from 184 MB to 152 MB.

## 7. Known limitations

- **Watch my back** needs reasonable light and a mostly frontal face to recognise faces. Someone
  standing in profile is caught only by the "person without a visible face" check.
- That check can raise a false alert when something large is moved behind you (a chair, a door left
  open), until it becomes part of the background after about a minute.
- Your body area follows your face. If you leave and someone else sits in your seat with their face
  hidden, they are treated as you until a face is seen.
- If someone stands directly behind you, most of them is inside your body area; their face (usually
  above yours) is what gets them noticed.

- **RAM:** about 55–60 MB private and 90–95 MB working set. That's inside the
  30–60 MB target for Task Manager's Memory column, but the working set
  includes shared Qt, OpenCV and driver DLL pages, which can't go much lower
  with Python + Qt + OpenCV.
- **DirectShow leak:** every time a camera is opened or reconfigured,
  DirectShow/OpenCV leaks about 6 MB that is never freed (measured with plain
  OpenCV, no DeskGuard code involved). DeskGuard keeps the camera open,
  configures it once, and never sets FPS. Reopens still happen on pause/resume,
  camera or resolution changes, reconnects, and the opt-in camera-off-when-hidden
  option. Restarting DeskGuard returns the memory.
- **Media Foundation:** the `opencv-python-headless` wheel doesn't ship its
  MSMF plugin, so DirectShow is the only backend in practice.
- **Always on top:** exclusive-fullscreen apps (some games, PowerPoint slideshow),
  the Start menu and admin-elevated windows can still appear above DeskGuard.
  That's a Windows rule.
- **Camera in use:** many webcams allow only one app at a time through
  DirectShow. If Teams or Zoom is using the camera, DeskGuard shows an error
  and retries.
- **Rounded corners** need Windows 11. On Windows 10 the preview is square.
- **Menus:** an open right-click menu takes the keyboard until it closes. That's
  the only time DeskGuard has focus.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| "Can't open …" | Close other camera apps. Then check *Settings › Privacy & security › Camera*: **Camera access** and **Let desktop apps access your camera** must both be on. |
| Black image | Lens cover or privacy shutter closed, or another app is holding the camera. |
| Wrong camera | Settings → Camera → pick it. The choice is remembered by name. |
| 1080p is choppy | Use 640×360. The preview is tiny, so higher resolutions only cost CPU and USB bandwidth. |
| Image is mirrored the wrong way | Right-click → Mirror image (toggle). |
| Motion alerts when I move | Turn on Watch my back, or right-click → Edit ignore zones… and draw over yourself. |
| Alerts about me | Settings → Watch my back → Your face → **Set up again** in your usual light, or set Face matching to *Relaxed*. |
| No alert when someone stands behind me | Check the delay (Settings → Watch my back), that their face is lit, and that "Also detect people whose face isn't visible" is on. |
| Face setup says "Only one face" | Make sure nobody else is in view while you set it up. |
| "This camera has no settings dialog" | The camera isn't using DirectShow, or its driver has no property sheet. |
| Preview lost off-screen | Right-click the tray icon → Settings… → Window & app → Reset, or start with `--reset`. |
| "Already running" | Look for the DeskGuard icon in the tray (it may be in the ^ overflow area). |
