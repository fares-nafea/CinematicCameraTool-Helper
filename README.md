# CinematicCameraTool Encoder Helper

The plugin's **EXPORT VIDEO (MP4)** section turns a cinematic into a real `.mp4` file. Roblox Studio
plugins cannot encode video or write arbitrary files, so the plugin talks to this small local
program, which runs **FFmpeg**:

```
Studio viewport  ->  PNG frames  ->  plugin  --localhost HTTP-->  encoder helper  -->  FFmpeg  -->  .mp4
```

Nothing here is installed or downloaded by the plugin. You start the helper yourself, when you
want to export, and stop it afterwards.

## What you need

* **Python 3.8 or newer** (standard library only; nothing to `pip install`).
* **FFmpeg** (and ideally **FFprobe**, used to double-check the finished file), installed by you:
  * macOS: `brew install ffmpeg`
  * Windows: `winget install ffmpeg` (or download a build from <https://ffmpeg.org/download.html>)
  * Linux: your package manager, e.g. `sudo apt install ffmpeg`

  The helper finds `ffmpeg` on your `PATH`, or you can point it at one with `--ffmpeg /path/to/ffmpeg`.

## Start it

```
python3 encoder_helper.py          # from the folder you unzipped it into
python3 helper/encoder_helper.py   # from the source repository
```

It prints something like:

```
  FFmpeg : /opt/homebrew/bin/ffmpeg
  Output : /Users/you/Movies/CinematicCameraTool
  Address: 127.0.0.1:53124
  Token  : Qm3...random...
```

In Studio, open the plugin, scroll to **EXPORT VIDEO (MP4)**, type the **Port** and the **Token** from the
console, press **Check Encoder** (it should say *Connected (FFmpeg found)*), choose FPS and resolution,
and press **Export MP4**. The first time, Studio asks to allow the plugin to take viewport screenshots
and to talk to `localhost`; allow both or the export cannot work.

The finished file is written by the helper into its output folder (default `~/Movies/CinematicCameraTool`
on macOS, `~/Videos/CinematicCameraTool` elsewhere; change it with `--output-dir`). The plugin shows the
path the helper reports. The token is new on every start and is never saved by the plugin.

Options: `--port N` (default: a free port), `--output-dir DIR`, `--ffmpeg PATH`, `--temp-dir DIR`, `--captures-dir DIR`, `--verbose`.

## If Studio does not let plugins read screenshots ("screenshot pickup")

Some Studio versions answer the plugin's screenshot permission request with *"Feature not supported yet."*.
Studio's `CaptureService` still takes the screenshot and writes it to a temporary PNG file, but never gives
the pixels to the plugin. In that case the plugin takes the screenshot as usual and only tells the helper
**"frame k was just taken"**; the helper finds the one new PNG in Studio's temporary screenshot folder and uses
it as frame k. The plugin never sees a file path or the picture.

* **Nothing to turn on on macOS**: the helper reads `~/Library/Roblox/tmp-capture-storage` by default and prints
  a `Shots` line when it can. On other systems pass `--captures-dir DIR` (the folder Studio writes its
  temporary screenshots to). Without a folder the pickup is off and the plugin says so.
* The helper only **reads** that folder: regular, non-hidden files that appeared after the export's baseline
  (symbolic links are never followed; the files must be complete PNGs). It never modifies or deletes Roblox's files.
* It takes the **one** new screenshot after each frame. If two appear (you took a screenshot yourself while
  exporting) the export stops with a clear message instead of guessing. Do not resize the Studio window during an
  export: a frame of another size stops it.
* This path can only export the viewport's **own size** (choose **Source**); the plugin cannot rescale Studio's
  screenshots. An odd viewport size (e.g. 1921x1081) is cropped by one pixel row/column, never stretched.
* The folder is not documented by Roblox, so a future Studio may move it; use `--captures-dir` then.
* Roblox keeps its temporary screenshots, so a long export leaves a few hundred MB there until Studio clears it.
* Speed: about 0.7 s per frame (a 10 s clip at 30 FPS takes about 3.5 minutes).

## What an export does (and does not)

* The cinematic is stepped **frame by frame at exact times** (`k / FPS`), not in real time; each frame is a
  screenshot of the Studio 3D viewport without the plugin panel or other UI.
* Frames are sent **one at a time** and acknowledged before the next is captured; the helper writes them to a
  temporary folder, runs FFmpeg once at the end, checks the result (file exists, not empty, valid MP4
  container, and with FFprobe the frame rate / frame count / duration / size), and **deletes the frames**. If a
  folder cannot be removed, the plugin is told its path.
* Resolution is what Studio's viewport really provides: *Source* is the viewport's own size (made even);
  720p / 1080p / 2160p are offered only if the viewport is at least that big **and has the same aspect ratio**.
  Nothing is upscaled or stretched. The 3D view's size is set by the Studio window / viewport size.
* **Transitions are included.** FADE and CROSSFADE are screen overlays in the plugin's Preview, so a screenshot never
  contains them. For the frames inside a transition the plugin asks this helper to mix the frame with black (FADE) or
  with a second picture of the other shot's view (CROSSFADE), using the same timing and blend as the Preview. A crossfade
  frame costs one extra screenshot. This needs this version of the helper: an older `encoder_helper.py` is refused with a
  message when the cinematic has transitions (cinematics without transitions still work).
* No audio.

## Security

* Listens on **127.0.0.1 only** and refuses any request whose `Host` is not a loopback name.
* Every request except the harmless `GET /hello` needs the random per-run **token** (`X-CCT-Token`).
* A **narrow command set**: `START_EXPORT`, `FRAME`, `FINISH_EXPORT`, `STATUS`, `CANCEL_EXPORT` (and, for the pickup, `CAPTURE_BASELINE`, `CAPTURE_PROBE`, `CAPTURE_FRAME`; for transitions, `AUX`, `CAPTURE_AUX`, `COMPOSE`). FFmpeg is also run to mix a transition into one frame, with a fixed argument list and validated numbers only. There is no
  endpoint that runs anything you send. The only programs ever started are FFmpeg and FFprobe, with a **fixed
  argument list** built from validated numbers (never a shell), writing to a file name the helper chooses.
* Everything is validated: FPS (24/30/60), even size, frame count, session id, file name, frame order, PNG
  signature / header size / end chunk, request sizes. It never overwrites an existing file.

## Protocol (for the curious)

| Request | Body |
| --- | --- |
| `GET /hello` | – (no token) |
| `POST /v1/export/start` | `{"Fps":30,"Width":1280,"Height":720,"Frames":91,"Name":"cinematic"}` |
| `POST /v1/export/frame` | PNG bytes; headers `X-CCT-Session`, `X-CCT-Frame`, `X-CCT-Time` |
| `POST /v1/export/finish` | `{"Session":"..."}` |
| `GET /v1/export/status?session=...` | – |
| `POST /v1/export/cancel` | `{"Session":"..."}` |
| `POST /v1/export/aux` | PNG bytes; header `X-CCT-Session` (the other view of a crossfade frame) |
| `POST /v1/export/capture-aux` | `{"Session":"..."}` (pickup: the new screenshot is that picture) |
| `POST /v1/export/compose` | `{"Session":"...","Frame":0,"Kind":"Fade"\|"Crossfade","Weight":0.5}` (mixes the frame just stored) |
| `POST /v1/captures/baseline` | `{}` (pickup: remember the files already in the folder) |
| `POST /v1/captures/probe` | `{}` (pickup: read the viewport size from the new screenshot) |
| `POST /v1/export/capture-frame` | `{"Session":"...","Frame":0,"Time":0}` (pickup: use the new screenshot as that frame) |

## Tests

```
python3 -m unittest discover -s helper -p "test_*.py" -v
```
