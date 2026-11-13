"""Stand-alone GNU Radio Python blocks (also exposed in GNU Radio Companion).

The all-in-one :class:`ntsc_btsc.flowgraph.tv_source` does not use these:
it runs the same encoders on its own threads and feeds GNU Radio through
pipes, which is considerably faster.  These blocks are for building your own
flowgraphs: e.g. the BTSC encoder in front of another transmitter, or the
composite video into a different modulator.
"""

import logging
import queue
import threading
import time

import numpy as np
from gnuradio import gr

from .btsc import BtscEncoder
from .feeders import encoded_frames, queue_source
from .ntsc import NtscEncoder, black_frame, color_bars

log = logging.getLogger("ntsc_btsc")

_POLL = 0.1   # seconds a work() call may wait before returning to the scheduler


class ntsc_video_source(queue_source):
    """NTSC composite at 4fsc (14.318 Msps) from a :class:`MediaReader`.

    Output is the visual carrier envelope (``output="rf"``) or the composite
    in IRE (``output="ire"``).  Frames are encoded on a worker thread, two
    frames ahead, so work() only copies memory.
    """

    def __init__(self, reader, setup=7.5, white_clip=110.0, idle="bars", output="rf",
                 idle_timeout=2.0):
        queue_source.__init__(self, queue.Queue(maxsize=2), np.float32,
                              name="ntsc_video_source")
        self.reader = reader
        self.enc = NtscEncoder(setup=setup, white_clip=white_clip, output=output)
        self.idle = color_bars() if idle == "bars" else black_frame()
        self.idle_timeout = idle_timeout
        self.frames_out = 0
        self._halt = threading.Event()
        self._thread = None

    def start(self):
        self._halt.clear()
        self.reader.start()
        self._thread = threading.Thread(target=self._encode_loop, name="ntsc-encoder",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._halt.set()
        self.reader.stop()
        return True

    def _put(self, item):
        while not self._halt.is_set():
            try:
                self.q.put(item, timeout=_POLL)
                return True
            except queue.Full:
                continue
        return False

    def _encode_loop(self):
        for frame in encoded_frames(self.reader, self.enc, self.idle, self._halt,
                                    self.idle_timeout):
            if not self._put(frame):
                return
            self.frames_out += 1
        self._put(None)

    def work(self, input_items, output_items):
        n = queue_source.work(self, input_items, output_items)
        if self.eof:
            self.reader.video_done = True
        return n


class btsc_encoder(gr.sync_block):
    """BTSC composite generator.

    Inputs: left, right (and SAP if enabled), float audio at 550.699 kHz
    (= 35 x fH, i.e. 4fsc / 26).  Output: aural carrier deviation in Hz.
    """

    def __init__(self, stereo=True, sap=False, gain_db=0.0, sap_gain_db=0.0, block_ms=0.25):
        self.sap = bool(sap)
        gr.sync_block.__init__(self, name="btsc_encoder",
                               in_sig=[np.float32] * (3 if self.sap else 2),
                               out_sig=[np.float32])
        self.enc = BtscEncoder(stereo=stereo, sap=sap, gain_db=gain_db,
                               sap_gain_db=sap_gain_db, block_s=block_ms * 1e-3)

    def work(self, input_items, output_items):
        sap = input_items[2] if self.sap else None
        output_items[0][:] = self.enc.process(input_items[0], input_items[1], sap)
        return len(output_items[0])


class status_probe(threading.Thread):
    """Logs progress every few seconds.  ``source`` needs a ``frames_out``."""

    def __init__(self, reader, source, interval=5.0):
        super().__init__(daemon=True)
        self.reader, self.source, self.interval = reader, source, interval
        self.halt = threading.Event()

    def run(self):
        t0 = time.monotonic()
        while not self.halt.wait(self.interval):
            el = time.monotonic() - t0
            n = self.source.frames_out
            log.info("[%6.1fs] sent %d frames (%.2f fps); %s", el, n, n / el,
                     self.reader.status())
