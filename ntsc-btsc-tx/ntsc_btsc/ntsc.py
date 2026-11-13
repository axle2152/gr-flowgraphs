"""NTSC-M (SMPTE 170M) composite video encoder at 4 x fsc.

Frames come in as BT.601 limited-range YUV 4:1:1 planes (752 x 480, chroma
188 x 480), which is what ffmpeg is asked to produce.  Each call to
:meth:`NtscEncoder.encode` returns one complete 525-line frame (477 750
samples at 14.318 Msps) with sync, equalising/serrated vertical pulses,
colour burst and the active picture.

Output can be the visual-carrier envelope for negative AM (sync tip = 1.0,
blanking = 0.75, white = 0.125), or the composite signal in IRE units.

Line numbering follows the standard: field 1 = lines 1-263 (half), field 2
starts in the middle of line 263.  The 480 active rows map to lines
23-262 (field 1, odd rows) and 285-524 (field 2, even rows).  Field 1 is
sent first, i.e. the frame is bottom-field-first.
"""

import numpy as np
from scipy import fft as sfft

from . import constants as C

# Levels in IRE
SYNC = -40.0
BURST_AMPL = 20.0                       # 40 IRE peak-to-peak

# Timing (in samples at 4fsc), relative to 0H (50 % point of sync leading edge)
H_SYNC = 4.7 * C.US
EQ_PULSE = 2.3 * C.US
BROAD_PULSE = (C.HALF / C.US - 4.7) * C.US      # H/2 - 4.7 us serration
BURST_START = 5.3 * C.US
BURST_LEN = 9.0 / C.FSC * C.FS                  # 9 cycles = 36 samples
SYNC_EDGE = 3.4                                 # ~140 ns 10-90 % rise
BURST_EDGE = 7.0                                # ~300 ns envelope rise
PICTURE_EDGE = 4                                # samples of taper at picture edges

# I/Q rotation (I leads U by 123 deg; C = I cos(wt + 33) + Q sin(wt + 33))
_S33, _C33 = np.sin(np.radians(33.0)), np.cos(np.radians(33.0))


def _edge(t, t50, width):
    """Raised-cosine rising step (0 -> 1) centred on t50, total width ``width``."""
    x = np.clip((t - t50) / width + 0.5, 0.0, 1.0)
    return 0.5 - 0.5 * np.cos(np.pi * x)


def _add_pulse(buf, start, end, level, width):
    """Add a shaped pulse (absolute sample positions, wraps around the frame)."""
    i0 = int(np.floor(start - width))
    i1 = int(np.ceil(end + width)) + 1
    t = np.arange(i0, i1, dtype=float)
    shape = _edge(t, start, width) - _edge(t, end, width)
    np.add.at(buf, np.arange(i0, i1) % len(buf), level * shape)


def _half_line_plan():
    """List of (line, half_kind_first, half_kind_second, burst) for lines 1..525."""
    plan = {}
    for ln in range(1, 526):
        plan[ln] = ("h", None, True)              # normal line with burst
    for ln in (1, 2, 3, 7, 8, 9):
        plan[ln] = ("eq", "eq", False)
    for ln in (4, 5, 6):
        plan[ln] = ("broad", "broad", False)
    plan[263] = ("h", "eq", True)
    plan[264] = plan[265] = ("eq", "eq", False)
    plan[266] = ("eq", "broad", False)
    plan[267] = plan[268] = ("broad", "broad", False)
    plan[269] = ("broad", "eq", False)
    plan[270] = plan[271] = ("eq", "eq", False)
    plan[272] = ("eq", None, False)
    return plan


