"""Producer threads that feed GNU Radio through OS pipes.

The NTSC frame encoder and the BTSC encoder are numpy code.  Running them
as GNU Radio Python blocks means every work() call has to win the GIL from
the other Python blocks, which costs more than the DSP itself at 14 Msps.
Instead they run on their own threads, produce large chunks, and write them
into pipes that GNU Radio reads with the C++ ``file_descriptor_source``.
Pipe back-pressure paces them exactly like a block's output buffer would.

If the pipe route can't be used (checked once at start-up with
:func:`pipe_transport_works`, which only matters on Windows), the same
producers hand their chunks to :class:`queue_source`, a plain Python block.
That is slower but works everywhere.
"""

import errno
import logging
import os
import queue
import sys
import threading

import numpy as np
from gnuradio import gr

from .btsc import BtscEncoder
from .ntsc import NtscEncoder, black_frame, color_bars

log = logging.getLogger("ntsc_btsc")

F_SETPIPE_SZ = 1031     # Linux fcntl() command


def make_pipe(size=None):
    """A binary pipe as (read_fd, write_fd), with a buffer of about ``size``.

    Windows: an anonymous pipe made with the requested buffer size (the
    default is only a few KB) whose C-runtime descriptors are switched to
    binary mode - text mode would mangle the float samples.
    Linux: os.pipe() enlarged with F_SETPIPE_SZ.
    """
    if os.name == "nt":
        import _winapi
        import msvcrt
        rh, wh = _winapi.CreatePipe(None, size or 0)
        r = msvcrt.open_osfhandle(rh, os.O_RDONLY)
        w = msvcrt.open_osfhandle(wh, os.O_WRONLY)
        msvcrt.setmode(r, os.O_BINARY)
        msvcrt.setmode(w, os.O_BINARY)
        return r, w
    r, w = os.pipe()
    if size and sys.platform.startswith("linux"):
        import fcntl
        try:
            fcntl.fcntl(w, F_SETPIPE_SZ, size)
        except OSError:
            pass
    return r, w


_PIPE_OK = None


def pipe_transport_works():
    """Can GNU Radio's file_descriptor_source read our pipes?

    Always true on Linux/macOS.  On Windows it depends on GNU Radio and
    Python sharing a C runtime (true for MSYS2 and conda builds), so it is
    tested once with a short known byte pattern - including CR, LF and
    Ctrl-Z, which text mode would corrupt.
    """
    global _PIPE_OK
    if _PIPE_OK is None:
        if os.name != "nt":
            _PIPE_OK = True
        else:
            _PIPE_OK = _probe_pipe()
            if not _PIPE_OK:
                log.warning("GNU Radio can't read Python pipes here; using the slower "
                            "Python-block transport")
    return _PIPE_OK


def _probe_pipe():
    from gnuradio import blocks
    try:
        data = np.frombuffer(bytes(range(256)) * 64, dtype=np.float32)
        r, w = make_pipe(1 << 16)
        os.write(w, data.tobytes())
        os.close(w)
        tb = gr.top_block()
        src = blocks.file_descriptor_source(gr.sizeof_float, r, False)
        snk = blocks.vector_sink_f()
        tb.connect(src, snk)
        tb.run()
        got = np.array(snk.data(), dtype=np.float32)
        return got.tobytes() == data.tobytes()
    except Exception as e:
        log.debug("pipe probe failed: %s", e)
        return False


