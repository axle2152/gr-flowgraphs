"""Media input through ffmpeg: files, live network streams and test signals.

One ffmpeg process decodes the input and writes two (or three) raw streams:

* video: 752x480 yuv411p, BT.601 limited range, 29.97 fps, bottom field first,
  on ffmpeg's stdout
* audio: stereo float32 at the BTSC composite rate (550.699 kHz), over a
  loopback TCP connection
* SAP (optional): mono float32 at the same rate, from a second track or input,
  over another loopback TCP connection

Loopback TCP rather than extra pipe file descriptors keeps it to a single
ffmpeg process (needed when ffmpeg itself is the RTMP/RTSP server) on every
OS, including Windows/MSYS2 where pass_fds does not exist.

Files are read with back-pressure (the SDR's sample clock paces ffmpeg), so
audio and video stay in step for any length of programme.  Live sources
(RTMP/RTSP/SRT/UDP...) run free; small jitter buffers absorb the clock
difference between the encoder (e.g. OBS) and the SDR, ffmpeg is restarted
automatically when the stream drops, and an idle picture is shown when there
is no input.
"""

import collections
import json
import logging
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import numpy as np

from . import constants as C
from .ntsc import frame_bytes

log = logging.getLogger("ntsc_btsc.media")

LIVE_SCHEMES = {"rtmp", "rtmps", "rtsp", "rtsps", "srt", "udp", "rtp", "tcp", "zmq"}

TEST_INPUTS = {
    # name: (video source, left tone, right tone)
    "test": "testsrc2=size=960x720:rate=60000/1001",
    "bars": "smptebars=size=640x480:rate=30000/1001",
}


_LIBRTMP = {}


def ffmpeg_uses_librtmp(ffmpeg="ffmpeg"):
    """True if this ffmpeg was built with librtmp (MSYS2's is).  librtmp
    replaces ffmpeg's own RTMP code and can't act as a server (-listen)."""
    if ffmpeg not in _LIBRTMP:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-buildconf"], capture_output=True,
                                 timeout=20).stdout
            _LIBRTMP[ffmpeg] = b"--enable-librtmp" in out
        except Exception:
            _LIBRTMP[ffmpeg] = False
    return _LIBRTMP[ffmpeg]


LIBRTMP_HELP = (
    "this ffmpeg is built with librtmp (MSYS2's is), which can't be an RTMP server, so "
    "--listen can't work with rtmp://.  Use SRT instead - it's built in and OBS supports it:\n"
    "  transmitter: -i \"srt://0.0.0.0:9000?mode=listener\"   (no --listen)\n"
    "  OBS server:  srt://<this PC>:9000?mode=caller        (stream key empty)\n"
    "or run an RTMP server such as MediaMTX and pull from it without --listen.")


def is_live_url(url):
    scheme = urlparse(url).scheme.lower()
    return scheme in LIVE_SCHEMES


def probe_has_audio(url, ffprobe="ffprobe", index=0):
    """True/False if ffprobe can tell, None if it can't (e.g. live input)."""
    exe = shutil.which(ffprobe)
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, "-v", "error", "-select_streams", "a", "-show_entries",
                              "stream=index", "-of", "json", url],
                             capture_output=True, timeout=20, check=True).stdout
        streams = json.loads(out or b"{}").get("streams", [])
        return len(streams) > index
    except Exception:
        return None