def row_to_line():
    """0-based frame line index for each of the 480 active rows."""
    r = np.arange(C.HEIGHT)
    lines = np.where(r % 2 == 1, 23 + r // 2, 285 + r // 2)   # 1-based
    return lines - 1


def raised_cosine(f, f6db, rolloff):
    """Raised-cosine low-pass magnitude: flat below f6db - rolloff/2, zero above
    f6db + rolloff/2, -6 dB at f6db."""
    lo = f6db - rolloff / 2.0
    x = np.clip((np.asarray(f, dtype=float) - lo) / rolloff, 0.0, 1.0)
    return 0.5 + 0.5 * np.cos(np.pi * x)


class ChromaUpsampler:
    """4:1:1 chroma (fs/4) -> 4fsc with separate I and Q band limiting.

    Done in the frequency domain along each line: the edge-padded line is
    transformed, weighted by the I or Q response and zero-padded 4x.
    SMPTE 170M: I < 2 dB down at 1.3 MHz; Q < 2 dB at 0.4 MHz, < 6 dB at
    0.5 MHz and > 6 dB at 0.6 MHz.
    """

    PAD = 16

    def __init__(self, i_bw=(1.6e6, 0.8e6), q_bw=(0.55e6, 0.3e6)):
        self.n_in = C.WIDTH // 4 + 2 * self.PAD
        f = np.fft.rfftfreq(self.n_in, d=4.0 / C.FS)
        h = np.stack([raised_cosine(f, *i_bw), raised_cosine(f, *q_bw)]) * 4.0
        self.h = h[:, None, :].astype(np.complex64)

    def __call__(self, iq):
        p = self.PAD
        cp = np.pad(iq, ((0, 0), (0, 0), (p, p)), mode="edge")
        spec = sfft.rfft(cp, axis=-1)
        spec *= self.h
        out = sfft.irfft(spec, n=4 * self.n_in, axis=-1)
        return out[:, :, 4 * p:4 * p + C.WIDTH]


class NtscEncoder:
    def __init__(self, setup=7.5, white_clip=110.0, black_clip=-25.0,
                 burst=True, output="rf"):
        """
        setup       black level in IRE (7.5 for NTSC-M, 0 for NTSC-J)
        white_clip  composite is clipped above this (keeps the visual carrier
                    from collapsing, which causes intercarrier buzz)
        output      "rf" -> visual carrier envelope, "ire" -> composite in IRE
        """
        self.setup = float(setup)
        self.gain = 100.0 - self.setup
        self.white_clip = float(white_clip)
        self.black_clip = float(black_clip)
        self.burst = bool(burst)
        self.output = output
        self.rows = row_to_line()
        self.upsample = ChromaUpsampler()

        x0 = C.ACTIVE_START
        w = np.ones(C.WIDTH, dtype=np.float32)
        ramp = _edge(np.arange(PICTURE_EDGE, dtype=float), (PICTURE_EDGE - 1) / 2.0, PICTURE_EDGE)
        w[:PICTURE_EDGE] = ramp
        w[-PICTURE_EDGE:] = ramp[::-1]
        self.window = w

        # Templates (sync + blanking + burst) and subcarrier tables for both
        # colour frames (the subcarrier phase flips every frame).
        self.templates = []
        self.tab_i = []
        self.tab_q = []
        for parity in (0, 1):
            tmpl = self._build_template(parity)
            if self.output == "rf":
                tmpl = self.to_rf(tmpl)
            self.templates.append(tmpl)
            n = (parity * C.FRAME + self.rows[:, None] * C.LINE + x0
                 + np.arange(C.WIDTH)[None, :])
            ph = 2.0 * np.pi * (n % 4) / 4.0
            self.tab_i.append(np.cos(ph + np.radians(33.0)).astype(np.float32))
            self.tab_q.append(np.sin(ph + np.radians(33.0)).astype(np.float32))

    # -- frame structure -------------------------------------------------
    def _build_template(self, parity):
        buf = np.zeros(C.FRAME, dtype=float)
        plan = _half_line_plan()
        for ln, (first, second, burst) in plan.items():
            base = (ln - 1) * C.LINE
            for half, kind in ((0, first), (1, second)):
                if kind is None:
                    continue
                o = base + half * C.HALF
                if kind == "h":
                    _add_pulse(buf, o, o + H_SYNC, SYNC, SYNC_EDGE)
                elif kind == "eq":
                    _add_pulse(buf, o, o + EQ_PULSE, SYNC, SYNC_EDGE)
                elif kind == "broad":
                    _add_pulse(buf, o, o + BROAD_PULSE, SYNC, SYNC_EDGE)
            if burst and self.burst:
                i0 = int(np.floor(base + BURST_START - BURST_EDGE))
                i1 = int(np.ceil(base + BURST_START + BURST_LEN + BURST_EDGE)) + 1
                t = np.arange(i0, i1, dtype=float)
                env = (_edge(t, base + BURST_START, BURST_EDGE)
                       - _edge(t, base + BURST_START + BURST_LEN, BURST_EDGE))
                n = parity * C.FRAME + np.arange(i0, i1)
                # burst sits at 180 deg relative to the B-Y (U) axis
                buf[i0:i1] += -BURST_AMPL * env * np.sin(2 * np.pi * (n % 4) / 4.0)
        return buf.astype(np.float32)

    # -- picture ---------------------------------------------------------
    def picture_ire(self, y, cb, cr, parity):
        """Active picture (480 x 752) in IRE for the given colour frame."""
        u = (cb.astype(np.float32) - 128.0) * np.float32(0.492111 * 1.772 / 224.0)
        v = (cr.astype(np.float32) - 128.0) * np.float32(0.877283 * 1.402 / 224.0)
        iq = np.empty((2,) + u.shape, dtype=np.float32)
        iq[0] = v * np.float32(_C33) - u * np.float32(_S33)
        iq[1] = v * np.float32(_S33) + u * np.float32(_C33)
        iq = self.upsample(iq)
        g = np.float32(self.gain / 219.0)
        pic = y.astype(np.float32)
        pic *= g
        pic += np.float32(self.setup - 16.0 * g)
        iq[0] *= self.tab_i[parity]
        iq[1] *= self.tab_q[parity]
        iq *= np.float32(self.gain)
        pic += iq[0]
        pic += iq[1]
        np.clip(pic, self.black_clip, self.white_clip, out=pic)
        pic *= self.window
        return pic

    @staticmethod
    def to_rf(ire):
        """Negative AM envelope: -40 IRE -> 1.0, 0 -> 0.75, 100 -> 0.125 (in place)."""
        ire *= np.float32(-0.00625)
        ire += np.float32(0.75)
        return ire

    def encode(self, y, cb, cr, parity):
        """Encode one frame.  Returns a flat float32 array of C.FRAME samples."""
        pic = self.picture_ire(y, cb, cr, parity)
        if self.output == "rf":
            self.to_rf(pic)
        frame = self.templates[parity].copy()
        f2 = frame.reshape(C.LINES, C.LINE)
        f2[self.rows, C.ACTIVE_START:C.ACTIVE_START + C.WIDTH] = pic
        return frame

    @staticmethod
    def split_planes(raw):
        """Split a raw yuv411p frame buffer into (Y, Cb, Cr) arrays."""
        ny = C.WIDTH * C.HEIGHT
        nc = (C.WIDTH // 4) * C.HEIGHT
        y = raw[:ny].reshape(C.HEIGHT, C.WIDTH)
        cb = raw[ny:ny + nc].reshape(C.HEIGHT, C.WIDTH // 4)
        cr = raw[ny + nc:ny + 2 * nc].reshape(C.HEIGHT, C.WIDTH // 4)
        return y, cb, cr


def black_frame():
    """Black picture as yuv411p planes."""
    return (np.full((C.HEIGHT, C.WIDTH), 16, np.uint8),
            np.full((C.HEIGHT, C.WIDTH // 4), 128, np.uint8),
            np.full((C.HEIGHT, C.WIDTH // 4), 128, np.uint8))


def frame_bytes():
    return C.WIDTH * C.HEIGHT + 2 * (C.WIDTH // 4) * C.HEIGHT


def rgb_to_yuv411(rgb):
    """RGB float image (H x W x 3, 0..1) -> BT.601 limited yuv411p planes."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = (b - y) / 1.772
    cr = (r - y) / 1.402
    Y = np.clip(np.round(16 + 219 * y), 0, 255).astype(np.uint8)
    Cb = 128 + 224 * cb
    Cr = 128 + 224 * cr
    h, w = Y.shape
    Cb = Cb.reshape(h, w // 4, 4).mean(axis=2)
    Cr = Cr.reshape(h, w // 4, 4).mean(axis=2)
    return (Y, np.clip(np.round(Cb), 0, 255).astype(np.uint8),
            np.clip(np.round(Cr), 0, 255).astype(np.uint8))


def color_bars(width=C.WIDTH, height=C.HEIGHT):
    """75 % colour bars with a PLUGE-style bottom strip, as yuv411p planes."""
    bars = np.array([[.75, .75, .75], [.75, .75, 0], [0, .75, .75], [0, .75, 0],
                     [.75, 0, .75], [.75, 0, 0], [0, 0, .75]])
    rgb = np.zeros((height, width, 3))
    edges = np.linspace(0, width, 8).astype(int)
    top = int(height * 0.67)
    mid = int(height * 0.75)
    for k in range(7):
        rgb[:top, edges[k]:edges[k + 1]] = bars[k]
    rev = [6, 0, 4, 0, 2, 0, 0]
    for k in range(7):
        rgb[top:mid, edges[k]:edges[k + 1]] = bars[rev[k]] if rev[k] else 0.0
    q = width // 6
    rgb[mid:, 0:q] = [0.0, 0.13, 0.3]            # -I-ish
    rgb[mid:, q:2 * q] = 1.0                     # 100 % white
    rgb[mid:, 2 * q:3 * q] = [0.2, 0.0, 0.42]    # +Q-ish
    rgb[mid:, 3 * q:] = 0.0
    return rgb_to_yuv411(rgb)