class queue_source(gr.sync_block):
    """Python source block streaming numpy chunks from a queue.

    A ``None`` in the queue ends the stream.  work() never waits longer than
    0.1 s so the flowgraph can always be stopped.
    """

    def __init__(self, q, dtype=np.float32, name="queue_source"):
        gr.sync_block.__init__(self, name=name, in_sig=None, out_sig=[dtype])
        self.q = q
        self.cur = None
        self.pos = 0
        self.eof = False

    def work(self, input_items, output_items):
        out = output_items[0]
        n = len(out)
        i = 0
        while i < n:
            if self.cur is None or self.pos >= len(self.cur):
                if self.eof:
                    return i if i else -1
                try:
                    chunk = self.q.get(timeout=0.1)
                except queue.Empty:
                    return i
                if chunk is None:
                    self.eof = True
                    return i if i else -1
                self.cur, self.pos = chunk, 0
            k = min(n - i, len(self.cur) - self.pos)
            out[i:i + k] = self.cur[self.pos:self.pos + k]
            i += k
            self.pos += k
        return n


def encoded_frames(reader, enc, idle_planes, halt, idle_timeout=2.0):
    """Yield encoded frames (alternating colour-frame parity) until EOF.

    Files: one frame per decoded frame.  Live: always yields at the pace the
    caller consumes - the newest frame, the last frame again when nothing new
    arrived, or the idle picture once the input has been gone for a while.
    """
    last = None
    parity = 0
    while not halt.is_set():
        try:
            raw = reader.next_video_frame(timeout=0.1)
        except EOFError:
            return
        if raw is not None:
            last = enc.split_planes(raw)
            planes = last
        elif not reader.live:
            continue
        elif last is not None and reader.seconds_since_frame() < idle_timeout:
            planes = last
        else:
            planes = idle_planes
        yield enc.encode(*planes, parity)
        parity ^= 1


