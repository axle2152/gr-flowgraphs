"""Reference receiver: decode an IQ recording back to pictures and stereo audio.

Use it to check the transmitter without any hardware::

    ntsc-btsc-tx -i movie.mp4 --sink file -o tx.cf32 --duration 5
    python -m ntsc_btsc.verify tx.cf32 --png frame.png --wav audio.wav

It demodulates the visual carrier synchronously through a Nyquist-slope
filter (like a TV IF), finds vertical sync, locks chroma to the burst and
decodes a frame to RGB.  The aural carrier is FM demodulated and passed
through a BTSC decoder (pilot recovery, L-R demodulation, dbx-TV expansion,
75 us de-emphasis).
"""

import argparse
import sys

import numpy as np
from scipy import signal

from . import constants as C
from .btsc import BtscDecoder
from .ntsc import row_to_line


class IqFile:
    """Random access to cf32 / cs8 / cs16 IQ recordings without loading them."""

    def __init__(self, path, fmt="cf32"):
        self.fmt = fmt
        if fmt == "cf32":
            self.raw = np.memmap(path, dtype=np.complex64, mode="r")
            self.n = len(self.raw)
        else:
            self.raw = np.memmap(path, dtype=np.int8 if fmt == "cs8" else np.int16, mode="r")
            self.scale = 127.0 if fmt == "cs8" else 32767.0
            self.n = len(self.raw) // 2

    def __len__(self):
        return self.n

    def read(self, start, count):
        start = max(0, start)
        stop = min(self.n, start + count)
        if self.fmt == "cf32":
            return np.array(self.raw[start:stop])
        d = self.raw[2 * start:2 * stop].astype(np.float32) / self.scale
        return (d[0::2] + 1j * d[1::2]).astype(np.complex64)