class SampleFifo:
    """Thread-safe float32 ring buffer with optional live jitter control."""

    def __init__(self, channels, seconds, rate=C.AUDIO_RATE):
        self.ch = channels
        self.cap = int(seconds * rate)
        self.buf = np.zeros((self.cap, channels), dtype=np.float32)
        self.r = 0
        self.w = 0
        self.cond = threading.Condition()
        self.eof = False
        self.primed = False
        self.underruns = 0
        self.dropped = 0

    def level(self):
        return self.w - self.r

    def write(self, data, block, stop):
        data = np.asarray(data, dtype=np.float32).reshape(-1, self.ch)
        pos = 0
        while pos < len(data):
            with self.cond:
                space = self.cap - (self.w - self.r)
                if space <= 0:
                    if block:
                        self.cond.wait(0.1)
                        if stop.is_set():
                            return
                        continue
                    drop = len(data) - pos
                    self.r += drop
                    self.dropped += drop
                    space = self.cap - (self.w - self.r)
                k = min(space, len(data) - pos)
                i = self.w % self.cap
                k = min(k, self.cap - i)
                self.buf[i:i + k] = data[pos:pos + k]
                self.w += k
                pos += k
                self.cond.notify_all()

    def _read_into(self, out, k):
        done = 0
        while done < k:
            i = self.r % self.cap
            m = min(k - done, self.cap - i)
            out[done:done + m] = self.buf[i:i + m]
            self.r += m
            done += m
        self.cond.notify_all()

    def read_blocking(self, n, timeout):
        """Up to n frames; waits up to ``timeout`` for data.  None at EOF."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.level() < n and not self.eof:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            k = min(n, self.level())
            if k == 0 and self.eof:
                return None
            out = np.empty((k, self.ch), dtype=np.float32)
            self._read_into(out, k)
            return out

    def read_live(self, n, target, slack):
        """Exactly n frames, zero filled on underrun, trimmed on overrun."""
        out = np.zeros((n, self.ch), dtype=np.float32)
        with self.cond:
            lvl = self.level()
            if not self.primed:
                if lvl >= target:
                    self.primed = True
                else:
                    return out
            if lvl > target + slack:
                drop = lvl - target
                self.r += drop
                self.dropped += drop
                lvl = target
            k = min(n, lvl)
            self._read_into(out, k)
            if k < n:
                self.primed = False
                self.underruns += 1
        return out

    def reset(self):
        with self.cond:
            self.r = self.w = 0
            self.primed = False
            self.eof = False


class MediaReader:
    def __init__(self, url, *, live=None, loop=False, listen=False, aspect="pad",
                 interlaced=True, audio=True, sap=None, sap_track=None, latency=0.3,
                 ffmpeg="ffmpeg", input_args=None, loglevel="warning", start=None,
                 priority="normal"):
        self.url = url
        self.test = url in TEST_INPUTS
        self.live = is_live_url(url) or listen if live is None else bool(live)
        self.loop = loop
        self.listen = listen
        self.aspect = aspect
        self.interlaced = interlaced
        self.latency = float(latency)
        self.ffmpeg = shutil.which(ffmpeg) or ffmpeg
        if (listen and urlparse(url).scheme.lower() in ("rtmp", "rtmps")
                and ffmpeg_uses_librtmp(self.ffmpeg)):
            raise ValueError(LIBRTMP_HELP)
        self.priority = priority
        self.input_args = list(input_args or [])
        self.loglevel = loglevel
        self.start_time = start

        if audio and not (self.test or self.live):
            probed = probe_has_audio(url)
            if probed is False:
                log.warning("input has no audio stream; sending silence")
                audio = False
        self.has_audio = bool(audio)
        self.sap_url = sap
        self.sap_track = sap_track
        self.has_sap = sap is not None or sap_track is not None

        self.fsize = frame_bytes()
        self.vq = collections.deque()
        self.vcond = threading.Condition()
        self.v_eof = False
        self.v_primed = False
        self.v_target = max(1, int(round(self.latency * C.FRAME_RATE)))
        self.v_slack = max(2, self.v_target // 2)
        self.v_max = 8 if not self.live else self.v_target + self.v_slack + 30
        self.frames_in = 0
        self.frames_dropped = 0
        self.last_frame_time = 0.0

        fifo_s = 4.0
        self.audio_fifo = SampleFifo(2, fifo_s)
        self.sap_fifo = SampleFifo(1, fifo_s) if self.has_sap else None
        self.a_target = int(self.latency * C.AUDIO_RATE)
        self.a_slack = max(int(0.1 * C.AUDIO_RATE), self.a_target // 2)

        self._stop = threading.Event()
        self._thread = None
        self._procs = []
        self._lock = threading.Lock()
        self.started = False
        self.video_done = False      # set by the video source when it finishes

    # -- ffmpeg command line ------------------------------------------------
    def video_filter(self):
        """Fit to 4:3 (as 752x480 anamorphic), convert to BT.601, interlace.

        One swscale pass does the resize, the colour-matrix conversion (it
        honours BT.709 tags) and the range conversion; frame-rate conversion
        comes after so duplicated frames are not scaled twice.
        """
        W, H = C.WIDTH, C.HEIGHT
        kw = W * 3 // 4          # 4:3 picture: 752 samples wide = 564 * DAR
        kh = H * 4 // 3          # 640 / DAR lines tall at full width
        wide = "gt(dar,4/3)"
        w_fit = f"trunc({kw}*dar/2)*2"
        h_fit = f"trunc({kh}/dar/2)*2"
        if self.aspect == "pad":
            w, h = f"if({wide},{W},{w_fit})", f"if({wide},{h_fit},{H})"
        elif self.aspect == "crop":
            w, h = f"if({wide},{w_fit},{W})", f"if({wide},{H},{h_fit})"
        else:
            w, h = str(W), str(H)
        f = [f"scale=w='{w}':h='{h}':flags=lanczos+accurate_rnd+full_chroma_int:"
             "out_color_matrix=bt601:out_range=tv",
             "format=yuv444p"]
        if self.aspect == "pad":
            f.append(f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
        elif self.aspect == "crop":
            f.append(f"crop={W}:{H}")
        f.append("setsar=1")
        if self.interlaced:
            # 59.94p -> 29.97i, bottom field (odd rows = field 1) from the earlier frame
            f += ["fps=60000/1001", "interlace=scan=bff:lowpass=complex"]
        else:
            f.append("fps=30000/1001")
        f.append("format=yuv411p")
        return ",".join(f)

    def audio_filter(self, channels):
        lay = "stereo" if channels == 2 else "mono"
        return (f"aresample={C.AUDIO_RATE}:async=1:first_pts=0,"
                f"aformat=sample_fmts=flt:channel_layouts={lay}")

    def _input_args(self, url, primary=True):
        a = []
        if self.loop:
            a += ["-stream_loop", "-1"]
        if primary and self.start_time:
            a += ["-ss", str(self.start_time)]
        if url in TEST_INPUTS:
            g = TEST_INPUTS[url] + "[out0];"
            g += ("sine=frequency=1000:sample_rate=48000[l];"
                  "sine=frequency=400:sample_rate=48000[r];"
                  "[l][r]join=inputs=2:channel_layout=stereo[out1]")
            return a + ["-f", "lavfi", "-i", g]
        if url.startswith("test:sap"):
            return a + ["-f", "lavfi", "-i", "sine=frequency=3000:sample_rate=48000"]
        if primary:
            scheme = urlparse(url).scheme.lower()
            if self.live:
                a += ["-fflags", "+nobuffer+discardcorrupt", "-flags", "low_delay",
                      "-probesize", "1000000", "-analyzeduration", "1000000"]
            if self.listen:
                if scheme in ("rtsp", "rtsps"):
                    a += ["-rtsp_flags", "listen"]
                else:
                    a += ["-listen", "1"]
            elif scheme in ("rtsp", "rtsps"):
                a += ["-rtsp_transport", "tcp"]
            a += self.input_args
        return a + ["-i", url]

    def _base(self):
        return [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", self.loglevel]

    def _outputs(self, which, target):
        o = []
        if which == "video":
            o += ["-map", "0:v:0", "-vf", self.video_filter(), "-pix_fmt", "yuv411p",
                  "-f", "rawvideo", target]
        elif which == "audio":
            o += ["-map", "0:a:0", "-af", self.audio_filter(2), "-f", "f32le", target]
        elif which == "sap":
            src = "1:a:0" if self.sap_url is not None else f"0:a:{int(self.sap_track)}"
            o += ["-map", src, "-af", self.audio_filter(1), "-f", "f32le", target]
        return o

    def build_command(self, ports=None):
        """ffmpeg command line.  ``ports`` maps "audio"/"sap" -> loopback TCP port."""
        ports = ports or {}
        cmd = self._base() + self._input_args(self.url)
        if self.sap_url is not None:
            cmd += self._input_args(self.sap_url, primary=False)
        cmd += self._outputs("video", "pipe:1")
        if self.has_audio:
            cmd += self._outputs("audio", f"tcp://127.0.0.1:{ports.get('audio', 0)}")
        if self.has_sap:
            cmd += self._outputs("sap", f"tcp://127.0.0.1:{ports.get('sap', 0)}")
        return cmd

    # -- lifecycle ------------------------------------------------------
    def start(self):
        with self._lock:
            if self.started:
                return
            self.started = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._supervise, name="ffmpeg-supervisor",
                                            daemon=True)
            self._thread.start()

    def stop(self):
        self._kill()
        self._stop.set()
        with self.vcond:
            self.vcond.notify_all()
        for f in (self.audio_fifo, self.sap_fifo):
            if f is not None:
                with f.cond:
                    f.cond.notify_all()

    def _kill(self):
        for p in list(self._procs):
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(0.5)
                    except subprocess.TimeoutExpired:
                        p.kill()
            except Exception:
                pass

    def _supervise(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._run_once()
            except FileNotFoundError:
                log.error("ffmpeg not found (%s); install ffmpeg or pass --ffmpeg", self.ffmpeg)
                break
            except Exception as e:  # pragma: no cover
                log.error("ffmpeg session failed: %s", e)
            if not self.live or self._stop.is_set():
                break
            log.info("input ended; waiting for the stream to come back")
            time.sleep(1.0 if time.monotonic() - t0 > 5 else 2.0)
        with self.vcond:
            self.v_eof = True
            self.vcond.notify_all()
        for f in (self.audio_fifo, self.sap_fifo):
            if f is not None:
                with f.cond:
                    f.eof = True
                    f.cond.notify_all()

    def _run_once(self):
        listeners = {}
        names = (["audio"] if self.has_audio else []) + (["sap"] if self.has_sap else [])
        try:
            for name in names:
                ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                ls.bind(("127.0.0.1", 0))
                ls.listen(1)
                ls.settimeout(0.2)
                listeners[name] = ls
            cmd = self.build_command({n: ls.getsockname()[1] for n, ls in listeners.items()})
            log.debug("running: %s", " ".join(cmd))
            flags = 0
            if self.priority == "high" and hasattr(subprocess, "HIGH_PRIORITY_CLASS"):
                flags = subprocess.HIGH_PRIORITY_CLASS     # Windows doesn't inherit it
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 bufsize=0, creationflags=flags)
            self._procs = [p]
            readers = [threading.Thread(target=self._read_video, args=(p.stdout,), daemon=True)]
            fifos = {"audio": self.audio_fifo, "sap": self.sap_fifo}
            for name, ls in listeners.items():
                readers.append(threading.Thread(target=self._accept_audio,
                                                args=(ls, fifos[name], p), daemon=True))
            for t in readers:
                t.start()
            p.wait()
            for t in readers:
                t.join(2)
        finally:
            self._procs = []
            for ls in listeners.values():
                ls.close()

    # -- reader threads -------------------------------------------------
    def _read_video(self, f):
        try:
            while not self._stop.is_set():
                frame = np.empty(self.fsize, dtype=np.uint8)
                mv = memoryview(frame)
                got = 0
                while got < self.fsize:
                    k = f.readinto(mv[got:])
                    if not k:
                        return
                    got += k
                with self.vcond:
                    while not self.live and len(self.vq) >= self.v_max and not self._stop.is_set():
                        self.vcond.wait(0.1)
                    self.vq.append(frame)
                    self.frames_in += 1
                    self.last_frame_time = time.monotonic()
                    while len(self.vq) > self.v_max:
                        self.vq.popleft()
                        self.frames_dropped += 1
                    self.vcond.notify_all()
        finally:
            f.close()

    def _accept_audio(self, listener, fifo, proc):
        conn = None
        while conn is None:
            if self._stop.is_set() or proc.poll() is not None:
                return
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
        conn.settimeout(None)
        try:
            self._read_audio(conn, fifo)
        finally:
            conn.close()

    def _read_audio(self, conn, fifo):
        buf = bytearray(1 << 16)
        mv = memoryview(buf)
        bpf = 4 * fifo.ch
        have = 0
        while not self._stop.is_set():
            try:
                k = conn.recv_into(mv[have:])
            except OSError:
                return
            if not k:
                return
            have += k
            n = have // bpf * bpf
            if n:
                fifo.write(np.frombuffer(buf, dtype=np.float32, count=n // 4), block=not self.live,
                           stop=self._stop)
                buf[:have - n] = buf[n:have]
                have -= n

    # -- consumer API ---------------------------------------------------
    def next_video_frame(self, timeout=0.2):
        """Next raw yuv411p frame.

        Files: waits up to ``timeout``; returns None on timeout, raises
        EOFError at the end.  Live: never waits; returns None when no new
        frame should be shown (caller repeats the last one or shows idle).
        """
        with self.vcond:
            if not self.live:
                deadline = time.monotonic() + timeout
                while not self.vq and not self.v_eof:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return None
                    self.vcond.wait(left)
                if not self.vq:
                    raise EOFError
                frame = self.vq.popleft()
                self.vcond.notify_all()
                return frame
            q = self.vq
            if not self.v_primed:
                if len(q) >= self.v_target:
                    self.v_primed = True
                else:
                    return None
            if not q:
                self.v_primed = False
                return None
            while len(q) > self.v_target + self.v_slack:
                q.popleft()
                self.frames_dropped += 1
            return q.popleft()

    def read_audio(self, fifo, n, timeout=0.2):
        """n frames from ``fifo``: may return fewer for files, None at EOF."""
        if self.live:
            return fifo.read_live(n, self.a_target, self.a_slack)
        return fifo.read_blocking(n, timeout)

    def seconds_since_frame(self):
        if not self.last_frame_time:
            return float("inf")
        return time.monotonic() - self.last_frame_time

    def status(self):
        a = self.audio_fifo
        return (f"frames in {self.frames_in}, dropped {self.frames_dropped}, "
                f"video queue {len(self.vq)}, audio buffer {a.level() / C.AUDIO_RATE:.2f}s "
                f"(underruns {a.underruns})")


def ffmpeg_available(ffmpeg="ffmpeg"):
    return shutil.which(ffmpeg) is not None


if __name__ == "__main__":  # pragma: no cover - manual debugging
    logging.basicConfig(level=logging.DEBUG)
    r = MediaReader(sys.argv[1] if len(sys.argv) > 1 else "test")
    print(" ".join(r.build_command({"audio": 50000})))