class _Feeder:
    pipe_size = None
    queue_depth = 4

    def __init__(self, reader, transport="pipe"):
        self.reader = reader
        self.transport = transport
        self.halt = threading.Event()
        self.thread = None
        self._fd_lock = threading.Lock()
        self.read_fd = self.write_fd = -1
        self.q = None
        if transport == "pipe":
            self.read_fd, self.write_fd = make_pipe(self.pipe_size)
        else:
            self.q = queue.Queue(maxsize=self.queue_depth)

    def gr_source(self):
        """The GNU Radio block that delivers this producer's float32 stream."""
        if self.transport == "pipe":
            from gnuradio import blocks
            return blocks.file_descriptor_source(gr.sizeof_float, self.read_fd, False)
        return queue_source(self.q, name=type(self).__name__.lower())

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name=type(self).__name__,
                                           daemon=True)
            self.thread.start()

    def stop(self):
        """Ask the producer to finish.  The producer thread closes its own end
        of the pipe (within ~0.1 s), which ends the stream for GNU Radio;
        closing it from here could race with a write in progress."""
        self.halt.set()
        if self.thread is None or not self.thread.is_alive():
            self._close()

    def _close(self):
        if self.q is not None:
            try:
                self.q.put_nowait(None)
            except queue.Full:
                pass
            return
        with self._fd_lock:
            fd, self.write_fd = self.write_fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    def _write(self, arr):
        if self.q is not None:
            while not self.halt.is_set():
                try:
                    self.q.put(arr, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False
        mv = memoryview(np.ascontiguousarray(arr)).cast("B")
        while len(mv) and not self.halt.is_set():
            try:
                k = os.write(self.write_fd, mv)
            except OSError as e:
                if e.errno in (errno.EPIPE, errno.EBADF, errno.EINVAL):
                    self.halt.set()
                    return False
                raise
            mv = mv[k:]
        return not self.halt.is_set()

    def _run(self):
        try:
            self.produce()
        except Exception:  # pragma: no cover - surfaced to the user
            if not self.halt.is_set():
                log.exception("%s failed", type(self).__name__)
        finally:
            self._close()


class VideoFeeder(_Feeder):
    """Encodes frames and writes the visual carrier envelope (float32).

    Encoding runs up to ``AHEAD`` frames in front of the pipe writer so a
    momentary stall (ffmpeg, garbage collection, the GIL) does not starve
    the SDR.  Together with the pipe that is ~85 ms of cushion, matched by
    the audio path so lip sync is kept.
    """

    pipe_size = 1 << 20
    queue_depth = 1
    AHEAD = 2

    def __init__(self, reader, setup=7.5, white_clip=110.0, idle="bars", output="rf",
                 idle_timeout=2.0, transport="pipe"):
        super().__init__(reader, transport)
        self.enc = NtscEncoder(setup=setup, white_clip=white_clip, output=output)
        self.idle = color_bars() if idle == "bars" else black_frame()
        self.idle_timeout = idle_timeout
        self.frames_out = 0

    def produce(self):
        q = queue.Queue(maxsize=self.AHEAD)
        enc = threading.Thread(target=self._encode, args=(q,), name="ntsc-encoder", daemon=True)
        enc.start()
        try:
            while not self.halt.is_set():
                try:
                    frame = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if frame is None or not self._write(frame):
                    break
                self.frames_out += 1
        finally:
            self.reader.video_done = True
            self.halt.set()

    def _encode(self, q):
        try:
            for frame in encoded_frames(self.reader, self.enc, self.idle, self.halt,
                                        self.idle_timeout):
                while not self.halt.is_set():
                    try:
                        q.put(frame, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except Exception:
            log.exception("video encoder failed")
        finally:
            while not self.halt.is_set():
                try:
                    q.put(None, timeout=0.1)
                    break
                except queue.Full:
                    continue


class AudioFeeder(_Feeder):
    """Runs the BTSC encoder and writes the aural deviation in Hz (float32)."""

    CHUNK = 8192            # 15 ms
    pipe_size = 1 << 17     # ~60 ms, roughly matching the video cushion
    queue_depth = 4

    def __init__(self, reader, stereo=True, gain_db=0.0, sap_gain_db=0.0, transport="pipe"):
        super().__init__(reader, transport)
        self.enc = BtscEncoder(stereo=stereo, sap=reader.has_sap, gain_db=gain_db,
                               sap_gain_db=sap_gain_db)

    def _get(self, fifo, n, ch):
        """Up to n frames (at least one), zeros once a file's audio has ended."""
        if fifo is None:
            return np.zeros((n, ch), np.float32)
        while not self.halt.is_set() and not self.reader.video_done:
            d = self.reader.read_audio(fifo, n, timeout=0.1)
            if d is None:                       # file ended: silence until video ends
                return np.zeros((n, ch), np.float32)
            if len(d):
                return d
        return None

    def _get_exact(self, fifo, n, ch):
        """Exactly n frames (the SAP track has to line up with the main audio)."""
        parts, have = [], 0
        while have < n:
            d = self._get(fifo, n - have, ch)
            if d is None:
                return None
            parts.append(d)
            have += len(d)
        return np.concatenate(parts) if len(parts) > 1 else parts[0]

    def produce(self):
        r = self.reader
        while not self.halt.is_set() and not r.video_done:
            a = self._get(r.audio_fifo if r.has_audio else None, self.CHUNK, 2)
            if a is None:
                break
            sap = None
            if r.has_sap:
                s = self._get_exact(r.sap_fifo, len(a), 1)
                if s is None:
                    break
                sap = s[:, 0]
            dev = self.enc.process(a[:, 0], a[:, 1], sap).astype(np.float32)
            if not self._write(dev):
                break


class lifecycle_sink(gr.sync_block):
    """Starts/stops the media reader and feeders with the flowgraph.

    It hangs off the (low-rate) aural deviation stream and ignores the data;
    it exists so that a hierarchical block gets start/stop notifications.
    """

    def __init__(self, reader, feeders):
        gr.sync_block.__init__(self, name="lifecycle", in_sig=[np.float32], out_sig=None)
        self.reader = reader
        self.feeders = feeders

    def start(self):
        self.reader.start()
        for f in self.feeders:
            f.start()
        return True

    def stop(self):
        for f in self.feeders:
            f.stop()
        self.reader.stop()
        return True

    def work(self, input_items, output_items):
        if self.reader.video_done:
            return -1
        return len(input_items[0])