class RatedIq:
    """4fsc view of a recording made at another sample rate (e.g. 15 Msps).

    Chunks are resampled with overlap and phase-aligned so chunk edges leave
    no trace; positions and counts are in 4fsc samples."""

    PAD = 4096

    def __init__(self, f, rate):
        from .flowgraph import resample_ratio
        self.f = f
        self.interp, self.decim = resample_ratio(rate)   # 4fsc -> rate
        self.n = len(f) * self.decim // self.interp

    def __len__(self):
        return self.n

    def read(self, start, count):
        start = max(0, start)
        count = max(0, min(count, self.n - start))
        if count == 0:
            return np.zeros(0, np.complex64)
        i, d = self.interp, self.decim
        a = max(0, (start * i // d - self.PAD) // i * i)    # input start, multiple of interp
        b = (start + count) * i // d + self.PAD
        x = self.f.read(a, b - a)
        y = signal.resample_poly(x, d, i)                    # rate -> 4fsc
        k = start - a * d // i
        return y[k:k + count].astype(np.complex64)


def load_iq(path, fmt="cf32", start=0, count=None):
    f = IqFile(path, fmt)
    return f.read(start, len(f) if count is None else count)


def nyquist_receiver_taps(fs=C.FS, numtaps=255):
    """TV IF response at baseband: Nyquist slope -0.75..+0.75 MHz, flat to 4.0 MHz,
    trap at 4.5 MHz.  Frequency-sampling design with a Kaiser window."""
    n = 4096
    f = np.fft.fftfreq(n, 1 / fs)
    h = np.clip((f + 0.75e6) / 1.5e6, 0, 1)
    h[f > 4.0e6] = np.clip(1 - (f[f > 4.0e6] - 4.0e6) / 0.3e6, 0, 1)
    imp = np.fft.fftshift(np.fft.ifft(h))
    mid = n // 2
    taps = imp[mid - numtaps // 2: mid + numtaps // 2 + 1] * signal.windows.kaiser(numtaps, 6)
    return taps


def demod_video(iq, channel_offset=0.0):
    """Visual carrier -> composite in IRE (sample aligned with the input)."""
    f_vis = channel_offset - C.VISUAL_TO_CENTER
    n = np.arange(len(iq))
    x = iq * np.exp(-2j * np.pi * f_vis * n / C.FS).astype(np.complex64)
    x = signal.fftconvolve(x, nyquist_receiver_taps(), mode="same")
    # carrier phase from the strongest samples (sync tips)
    mag = np.abs(x)
    tips = mag > np.percentile(mag, 99)
    x = x * np.exp(-1j * np.angle(np.sum(x[tips])))
    env = 2.0 * x.real          # Nyquist slope halves the carrier and DSB region
    tip = np.percentile(env, 99.5)
    # blanking level: the back porch, 8.2-9.0 us after each H sync leading edge
    sync = env > 0.875 * tip
    d = np.diff(sync.astype(np.int8))
    starts = np.flatnonzero(d == 1) + 1
    ends = np.flatnonzero(d == -1) + 1
    ends = ends[np.searchsorted(ends, starts[0]):] if len(starts) else ends
    m = min(len(starts), len(ends))
    hs = starts[:m][((ends[:m] - starts[:m]) > 55) & ((ends[:m] - starts[:m]) < 80)]
    a, b = int(8.2 * C.US), int(9.0 * C.US)
    porch = [env[h + a:h + b] for h in hs[:2000] if h + b < len(env)]
    blank = float(np.median(np.concatenate(porch))) if porch else 0.75 * tip
    return (blank - env) / (tip - blank) * 40.0, tip, blank


def find_frames(ire):
    """Start sample of line 1 (0H) for every complete frame found."""
    low = ire < -20.0
    d = np.diff(low.astype(np.int8))
    starts = np.flatnonzero(d == 1) + 1
    ends = np.flatnonzero(d == -1) + 1
    if len(ends) and len(starts) and ends[0] < starts[0]:
        ends = ends[1:]
    m = min(len(starts), len(ends))
    starts, ends = starts[:m], ends[:m]
    width = ends - starts
    broad = starts[width > 300]
    hs = starts[(width > 55) & (width < 80)]
    frames = []
    k = 0
    while k < len(broad):
        p = broad[k]
        # Field 1's serrations start on a line boundary (line 4), field 2's in
        # mid-line (266.5): look at the phase of the first normal H sync after.
        nxt = hs[hs > p + 5 * C.LINE]
        if len(nxt):
            ph = (nxt[0] - p) % C.LINE
            if min(ph, C.LINE - ph) < 20:
                frames.append(int(p - 3 * C.LINE))
        while k < len(broad) and broad[k] < p + 10 * C.LINE:
            k += 1
    return frames


def decode_frame(ire, start, setup=7.5):
    """Decode one frame starting at ``start`` (line 1, 0H) to RGB (480 x 752 x 3)."""
    k5 = np.array([1, 2, 2, 2, 1], dtype=float) / 8.0       # nulls fsc and 2 fsc at 4fsc
    lines = row_to_line()
    gain = 100.0 - setup
    rgb = np.zeros((C.HEIGHT, C.WIDTH, 3))
    for r, ln in enumerate(lines):
        base = start + ln * C.LINE
        seg = ire[base:base + C.LINE].astype(float)
        if len(seg) < C.LINE:
            break
        n = np.arange(base, base + C.LINE)
        lo = np.convolve(seg, k5, mode="same")
        chroma = seg - lo
        b0 = int(round(5.3 * C.US)) + 6
        burst = np.sum(chroma[b0:b0 + 24] * np.exp(-2j * np.pi * n[b0:b0 + 24] / 4)) * (2 / 24)
        z = chroma * 2 * np.exp(-2j * np.pi * n / 4)
        z = np.convolve(z, k5, mode="same")
        z = np.convolve(z, np.ones(3) / 3, mode="same")
        z = z * (20j / burst) if abs(burst) > 1 else z * 0
        a = slice(C.ACTIVE_START, C.ACTIVE_START + C.WIDTH)
        y = (lo[a] - setup) / gain
        v = z[a].real / gain
        u = -z[a].imag / gain
        bb = y + u / 0.492111
        rr = y + v / 0.877283
        gg = (y - 0.299 * rr - 0.114 * bb) / 0.587
        rgb[r] = np.stack([rr, gg, bb], axis=1)
    return np.clip(rgb, 0, 1)


def demod_aural(iq, channel_offset=0.0, chunk=1 << 22):
    """Aural carrier -> instantaneous deviation (Hz) at the BTSC composite rate.

    ``iq`` may be an array or anything with ``read(start, count)``."""
    f_aur = channel_offset - C.VISUAL_TO_CENTER + C.AURAL_OFFSET
    D = C.BTSC_DECIM
    h = signal.firwin(25 * D | 1, 230e3, fs=C.FS)
    out = []
    tail = np.zeros(len(h) - 1, dtype=np.complex64)
    pos = 0
    phase_n = 0
    chunk -= chunk % D
    while pos < len(iq):
        x = iq.read(pos, chunk) if hasattr(iq, "read") else iq[pos:pos + chunk]
        n = phase_n + np.arange(len(x))
        x = x * np.exp(-2j * np.pi * f_aur * n / C.FS)
        buf = np.concatenate([tail, x])
        y = signal.upfirdn(h, buf, down=D)
        # keep only outputs whose window lies entirely in this buffer
        first = int(np.ceil((len(h) - 1) / D))
        last = (len(buf) - 1) // D
        out.append(y[first:last + 1])
        consumed = (last + 1) * D
        tail = buf[consumed - (len(h) - 1):]
        pos += len(x)
        phase_n += len(x)
    z = np.concatenate(out)
    dev = np.angle(z[1:] * np.conj(z[:-1])) * C.BTSC_RATE / (2 * np.pi)
    return dev


class _Window:
    def __init__(self, f, start, count):
        self.f, self.start, self.n = f, start, max(0, min(count, len(f) - start))

    def __len__(self):
        return self.n

    def read(self, start, count):
        return self.f.read(self.start + start, min(count, self.n - start))


def tone_level(x, f, fs):
    n = np.arange(len(x))
    return 2 * abs(np.mean(x * np.exp(-2j * np.pi * f * n / fs)))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("iq")
    p.add_argument("--format", choices=["cf32", "cs8", "cs16"], default="cf32")
    p.add_argument("--offset", type=float, default=1.75e6,
                   help="channel offset used at TX (default 1.75e6, the transmitter default)")
    p.add_argument("--rate", type=float, default=C.FS,
                   help="sample rate of the recording (default 4fsc = 14318182)")
    p.add_argument("--setup", type=float, default=7.5)
    p.add_argument("--frame", type=int, default=1,
                   help="decode the frame nearest this frame number (29.97 per second)")
    p.add_argument("--audio-start", type=float, default=0.0, help="seconds")
    p.add_argument("--audio-length", type=float, default=None, help="seconds (default: all)")
    p.add_argument("--png", help="write the decoded frame here")
    p.add_argument("--wav", help="write decoded stereo audio here")
    p.add_argument("--sap", action="store_true", help="also decode the SAP channel")
    p.add_argument("--tones", default="1000,400",
                   help="expected left,right test tones for the separation report")
    a = p.parse_args(argv)

    iqf = IqFile(a.iq, a.format)
    if abs(a.rate - C.FS) > 1e-3:
        iqf = RatedIq(iqf, a.rate)
    first = max(0, a.frame - 1) * C.FRAME
    seg = iqf.read(first, 3 * C.FRAME)
    print(f"{len(iqf) / C.FS:.2f} s of IQ; frames {first / C.FRAME:.0f}-{first / C.FRAME + 3:.0f}: "
          f"peak |x| = {np.abs(seg).max():.3f}")

    # ---- video ----
    ire, tip, blank = demod_video(seg, a.offset)
    frames = find_frames(ire)
    print(f"video: blanking/sync-tip ratio {blank / tip:.3f} (ideal 0.750), "
          f"{len(frames)} frame starts found")
    if frames:
        if len(frames) > 1:
            print(f"  frame period {np.diff(frames).mean():.1f} samples (ideal {C.FRAME})")
        st = frames[0]
        burst_pp = []
        for ln in range(30, 40):
            # quadrature amplitude: independent of where the samples fall on the cycle
            b0 = st + ln * C.LINE + 80
            seg = ire[b0:b0 + 28]
            n = np.arange(b0, b0 + 28)
            burst_pp.append(4 * abs(np.mean((seg - seg.mean()) * np.exp(-0.5j * np.pi * n))))
        print(f"  burst {np.mean(burst_pp):.1f} IRE p-p (ideal 40)")
        rgb = decode_frame(ire, st, a.setup)
        if a.png:
            from PIL import Image
            img = Image.fromarray((rgb * 255 + 0.5).astype(np.uint8))
            img.resize((640, 480), Image.LANCZOS).save(a.png)
            print(f"  wrote {a.png}")

    # ---- audio ----
    a0 = int(a.audio_start * C.FS)
    alen = len(iqf) - a0 if a.audio_length is None else int(a.audio_length * C.FS)
    dev = demod_aural(_Window(iqf, a0, alen), a.offset)
    skip = int(0.25 * C.BTSC_RATE)
    dev = dev[skip:] if len(dev) > 2 * skip else dev
    dec = BtscDecoder().decode(dev, sap=a.sap)
    print(f"audio: carrier offset {np.mean(dev):+.1f} Hz, peak deviation "
          f"{np.percentile(np.abs(dev), 99.9) / 1e3:.1f} kHz, pilot {dec['pilot_dev']:.0f} Hz "
          f"deviation (ideal 5000)")
    L, R = dec["left"], dec["right"]
    s0 = int(0.3 * C.BTSC_RATE)
    if a.tones and len(L) > 2 * s0:
        fl, fr = (float(t) for t in a.tones.split(","))
        Ll, Lr = tone_level(L[s0:], fl, C.BTSC_RATE), tone_level(R[s0:], fl, C.BTSC_RATE)
        Rr, Rl = tone_level(R[s0:], fr, C.BTSC_RATE), tone_level(L[s0:], fr, C.BTSC_RATE)
        print(f"  left  {fl:.0f} Hz: L {20 * np.log10(Ll + 1e-12):6.1f} dBFS, leak into R "
              f"{20 * np.log10(Lr + 1e-12):6.1f} dBFS -> separation {20 * np.log10(Ll / max(Lr, 1e-12)):.1f} dB")
        print(f"  right {fr:.0f} Hz: R {20 * np.log10(Rr + 1e-12):6.1f} dBFS, leak into L "
              f"{20 * np.log10(Rl + 1e-12):6.1f} dBFS -> separation {20 * np.log10(Rr / max(Rl, 1e-12)):.1f} dB")
    if a.sap and "sap" in dec:
        s = dec["sap"][s0:]
        f = np.fft.rfftfreq(len(s), 1 / C.BTSC_RATE)
        S = np.abs(np.fft.rfft(s * np.hanning(len(s))))
        print(f"  SAP: strongest tone {f[np.argmax(S)]:.0f} Hz, rms {np.sqrt(np.mean(s ** 2)):.3f}")
    if a.wav:
        from scipy.io import wavfile
        st = np.stack([L, R], axis=1)
        st = signal.resample_poly(st, 1, 11, axis=0)
        wavfile.write(a.wav, int(round(C.BTSC_RATE / 11)),
                      np.clip(st * 32767, -32768, 32767).astype(np.int16))
        print(f"  wrote {a.wav}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
