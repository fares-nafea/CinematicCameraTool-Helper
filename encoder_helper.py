#!/usr/bin/env python3
"""CinematicCameraTool Encoder Helper.

A small LOCAL process that turns the PNG frames the Roblox Studio plugin sends it
into a real .mp4 file by running FFmpeg. It is optional and separate from the
plugin: nothing here is embedded in the .rbxm, and nothing is downloaded.

    Studio plugin --localhost HTTP--> this helper --subprocess--> FFmpeg --> .mp4

Security design
  * Binds to 127.0.0.1 only and rejects any Host header that is not a loopback name,
    so a web page cannot reach it through DNS rebinding.
  * Every request except GET /hello must carry the random per-run token
    (header X-CCT-Token). The token is printed once on start-up and never stored.
  * A narrow command set only: HELLO, START_EXPORT, FRAME, FINISH_EXPORT, STATUS,
    CANCEL_EXPORT, plus the three screenshot-pickup commands (CAPTURE_BASELINE,
    CAPTURE_PROBE, CAPTURE_FRAME; see "Screenshot pickup" below), and AUX, CAPTURE_AUX and
    COMPOSE for transitions (see "Transitions" below). There is no "run this command" request. The only program ever
    executed is FFmpeg (and FFprobe to check the result), with a FIXED argument list
    built from validated numbers; arguments are passed as a list, never to a shell.
  * Every field is validated (FPS, frame count, size, session id, name, frame order,
    PNG signature / IHDR size / IEND), sizes are capped, and the output path is chosen
    here (inside --output-dir, ".mp4" forced, never overwriting), not by the client.

Frames are streamed to a temporary directory owned by this helper, one PNG per
request, and removed after the export (or on cancel / failure; if removal fails the
directory path is reported).

Transitions
  FADE and CROSSFADE are screen overlays in the plugin's Preview, so a screenshot of the viewport
  never contains them. For the frames inside a transition the plugin sends the frame as usual,
  then (crossfade only) a second picture of the "other" view (AUX / CAPTURE_AUX), then COMPOSE with a
  kind and a weight 0..1. The helper mixes the frame it just stored with black or with that picture:
  picture * (1 - weight) + other * weight, using FFmpeg with a fixed argument structure and only
  validated numbers, exactly like the final encoding.

Screenshot pickup (for a Studio that does not let plugins read their own screenshots)
  Studio's CaptureService writes every screenshot it takes to a temporary PNG file, but
  never hands the pixels to the plugin. In that mode the plugin takes the screenshot and
  only tells this helper "frame k was just taken"; the helper finds the ONE new PNG in the
  captures folder (--captures-dir), validates it and uses it as frame k. The plugin never
  sees a file path or the pixels. Only regular, non-hidden files of that single folder
  that appeared after the baseline are ever read; nothing in it is modified or deleted.

Usage:  python3 encoder_helper.py [--port N] [--output-dir DIR] [--ffmpeg PATH]
                                  [--temp-dir DIR] [--captures-dir DIR]
"""

import argparse
import errno
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PROTOCOL = 1
HOST = "127.0.0.1"
APP_NAME = "CinematicCameraToolEncoder"

ALLOWED_FPS = (24, 30, 60)
MAX_FRAMES = 60 * 600 + 1  # the longest take the plugin can record: 600 s at 60 FPS
MIN_SIZE = 16
MAX_SIZE = 7680  # 8K wide; the plugin only offers what Studio can really capture
MAX_JSON_BYTES = 4096
MAX_FRAME_BYTES = 64 * 1024 * 1024
SESSION_IDLE_SECONDS = 180  # a session this quiet is removed by itself
START_TAKEOVER_SECONDS = 20  # a NEW start may replace a receiving session that has been this quiet
FFMPEG_TIMEOUT_BASE = 600
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SESSION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
CAPTURE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
STDERR_TAIL = 4000
COMPOSE_TIMEOUT = 60  # seconds for mixing ONE frame


