# DeskGuard

A tiny always-on-top webcam preview for Windows 11. Mount a USB webcam on your
cubicle partition and see who's approaching without turning around.

- 240×150 floating preview, top-right by default. Drag to move, drag the edges to resize.
- It never takes keyboard focus, never shows in the taskbar, and makes no sounds or popups.
- Optional motion detection (off by default) shows a red **MOTION** border.
- Settings live in the right-click menu (on the preview or the tray icon).
- Everything stays private: no recording, no screenshots, no audio, no network. Frames exist only in memory.

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

This creates `.venv`, installs pinned dependencies, runs the tests and builds
`dist\DeskGuard\DeskGuard.exe`.

**Install:** copy the whole `dist\DeskGuard` folder anywhere (for example
`%LOCALAPPDATA%\Programs\DeskGuard`) and run `DeskGuard.exe`. No admin rights
are needed. Use **right-click → Start with Windows** to launch it at sign-in.

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
| Settings | Right-click the preview or the tray icon |

**Status dot** (top-left of the preview, also on the tray icon):

| Colour | Meaning |
|---|---|
| Green | Camera live |
| Amber | Starting or reconnecting |
| Grey | Paused (camera off) |
| Red | Error (the message explains why) |

### Right-click menu

| Menu | Options |
|---|---|
| Camera | Lists cameras by name (listing never opens a camera) |
| Resolution | 640×480 (recommended), 1280×720, 1920×1080 |
| Frame rate | 10 or 15 fps |
| Motion detection | Enabled · Sensitivity Low/Medium/High · Cooldown 3/5/10 s · Keep monitoring while hidden · Show preview on motion |
| Window | Always on top · Fill window (crop) · Opacity 100/85/70/50% · Reset size and position |
| Start with Windows | Per-user startup entry |
| Pause camera | Releases the camera completely (its light turns off) |
| Turn camera off when hidden (after 1 min) | Opt-in; see *While hidden* below |
| Open log folder | `%LOCALAPPDATA%\DeskGuard` |

**While hidden:**
- *Monitoring off (default):* the camera stays open but no frames are read (≈0.3 % CPU).
- *Turn camera off when hidden (opt-in):* after 1 minute hidden the camera is released (light off, 0 % CPU) and reopened (<1 s) when you show the preview. Off by default because each reopen leaks ≈6 MB inside Windows DirectShow (see *Known limitations*).
- *Monitoring on:* motion detection runs at 5 fps. Motion turns the tray icon red for 5 s.
- *Show preview on motion:* the preview also pops up without taking focus, then hides again after 10 s unless you touch it.

### Files

| File | Location |
|---|---|
| Settings | `%APPDATA%\DeskGuard\config.json` |
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
   ├ motion: 160×120 grey diff (if enabled)
   └ resize to window's exact pixel size ──► latest-frame slot (max 1 frame) ──► frame_ready
```

There are two threads in total. If the GUI is busy, frames are replaced, never queued.
OpenCV and OpenBLAS are limited to one thread each (`main.py`), and OpenCL is off.
At 640×480 the camera is used in its default format (YUY2) with no property
changes, so DirectShow builds its capture graph only once.

| Module | Responsibility |
|---|---|
| `main.py` | Logging, single-instance lock, error dialogs, OpenCV/OpenBLAS thread and environment settings |
| `deskguard/app.py` | Wiring: window ↔ worker ↔ tray ↔ menu ↔ settings; show/hide; stall watchdog |
| `deskguard/preview_window.py` | Frameless no-focus window, painting, manual drag/resize, hover buttons |
| `deskguard/camera_worker.py` | Capture loop, frame pacing, reconnect with backoff, black-image check |
| `deskguard/camera.py` | Opening a camera with backend fallback; camera list |
| `deskguard/motion.py` | Motion detector (pure OpenCV/NumPy, no Qt) |
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

There are 33 tests across four files:

| File | What it covers |
|---|---|
| `test_config.py` | Defaults, round-trip, invalid values, corrupt file |
| `test_motion.py` | Noise, entering person, lights switched, slow drift, single-frame glitch, cooldown, sensitivity |
| `test_camera.py` | A fake camera at 30 fps: 10/15 fps pacing, decoding only needed frames, bounded hand-off, hidden idle, opt-in camera release and monitoring modes, DirectShow property changes kept to a minimum, unplug/replug recovery, retry backoff, pause/resume, resolution fallback message, clean release on stop, driver exceptions |
| `test_preview.py` | No-focus window flags, painting, drag/resize/minimum size, buttons, full app start → menu → hide/show → settings saved |

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

Windows 11 Pro, Intel 12th gen, HP Wide Vision HD Camera, 640×480, 10 fps,
measured with `tools\measure.py`. *Private* is Task Manager's **Memory** column.

| Phase | CPU | Private | Working set |
|---|---|---|---|
| Startup (first 15 s) | 1.1 % | 58.5 MB | 92.9 MB |
| Preview visible, 10 fps | 0.9 % | 58.4 MB | 92.8 MB |
| Visible + motion detection | 1.0 % | 58.3 MB | 94.1 MB |
| Hidden (camera open, default) | 0.34 % | 58.1 MB | 92.7 MB |
| Hidden, camera released (opt-in) | 0.00 % | 54.3 MB | 90.6 MB |
| After 30 min visible | *pending* | *pending* | *pending* |
| Built `DeskGuard.exe`, 20 s after start | — | 53.6 MB | 89.6 MB |

Version 1.0.0 on the same machine used **435 MB** private memory: OpenBLAS and
OpenCV each started a thread per CPU core with its own buffers. Version 1.1.0
limits them to one thread, which cuts private memory by about 87 %. The camera
also opens in under 1 s (previously 2.5–3.5 s), and keyboard focus stayed with
the foreground app (checked with `GetForegroundWindow`). The one-folder build
shrank from 184 MB to 152 MB.

## 7. Known limitations

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
| Wrong camera | Right-click → Camera → pick it. The choice is remembered by name. |
| 1080p is choppy | Use 640×480. The preview is tiny, so higher resolutions only cost CPU and USB bandwidth. |
| Preview lost off-screen | Right-click the tray icon → Window → Reset size and position, or start with `--reset`. |
| "Already running" | Look for the DeskGuard icon in the tray (it may be in the ^ overflow area). |