class HelperError(Exception):
    """A request the helper refuses; code is machine-readable, message is for the UI."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def default_output_dir():
    home = os.path.expanduser("~")
    system = platform.system()
    if system == "Darwin":
        return os.path.join(home, "Movies", "CinematicCameraTool")
    return os.path.join(home, "Videos", "CinematicCameraTool")


def default_captures_dir():
    """Where Roblox Studio writes its temporary screenshot files (macOS). None elsewhere:
    pass --captures-dir to turn the pickup on."""
    if platform.system() == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Roblox", "tmp-capture-storage")
    return None


def png_info(data):
    """Returns (width, height) of a complete PNG, or raises HelperError."""
    if len(data) < 33 or not data.startswith(PNG_SIGNATURE):
        raise HelperError(400, "InvalidPng", "The frame is not a PNG image.")
    if data[12:16] != b"IHDR":
        raise HelperError(400, "InvalidPng", "The PNG has no header chunk.")
    if not data.endswith(PNG_IEND):
        raise HelperError(400, "InvalidPng", "The PNG is incomplete (no end chunk).")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def find_tool(name, explicit=None, sibling_of=None):
    """Locates ffmpeg / ffprobe. Returns an absolute path or None."""
    if explicit:
        explicit = os.path.abspath(os.path.expanduser(explicit))
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None
    if sibling_of:
        candidate = os.path.join(os.path.dirname(sibling_of), name + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(name)


def ffmpeg_arguments(ffmpeg, fps, output, crop=None):
    """The ONE fixed FFmpeg argument structure. Only numbers that were validated and
    the output path chosen by this helper are inserted; nothing comes from the client.
    `crop` = (width, height) cuts a one-pixel odd edge off the top-left-anchored frames."""
    arguments = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-n",  # never overwrite an existing file
        "-framerate", str(int(fps)),
        "-i", "frame_%06d.png",
    ]
    if crop:
        arguments += ["-vf", "crop=%d:%d:0:0" % (int(crop[0]), int(crop[1]))]
    arguments += [
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output,
    ]
    return arguments


def compose_arguments(ffmpeg, kind, weight, frame, aux, output):
    """The ONE fixed FFmpeg argument structure for mixing a transition into a frame. Only the weight
    (validated, formatted as a number) and file names this helper chose are inserted."""
    arguments = [ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-i", frame]
    if kind == "Crossfade":
        # blend: the first input is the TOP layer, and all_opacity is how much of it is kept
        arguments += ["-i", aux, "-filter_complex", "[0:v][1:v]blend=all_mode=normal:all_opacity=%.6f" % (1.0 - weight)]
    else:
        keep = "%.6f" % (1.0 - weight)
        arguments += ["-vf", "colorchannelmixer=rr=%s:gg=%s:bb=%s" % (keep, keep, keep)]
    arguments += ["-frames:v", "1", output]
    return arguments


class Session:
    def __init__(self, session_id, fps, width, height, frames, name, directory, output):
        self.id = session_id
        self.fps = fps
        self.width = width
        self.height = height
        self.frames = frames
        self.name = name
        self.directory = directory
        self.output = output
        self.next_frame = 0
        self.state = "Receiving"  # Receiving -> Encoding -> Complete | Failed | Cancelled
        self.message = ""
        self.result = None
        self.process = None
        self.last_activity = time.monotonic()
        self.cleanup_error = None
        self.bytes_written = 0
        self.source = None  # (width, height) of the picked-up screenshots, when they differ from the export size
        self.aux = None  # path of the "other" picture of the next crossfade frame, or None


class HelperState:
    """All the rules, with no HTTP in it (so they can be tested directly)."""

    def __init__(self, output_dir, ffmpeg=None, temp_root=None, ffmpeg_timeout=None, captures_dir=None):
        self.output_dir = os.path.abspath(output_dir)
        self.captures_dir = os.path.abspath(captures_dir) if captures_dir else None
        self.capture_seen = None  # file names already used / present at the baseline (None: no baseline)
        self.capture_size = None  # (width, height) of the probe screenshot
        self.ffmpeg = find_tool("ffmpeg", ffmpeg)
        self.ffprobe = find_tool("ffprobe", None, self.ffmpeg) if self.ffmpeg else shutil.which("ffprobe")
        self.temp_root = temp_root or os.path.join(tempfile.gettempdir(), "CinematicCameraTool", "temp")
        self.ffmpeg_timeout = ffmpeg_timeout
        self.lock = threading.RLock()
        self.session = None

    # ----- discovery -----
    def hello(self):
        return {
            "App": APP_NAME,
            "Protocol": PROTOCOL,
            "TokenRequired": True,
            "Ffmpeg": self.ffmpeg is not None,
            "Ffprobe": self.ffprobe is not None,
            "Platform": platform.system(),
            "Fps": list(ALLOWED_FPS),
            "Captures": self.captures_dir is not None and os.path.isdir(self.captures_dir),
            "Compose": self.ffmpeg is not None,
        }

    # ----- helpers -----
    def _require_session(self, session_id):
        if not isinstance(session_id, str) or not SESSION_PATTERN.match(session_id):
            raise HelperError(400, "InvalidSession", "The session id is invalid.")
        session = self.session
        if session is None or session.id != session_id:
            raise HelperError(404, "UnknownSession", "There is no such export session.")
        session.last_activity = time.monotonic()
        return session

    @staticmethod
    def _int(value, name, low, high):
        if isinstance(value, bool) or not isinstance(value, int) or value < low or value > high:
            raise HelperError(400, "InvalidArgument", "%s must be a whole number from %d to %d." % (name, low, high))
        return value

    def _cleanup(self, session):
        if session.directory and os.path.isdir(session.directory):
            try:
                shutil.rmtree(session.directory)
            except OSError as error:
                session.cleanup_error = "%s (%s)" % (session.directory, error)
        return session.cleanup_error

    # ----- commands -----
    def start_export(self, request):
        with self.lock:
            # A plugin that lost its connection mid-export leaves a half-finished session
            # behind; a live export sends a frame every few seconds at most.
            self.expire_idle(START_TAKEOVER_SECONDS)
            if self.session is not None and self.session.state in ("Receiving", "Encoding"):
                raise HelperError(409, "Busy", "An export is already running.")
            if self.ffmpeg is None:
                raise HelperError(503, "FfmpegMissing", "FFmpeg was not found on this computer.")
            if not isinstance(request, dict):
                raise HelperError(400, "InvalidArgument", "The request must be a JSON object.")
            fps = self._int(request.get("Fps"), "Fps", 1, 240)
            if fps not in ALLOWED_FPS:
                raise HelperError(400, "InvalidArgument", "Fps must be 24, 30 or 60.")
            width = self._int(request.get("Width"), "Width", MIN_SIZE, MAX_SIZE)
            height = self._int(request.get("Height"), "Height", MIN_SIZE, MAX_SIZE)
            if width % 2 or height % 2:
                raise HelperError(400, "InvalidArgument", "Width and Height must be even numbers (H.264 / yuv420p).")
            frames = self._int(request.get("Frames"), "Frames", 1, MAX_FRAMES)
            source = None
            if request.get("Captures") is True:
                if self.capture_size is None or self.capture_seen is None:
                    raise HelperError(409, "NoProbe", "Take the probe screenshot first.")
                source = self.capture_size
                if source[0] - width not in (0, 1) or source[1] - height not in (0, 1):
                    raise HelperError(400, "SizeMismatch", "The screenshots are %dx%d but the export is %dx%d." % (source[0], source[1], width, height))
                if source == (width, height):
                    source = None
            name = request.get("Name", "cinematic")
            if not isinstance(name, str) or not NAME_PATTERN.match(name):
                raise HelperError(400, "InvalidArgument", "Name may only use letters, digits, '_' and '-'.")
            session_id = uuid.uuid4().hex
            try:
                os.makedirs(self.output_dir, exist_ok=True)
                directory = os.path.join(self.temp_root, "export-" + session_id)
                os.makedirs(directory)
            except OSError as error:
                raise HelperError(500, "OutputUnavailable", "Cannot create the output or temporary folder: %s" % error)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            output = os.path.join(self.output_dir, "%s-%s.mp4" % (name, stamp))
            counter = 1
            while os.path.exists(output):
                counter += 1
                output = os.path.join(self.output_dir, "%s-%s-%d.mp4" % (name, stamp, counter))
            self.session = Session(session_id, fps, width, height, frames, name, directory, output)
            self.session.source = source
            return {"Session": session_id, "NextFrame": 0}

    def add_frame(self, session_id, index_text, timestamp_text, data):
        with self.lock:
            session = self._require_session(session_id)
            if session.state != "Receiving":
                raise HelperError(409, "WrongState", "The export is not accepting frames.")
            try:
                index = int(index_text)
            except (TypeError, ValueError):
                raise HelperError(400, "InvalidArgument", "The frame index is not a number.")
            try:
                timestamp = float(timestamp_text)
            except (TypeError, ValueError):
                raise HelperError(400, "InvalidArgument", "The frame timestamp is not a number.")
            if timestamp != timestamp or timestamp < 0 or timestamp > 3600:
                raise HelperError(400, "InvalidArgument", "The frame timestamp is out of range.")
            if index != session.next_frame:
                raise HelperError(409, "FrameOrder", "Expected frame %d but received %d." % (session.next_frame, index))
            if index >= session.frames:
                raise HelperError(409, "TooManyFrames", "More frames than announced.")
            width, height = png_info(data)
            if width != session.width or height != session.height:
                raise HelperError(400, "SizeMismatch", "The frame is %dx%d but the export is %dx%d." % (width, height, session.width, session.height))
            return self._store_frame(session, index, data)

    def _store_frame(self, session, index, data):
        path = os.path.join(session.directory, "frame_%06d.png" % index)
        try:
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError as error:
            if error.errno == errno.ENOSPC:
                self._fail(session, "DiskFull", "The disk is full.")
                raise HelperError(507, "DiskFull", "The disk is full; the export was stopped and cleaned up.")
            self._fail(session, "WriteFailed", "Cannot write a frame: %s" % error)
            raise HelperError(500, "WriteFailed", "Cannot write a frame: %s" % error)
        session.next_frame += 1
        session.bytes_written += len(data)
        return {"Received": session.next_frame}

    # ----- transitions -----
    def _expected_size(self, session):
        return session.source or (session.width, session.height)

    def _write_aux(self, session, data):
        path = os.path.join(session.directory, "aux.png")
        try:
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError as error:
            self._fail(session, "WriteFailed", "Cannot write a picture: %s" % error)
            raise HelperError(500, "WriteFailed", "Cannot write a picture: %s" % error)
        session.aux = path
        session.bytes_written += len(data)
        return {"Aux": True}

    def add_aux(self, session_id, data):
        """The "other" view of the next crossfade frame, sent as PNG bytes."""
        with self.lock:
            session = self._require_session(session_id)
            if session.state != "Receiving":
                raise HelperError(409, "WrongState", "The export is not accepting pictures.")
            width, height = png_info(data)
            expected = self._expected_size(session)
            if (width, height) != expected:
                raise HelperError(400, "SizeMismatch", "The picture is %dx%d but the export expects %dx%d." % (width, height, expected[0], expected[1]))
            return self._write_aux(session, data)

    def compose(self, request):
        """Mixes the frame that was just stored with black (Fade) or with the aux picture (Crossfade):
        picture * (1 - weight) + other * weight."""
        with self.lock:
            if not isinstance(request, dict):
                raise HelperError(400, "InvalidArgument", "The request must be a JSON object.")
            session = self._require_session(request.get("Session"))
            if session.state != "Receiving":
                raise HelperError(409, "WrongState", "The export is not accepting frames.")
            index = request.get("Frame")
            if isinstance(index, bool) or not isinstance(index, int):
                raise HelperError(400, "InvalidArgument", "The frame index is not a number.")
            kind = request.get("Kind")
            if kind not in ("Fade", "Crossfade"):
                raise HelperError(400, "InvalidArgument", "Kind must be Fade or Crossfade.")
            weight = request.get("Weight")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight != weight or weight < 0 or weight > 1:
                raise HelperError(400, "InvalidArgument", "Weight must be a number from 0 to 1.")
            if session.next_frame == 0 or index != session.next_frame - 1:
                raise HelperError(409, "FrameOrder", "Only the frame that was just stored can be mixed (frame %d)." % (session.next_frame - 1))
            if self.ffmpeg is None:
                raise HelperError(503, "FfmpegMissing", "FFmpeg was not found on this computer.")
            aux = session.aux
            session.aux = None  # a picture serves one frame only
            try:
                if kind == "Crossfade" and (aux is None or not os.path.isfile(aux)):
                    raise HelperError(409, "NoAux", "A crossfade needs the other picture first.")
                if weight > 0:
                    self._mix(session, index, kind, float(weight))
            finally:
                if aux and os.path.isfile(aux):
                    try:
                        os.remove(aux)
                    except OSError:
                        pass
            return {"Composed": index}

    def _mix(self, session, index, kind, weight):
        name = "frame_%06d.png" % index
        out = "mix_%06d.png" % index
        out_path = os.path.join(session.directory, out)
        if os.path.exists(out_path):
            os.remove(out_path)
        arguments = compose_arguments(self.ffmpeg, kind, weight, name, "aux.png" if kind == "Crossfade" else None, out)
        try:
            result = subprocess.run(
                arguments,
                cwd=session.directory,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=COMPOSE_TIMEOUT,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._fail(session, "ComposeFailed", "FFmpeg could not mix the transition: %s" % type(error).__name__)
            raise HelperError(500, "ComposeFailed", "FFmpeg could not mix the transition.")
        ok = result.returncode == 0 and os.path.isfile(out_path)
        if ok:
            try:
                with open(out_path, "rb") as handle:
                    width, height = png_info(handle.read(MAX_FRAME_BYTES + 1))
                ok = (width, height) == self._expected_size(session)
            except (OSError, HelperError):
                ok = False
        if not ok:
            tail = (result.stderr or b"")[-STDERR_TAIL:].decode("utf-8", "replace").strip()
            self._fail(session, "ComposeFailed", "FFmpeg could not mix the transition. %s" % tail)
            raise HelperError(500, "ComposeFailed", "FFmpeg could not mix the transition.")
        os.replace(out_path, os.path.join(session.directory, name))

    # ----- screenshot pickup -----
    def _require_captures(self):
        if self.captures_dir is None or not os.path.isdir(self.captures_dir):
            raise HelperError(503, "CapturesUnavailable", "The helper has no screenshot folder to read. Start it with --captures-dir.")

    def capture_baseline(self):
        """Remembers which files are already in the captures folder; only files that appear
        afterwards can ever be used."""
        with self.lock:
            self._require_captures()
            try:
                self.capture_seen = set(os.listdir(self.captures_dir))
            except OSError as error:
                raise HelperError(503, "CapturesUnavailable", "Cannot read the screenshot folder: %s" % error)
            self.capture_size = None
            return {"Ready": True, "Existing": len(self.capture_seen)}

    def _take_new_capture(self):
        """Returns (name, bytes, width, height) of the ONE new, complete PNG in the captures
        folder, and marks it as used. 409 NoNewCapture when there is none yet (the plugin
        asks again), 409 AmbiguousCapture when there are several (something else took a
        screenshot, so it is not certain which one is ours)."""
        self._require_captures()
        if self.capture_seen is None:
            raise HelperError(409, "NoBaseline", "No screenshot baseline was taken.")
        try:
            names = os.listdir(self.captures_dir)
        except OSError as error:
            raise HelperError(503, "CapturesUnavailable", "Cannot read the screenshot folder: %s" % error)
        found = []
        for name in names:
            if name in self.capture_seen or not CAPTURE_NAME_PATTERN.match(name):
                continue
            path = os.path.join(self.captures_dir, name)
            try:
                info = os.lstat(path)  # lstat: a symbolic link is never followed
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size < 33 or info.st_size > MAX_FRAME_BYTES:
                continue
            try:
                with open(path, "rb") as handle:
                    data = handle.read(MAX_FRAME_BYTES + 1)
                width, height = png_info(data)
            except (OSError, HelperError):
                continue  # still being written, or not a PNG: not ready
            found.append((name, data, width, height))
        if not found:
            raise HelperError(409, "NoNewCapture", "No new screenshot has appeared yet.")
        if len(found) > 1:
            for item in found:
                self.capture_seen.add(item[0])
            raise HelperError(409, "AmbiguousCapture", "More than one new screenshot appeared; another screenshot was taken during the export.")
        self.capture_seen.add(found[0][0])
        return found[0]

    def capture_probe(self):
        """Uses the newest screenshot only to learn the viewport size (it is not a frame)."""
        with self.lock:
            _, _, width, height = self._take_new_capture()
            self.capture_size = (width, height)
            return {"Width": width, "Height": height}

    def capture_frame(self, request):
        with self.lock:
            if not isinstance(request, dict):
                raise HelperError(400, "InvalidArgument", "The request must be a JSON object.")
            session = self._require_session(request.get("Session"))
            if session.state != "Receiving":
                raise HelperError(409, "WrongState", "The export is not accepting frames.")
            index = request.get("Frame")
            if isinstance(index, bool) or not isinstance(index, int):
                raise HelperError(400, "InvalidArgument", "The frame index is not a number.")
            timestamp = request.get("Time")
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or timestamp != timestamp or timestamp < 0 or timestamp > 3600:
                raise HelperError(400, "InvalidArgument", "The frame timestamp is out of range.")
            if index != session.next_frame:
                raise HelperError(409, "FrameOrder", "Expected frame %d but received %d." % (session.next_frame, index))
            if index >= session.frames:
                raise HelperError(409, "TooManyFrames", "More frames than announced.")
            if session.source is None and self.capture_size is None:
                raise HelperError(409, "NoProbe", "Take the probe screenshot first.")
            _, data, width, height = self._take_new_capture()
            expected = session.source or (session.width, session.height)
            if (width, height) != expected:
                self._fail(session, "SizeMismatch", "The screenshot is %dx%d but the export expects %dx%d (was the Studio viewport resized?)." % (width, height, expected[0], expected[1]))
                raise HelperError(400, "SizeMismatch", "The screenshot is %dx%d but the export expects %dx%d (was the Studio viewport resized?). The export was stopped and cleaned up." % (width, height, expected[0], expected[1]))
            return self._store_frame(session, index, data)

    def capture_aux(self, request):
        """Like CAPTURE_FRAME, but the new screenshot is the "other" picture of a crossfade frame."""
        with self.lock:
            if not isinstance(request, dict):
                raise HelperError(400, "InvalidArgument", "The request must be a JSON object.")
            session = self._require_session(request.get("Session"))
            if session.state != "Receiving":
                raise HelperError(409, "WrongState", "The export is not accepting pictures.")
            if session.source is None and self.capture_size is None:
                raise HelperError(409, "NoProbe", "Take the probe screenshot first.")
            _, data, width, height = self._take_new_capture()
            expected = self._expected_size(session)
            if (width, height) != expected:
                self._fail(session, "SizeMismatch", "The screenshot is %dx%d but the export expects %dx%d (was the Studio viewport resized?)." % (width, height, expected[0], expected[1]))
                raise HelperError(400, "SizeMismatch", "The screenshot is %dx%d but the export expects %dx%d (was the Studio viewport resized?). The export was stopped and cleaned up." % (width, height, expected[0], expected[1]))
            return self._write_aux(session, data)

    def finish_export(self, session_id):
        with self.lock:
            session = self._require_session(session_id)
            if session.state != "Receiving":
                raise HelperError(409, "WrongState", "The export cannot be finished from state %s." % session.state)
            if session.next_frame != session.frames:
                raise HelperError(409, "FrameCount", "Received %d of %d frames." % (session.next_frame, session.frames))
            session.state = "Encoding"
            threading.Thread(target=self._encode, args=(session,), daemon=True).start()
            return {"State": "Encoding"}

    def status(self, session_id):
        with self.lock:
            session = self._require_session(session_id)
            payload = {
                "State": session.state,
                "Received": session.next_frame,
                "Frames": session.frames,
                "Message": session.message,
            }
            if session.state == "Complete":
                payload["Output"] = session.output
            if session.state in ("Complete", "Failed") and session.result is not None:
                payload["Result"] = session.result
            if session.cleanup_error:
                payload["TempLeftBehind"] = session.cleanup_error
            return payload

    def cancel_export(self, session_id):
        with self.lock:
            session = self._require_session(session_id)
            if session.state in ("Complete", "Failed", "Cancelled"):
                return {"State": session.state}
            session.state = "Cancelled"
            session.message = "Cancelled."
            process = session.process
        if process is not None:
            self._kill(process)
        with self.lock:
            leftover = self._cleanup(session)
            payload = {"State": "Cancelled"}
            if leftover:
                payload["TempLeftBehind"] = leftover
            return payload

    def expire_idle(self, limit=None):
        """A session that went quiet (plugin crashed / unloaded) must not keep frames."""
        with self.lock:
            session = self.session
            if limit is None:
                limit = SESSION_IDLE_SECONDS
            if session and session.state == "Receiving" and time.monotonic() - session.last_activity > limit:
                session.state = "Cancelled"
                session.message = "The plugin stopped sending frames."
                self._cleanup(session)

    # ----- encoding -----
    @staticmethod
    def _kill(process):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except Exception:
            pass
        for pipe in (process.stderr, process.stdout, process.stdin):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass

    def _fail(self, session, code, message):
        session.state = "Failed"
        session.message = message
        session.result = {"Error": code}
        self._cleanup(session)

    def _encode(self, session):
        crop = (session.width, session.height) if session.source else None
        arguments = ffmpeg_arguments(self.ffmpeg, session.fps, session.output, crop)
        timeout = self.ffmpeg_timeout or (FFMPEG_TIMEOUT_BASE + session.frames)
        try:
            process = subprocess.Popen(
                arguments,
                cwd=session.directory,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as error:
            with self.lock:
                self._fail(session, "FfmpegFailed", "FFmpeg could not be started: %s" % error)
            return
        with self.lock:
            if session.state == "Cancelled":
                self._kill(process)
                return
            session.process = process
        try:
            _, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(process)
            with self.lock:
                if session.state != "Cancelled":
                    self._fail(session, "FfmpegTimeout", "FFmpeg took too long and was stopped.")
            return
        with self.lock:
            if session.state == "Cancelled":
                return
            if process.returncode != 0:
                tail = (stderr or b"")[-STDERR_TAIL:].decode("utf-8", "replace").strip()
                self._remove_output(session)
                self._fail(session, "FfmpegFailed", "FFmpeg failed (exit code %d). %s" % (process.returncode, tail))
                return
        verdict = self._validate_output(session)
        with self.lock:
            if session.state == "Cancelled":
                return
            if verdict.get("Error"):
                self._remove_output(session)
                self._fail(session, verdict["Error"], verdict["Message"])
                return
            session.result = verdict
            session.state = "Complete"
            session.message = "MP4 export complete."
            self._cleanup(session)

    @staticmethod
    def _remove_output(session):
        try:
            if os.path.isfile(session.output):
                os.remove(session.output)
        except OSError:
            pass

    def _validate_output(self, session):
        """Success means: the file exists, is not empty, is an MP4 container, and (when
        FFprobe is available) has the video stream, frame rate and duration requested."""
        path = session.output
        if not path.lower().endswith(".mp4"):
            return {"Error": "InvalidOutput", "Message": "The output is not an .mp4 file."}
        if not os.path.isfile(path):
            return {"Error": "OutputMissing", "Message": "FFmpeg finished but no output file exists."}
        size = os.path.getsize(path)
        if size <= 0:
            return {"Error": "OutputEmpty", "Message": "The output file is empty."}
        with open(path, "rb") as handle:
            head = handle.read(12)
        if len(head) < 12 or head[4:8] != b"ftyp":
            return {"Error": "InvalidOutput", "Message": "The output is not a valid MP4 container (no ftyp box)."}
        result = {"Size": size, "Container": "mp4", "Verified": "Basic"}
        if self.ffprobe:
            probe = self._probe(path)
            if probe.get("Error"):
                return probe
            expected = session.frames / float(session.fps)
            if probe["Frames"] is not None and probe["Frames"] != session.frames:
                return {"Error": "InvalidOutput", "Message": "The video has %s frames, expected %d." % (probe["Frames"], session.frames)}
            if abs(probe["Fps"] - session.fps) > 0.01:
                return {"Error": "InvalidOutput", "Message": "The video frame rate is %.3f, expected %d." % (probe["Fps"], session.fps)}
            if probe["Duration"] is not None and abs(probe["Duration"] - expected) > max(0.25, expected * 0.05):
                return {"Error": "InvalidOutput", "Message": "The video lasts %.2fs, expected about %.2fs." % (probe["Duration"], expected)}
            if (probe["Width"], probe["Height"]) != (session.width, session.height):
                return {"Error": "InvalidOutput", "Message": "The video is %dx%d, expected %dx%d." % (probe["Width"], probe["Height"], session.width, session.height)}
            result.update({"Verified": "FFprobe", "Fps": probe["Fps"], "Frames": probe["Frames"], "Duration": probe["Duration"], "Codec": probe["Codec"], "Width": probe["Width"], "Height": probe["Height"]})
        return result

    def _probe(self, path):
        arguments = [
            self.ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=codec_name,width,height,avg_frame_rate,nb_read_frames:format=format_name,duration",
            "-of", "json", path,
        ]
        try:
            completed = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True, timeout=120, shell=False)
            data = json.loads(completed.stdout.decode("utf-8", "replace"))
            stream = data["streams"][0]
            numerator, denominator = stream["avg_frame_rate"].split("/")
            fps = float(numerator) / float(denominator)
            fmt = data.get("format", {})
            if "mp4" not in fmt.get("format_name", ""):
                return {"Error": "InvalidOutput", "Message": "FFprobe says the container is %s, not MP4." % fmt.get("format_name")}
            frames = stream.get("nb_read_frames")
            duration = fmt.get("duration")
            return {
                "Codec": stream.get("codec_name"),
                "Width": int(stream["width"]),
                "Height": int(stream["height"]),
                "Fps": fps,
                "Frames": int(frames) if frames is not None else None,
                "Duration": float(duration) if duration is not None else None,
            }
        except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError, subprocess.SubprocessError) as error:
            return {"Error": "InvalidOutput", "Message": "FFprobe could not read the video: %s" % error}


class Handler(BaseHTTPRequestHandler):
    server_version = "CinematicCameraToolEncoder/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # keep the console to the useful lines
        if self.server.verbose:
            sys.stderr.write("[helper] " + (format % args) + "\n")

    # ----- plumbing -----
    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, error):
        self._send(error.status, {"Ok": False, "Error": error.code, "Message": error.message})

    def _check_host(self):
        host = self.headers.get("Host", "")
        allowed = ("127.0.0.1:%d" % self.server.port, "localhost:%d" % self.server.port, "127.0.0.1", "localhost")
        if host not in allowed:
            raise HelperError(403, "BadHost", "Only loopback requests are accepted.")

    def _check_token(self):
        given = self.headers.get("X-CCT-Token", "")
        if not hmac.compare_digest(given.encode("utf-8"), self.server.token.encode("utf-8")):
            raise HelperError(401, "BadToken", "The session token is missing or wrong.")

    def _read_body(self, limit):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise HelperError(411, "LengthRequired", "Content-Length is required.")
        if length < 0 or length > limit:
            raise HelperError(413, "TooLarge", "The request body is too large.")
        data = self.rfile.read(length)
        if len(data) != length:
            raise HelperError(400, "ShortBody", "The request body was cut short.")
        return data

    def _json_body(self):
        data = self._read_body(MAX_JSON_BYTES)
        try:
            return json.loads(data.decode("utf-8")) if data else {}
        except (ValueError, UnicodeDecodeError):
            raise HelperError(400, "InvalidJson", "The request body is not valid JSON.")

    # ----- routes -----
    def do_GET(self):
        try:
            self._check_host()
            parsed = urlparse(self.path)
            if parsed.path == "/hello":
                self._send(200, self.server.state.hello())  # no token: reveals nothing sensitive
                return
            self._check_token()
            if parsed.path == "/v1/export/status":
                session = parse_qs(parsed.query).get("session", [""])[0]
                self._send(200, self.server.state.status(session))
                return
            raise HelperError(404, "NotFound", "Unknown request.")
        except HelperError as error:
            self._fail(error)

    def do_POST(self):
        try:
            self._check_host()
            self._check_token()
            state = self.server.state
            path = urlparse(self.path).path
            if path == "/v1/export/start":
                self._send(200, state.start_export(self._json_body()))
            elif path == "/v1/export/frame":
                data = self._read_body(MAX_FRAME_BYTES)
                self._send(200, state.add_frame(
                    self.headers.get("X-CCT-Session", ""),
                    self.headers.get("X-CCT-Frame", ""),
                    self.headers.get("X-CCT-Time", ""),
                    data,
                ))
            elif path == "/v1/captures/baseline":
                self._json_body()
                self._send(200, state.capture_baseline())
            elif path == "/v1/captures/probe":
                self._json_body()
                self._send(200, state.capture_probe())
            elif path == "/v1/export/capture-frame":
                self._send(200, state.capture_frame(self._json_body()))
            elif path == "/v1/export/aux":
                data = self._read_body(MAX_FRAME_BYTES)
                self._send(200, state.add_aux(self.headers.get("X-CCT-Session", ""), data))
            elif path == "/v1/export/capture-aux":
                self._send(200, state.capture_aux(self._json_body()))
            elif path == "/v1/export/compose":
                self._send(200, state.compose(self._json_body()))
            elif path == "/v1/export/finish":
                self._send(200, state.finish_export(self._json_body().get("Session")))
            elif path == "/v1/export/cancel":
                self._send(200, state.cancel_export(self._json_body().get("Session")))
            else:
                raise HelperError(404, "NotFound", "Unknown request.")
        except HelperError as error:
            self._fail(error)
        except Exception as error:  # never leak a traceback to the client, never crash the server
            self._send(500, {"Ok": False, "Error": "Internal", "Message": "Unexpected helper error: %s" % type(error).__name__})


class HelperServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, state, token, verbose=False):
        super().__init__((HOST, port), Handler)  # explicit loopback binding
        self.state = state
        self.token = token
        self.verbose = verbose
        self.port = self.server_address[1]


def make_server(port=0, output_dir=None, ffmpeg=None, temp_root=None, token=None, verbose=False, ffmpeg_timeout=None, captures_dir=None):
    state = HelperState(output_dir or default_output_dir(), ffmpeg, temp_root, ffmpeg_timeout, captures_dir)
    return HelperServer(port, state, token or secrets.token_urlsafe(24), verbose)


def main(argv=None):
    parser = argparse.ArgumentParser(description="CinematicCameraTool Encoder Helper (local MP4 encoder)")
    parser.add_argument("--port", type=int, default=0, help="TCP port on 127.0.0.1 (default: a free one)")
    parser.add_argument("--output-dir", default=None, help="where finished .mp4 files are written")
    parser.add_argument("--ffmpeg", default=None, help="path to the ffmpeg executable (default: found on PATH)")
    parser.add_argument("--temp-dir", default=None, help="where temporary PNG frames are written")
    parser.add_argument("--captures-dir", default=None, help="the folder Studio writes its temporary screenshots to (default on macOS: ~/Library/Roblox/tmp-capture-storage)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be 0-65535")
    captures_dir = args.captures_dir or default_captures_dir()
    server = make_server(args.port, args.output_dir, args.ffmpeg, args.temp_dir, None, args.verbose, None, captures_dir)
    state = server.state
    print("CinematicCameraTool Encoder Helper")
    print("  FFmpeg : %s" % (state.ffmpeg or "NOT FOUND (install FFmpeg or pass --ffmpeg)"))
    print("  FFprobe: %s" % (state.ffprobe or "not found (the MP4 is then only checked for a valid container)"))
    print("  Output : %s" % state.output_dir)
    print("  Shots  : %s" % (state.captures_dir if state.captures_dir and os.path.isdir(state.captures_dir) else "none (screenshot pickup off; pass --captures-dir to turn it on)"))
    print("  Address: %s:%d" % (HOST, server.port))
    print("  Token  : %s" % server.token)
    print("Enter the port and the token in the plugin's EXPORT VIDEO (MP4) section. Press Ctrl+C to stop.")
    print(json.dumps({"event": "ready", "host": HOST, "port": server.port, "token": server.token}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
