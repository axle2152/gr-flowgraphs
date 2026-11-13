"""BTSC (MTS) stereo + SAP composite encoder, after FCC OET Bulletin 60 Rev. A.

Signal levels used throughout this module:

* Audio inputs are floats where 1.0 is digital full scale.
* Channel signals are "fraction of 100 % modulation" of their channel:
  1.0 on the main channel = 25 kHz deviation, 1.0 on the stereo subchannel
  (the dbx-TV encoded L-R) = 50 kHz, 1.0 on the SAP subchannel = 10 kHz
  deviation of the SAP subcarrier.
* The composite produced by :class:`BtscEncoder` is the instantaneous
  frequency deviation of the aural carrier in Hz.

The dbx-TV compressor below is a direct implementation of the OET-60
equations (fixed pre-emphasis F(f), wideband gain control weighted by P(f),
spectral compression S(f, b) controlled through Q(f), overmodulation
protection and band limiting inside the feedback loop).  The analogue
prototypes are mapped to the digital domain with the bilinear transform at
the composite rate (550.7 kHz), where frequency warping in the audio band is
well under 1 %.

The feedback detectors are updated once per short processing block (0.25 ms
by default) so the loop can run with vectorised numpy/scipy code instead of
a per-sample Python loop.  The RMS time constants are 11.4 ms and 34.7 ms, so
the extra loop delay is negligible.
"""

import numpy as np
from scipy import signal

from . import constants as C



def _fast_sosfilt():
    """scipy's private in-place SOS kernel (~10x less call overhead than
    sosfilt(), which matters in the dbx feedback loop), if it exists and
    still behaves the way we use it; otherwise None and the public API."""
    try:
        from scipy.signal._sosfilt import _sosfilt as kernel
        sos = signal.butter(4, 0.2, output="sos")
        x = np.linspace(-1.0, 1.0, 64)
        y = x.reshape(1, -1).copy()
        zi = np.zeros((1, sos.shape[0], 2))
        kernel(sos, y, zi)
        if np.allclose(y[0], signal.sosfilt(sos, x)):
            return kernel
    except Exception:
        pass
    return None


_sosfilt_inplace = _fast_sosfilt()

# --- OET-60 dbx-TV constants ------------------------------------------------
T1 = 34.7e-3        # wideband ERMS time constant
T2 = 11.4e-3        # spectral ERMS time constant
WB_REF = 0.062490   # 0 dB reference of the wideband detector (Fig. 1)
SP_REF = 0.035655   # 0 dB reference of the spectral detector (Fig. 1)
F_SPECTRAL = 20.1e3

A_MIN = 1e-3        # max wideband gain +60 dB (silence)
B_MIN, B_MAX = 1e-4, 1e4

HF_POLE = 100e3     # realisation pole shared by sum and difference paths
TAU_75US = 75e-6


def _w(f_hz):
    return 2.0 * np.pi * f_hz


def _zpk_to_sos(z, p, k, fs):
    zd, pd, kd = signal.bilinear_zpk(np.asarray(z, dtype=complex),
                                     np.asarray(p, dtype=complex), k, fs)
    return signal.zpk2sos(zd, pd, kd)


# Analogue prototypes in zpk form (s in rad/s).  Each is written exactly as
# the OET-60 expression with f in kHz turned into s / (2 pi f0).

def fixed_preemphasis_zpk():
    """F(f) = (jf/0.408 + 1)(jf/2.19 + 1) / ((jf/5.23 + 1)(jf/62.5 + 1))"""
    z = [-_w(408.0), -_w(2190.0)]
    p = [-_w(5230.0), -_w(62500.0)]
    k = (_w(5230.0) * _w(62500.0)) / (_w(408.0) * _w(2190.0))
    return z, p, k


def wideband_weighting_zpk():
    """P(f) = (jf/0.0354) / ((jf/0.0354 + 1)(jf/2.09 + 1))"""
    wa, wb = _w(35.4), _w(2090.0)
    return [0.0], [-wa, -wb], wb


def spectral_weighting_zpk():
    """Q(f) = (jf/5.86)^3 / (((jf/7.66)^2 + jf/7.31 + 1)(jf/26.9 + 1)(jf/3.92 + 1))"""
    wq, wc, wd, we, wf = _w(5860.0), _w(7660.0), _w(7310.0), _w(26900.0), _w(3920.0)
    quad = np.roots([1.0, wc * wc / wd, wc * wc])
    p = list(quad) + [-we, -wf]
    k = wc * wc * we * wf / wq ** 3
    return [0.0, 0.0, 0.0], p, k


def analog_response(zpk, f_hz):
    z, p, k = zpk
    s = 1j * _w(np.asarray(f_hz, dtype=float))
    h = k * np.ones_like(s)
    for zz in z:
        h = h * (s - zz)
    for pp in p:
        h = h / (s - pp)
    return h


def spectral_response(f_hz, b):
    """Analogue S(f, b) of OET-60 (for tests and reference)."""
    x = 1j * np.asarray(f_hz, dtype=float) / F_SPECTRAL
    return (1 + x * (b + 51) / (b + 1)) / (1 + x * (1 + 51 * b) / (b + 1))


def _spectral_coeffs(b, fs):
    """Bilinear first-order section for S(f, b): returns (b0, b1, a1)."""
    wF = _w(F_SPECTRAL)
    wz = wF * (b + 1.0) / (b + 51.0)
    wp = wF * (b + 1.0) / (1.0 + 51.0 * b)
    K = 2.0 * fs
    a0 = 1.0 + K / wp
    return (1.0 + K / wz) / a0, (1.0 - K / wz) / a0, (1.0 - K / wp) / a0


def bandlimit_sos(fs, f_pass, f_stop, ripple_db=0.05, atten_db=65.0):
    return signal.iirdesign(f_pass, f_stop, ripple_db, atten_db, ftype="ellip",
                            output="sos", fs=fs)


def hf_pole_sos(fs, f=HF_POLE):
    return _zpk_to_sos([], [-_w(f)], _w(f), fs)


def preemphasis_75us_sos(fs, f_pole=HF_POLE):
    """(1 + s tau) / (1 + s / w_pole): 75 us pre-emphasis made realisable."""
    return _zpk_to_sos([-1.0 / TAU_75US], [-_w(f_pole)], TAU_75US * _w(f_pole), fs)


def deemphasis_75us_sos(fs):
    return _zpk_to_sos([], [-1.0 / TAU_75US], 1.0 / TAU_75US, fs)


class Sos:
    """Stateful cascaded-biquad filter with low per-call overhead."""

    def __init__(self, sos):
        self.sos = np.ascontiguousarray(sos, dtype=float)
        self.zi = np.zeros((1, self.sos.shape[0], 2))

    def __call__(self, x):
        if _sosfilt_inplace is not None:
            y = np.array(x, dtype=float, order="C", copy=True).reshape(1, -1)
            _sosfilt_inplace(self.sos, y, self.zi)
            return y[0]
        y, z = signal.sosfilt(self.sos, x, zi=self.zi[0])
        self.zi[0] = z
        return y


class DbxCompressor:
    """dbx-TV encoder (OET-60 Figure 1) for the L-R or SAP channel."""

    def __init__(self, fs, bandlimit, block_s=0.25e-3):
        self.fs = float(fs)
        self.F = Sos(_zpk_to_sos(*fixed_preemphasis_zpk(), self.fs))
        self.P = Sos(_zpk_to_sos(*wideband_weighting_zpk(), self.fs))
        self.Q = Sos(_zpk_to_sos(*spectral_weighting_zpk(), self.fs))
        self.BL = Sos(bandlimit)
        self.r1 = np.exp(-1.0 / (T1 * self.fs))
        self.r2 = np.exp(-1.0 / (T2 * self.fs))
        self.block = max(1, int(round(block_s * self.fs)))
        k = np.arange(self.block - 1, -1, -1, dtype=float)
        self._w1 = (1.0 - self.r1) * self.r1 ** k
        self._w2 = (1.0 - self.r2) * self.r2 ** k
        self._r1B = self.r1 ** self.block
        self._r2B = self.r2 ** self.block
        # Start at unity gain / flat spectral weighting.
        self.ms1 = WB_REF ** 2
        self.ms2 = SP_REF ** 2
        self.a = 1.0
        self.b = 1.0
        self.sx = 0.0   # previous input/output of the S(f, b) section
        self.sy = 0.0

    def process(self, x):
        x = np.asarray(x, dtype=float)
        u = self.F(x)
        y = np.empty_like(u)
        B = self.block
        fs = self.fs
        for s in range(0, len(u), B):
            v = u[s:s + B] / self.a                      # wideband gain 1/a
            n = len(v)
            b0, b1, a1 = _spectral_coeffs(self.b, fs)    # spectral S(f, b)
            w, _ = signal.lfilter((b0, b1), (1.0, a1), v,
                                  zi=(b1 * self.sx - a1 * self.sy,))
            self.sx = v[-1]
            self.sy = w[-1]
            np.clip(w, -1.0, 1.0, out=w)                 # overmod protection
            yb = self.BL(w)
            y[s:s + n] = yb
            # Detectors watch the encoder output (feedback compressor).
            p = self.P(yb)
            q = self.Q(yb)
            if n == B:
                self.ms1 = self._r1B * self.ms1 + np.dot(self._w1, p * p)
                self.ms2 = self._r2B * self.ms2 + np.dot(self._w2, q * q)
            else:
                k = np.arange(n - 1, -1, -1, dtype=float)
                self.ms1 = self.r1 ** n * self.ms1 + np.dot((1 - self.r1) * self.r1 ** k, p * p)
                self.ms2 = self.r2 ** n * self.ms2 + np.dot((1 - self.r2) * self.r2 ** k, q * q)
            self.a = max(np.sqrt(self.ms1) / WB_REF, A_MIN)
            self.b = min(max(np.sqrt(self.ms2) / SP_REF, B_MIN), B_MAX)
        return y


class DbxExpander:
    """Complementary dbx-TV decoder (what a TV does), used for verification."""

    def __init__(self, fs, block_s=0.02e-3):
        self.fs = float(fs)
        z, p, k = fixed_preemphasis_zpk()
        self.Finv = Sos(_zpk_to_sos(p, z, 1.0 / k, self.fs))
        self.P = Sos(_zpk_to_sos(*wideband_weighting_zpk(), self.fs))
        self.Q = Sos(_zpk_to_sos(*spectral_weighting_zpk(), self.fs))
        self.r1 = np.exp(-1.0 / (T1 * self.fs))
        self.r2 = np.exp(-1.0 / (T2 * self.fs))
        self.ms1 = WB_REF ** 2
        self.ms2 = SP_REF ** 2
        self.block = max(1, int(round(block_s * self.fs)))
        self.sx = 0.0
        self.sy = 0.0

    def process(self, y):
        y = np.asarray(y, dtype=float)
        p = self.P(y)
        q = self.Q(y)
        ms1, _ = signal.lfilter([1 - self.r1], [1, -self.r1], p * p, zi=[self.r1 * self.ms1])
        ms2, _ = signal.lfilter([1 - self.r2], [1, -self.r2], q * q, zi=[self.r2 * self.ms2])
        self.ms1, self.ms2 = ms1[-1], ms2[-1]
        a = np.maximum(np.sqrt(ms1) / WB_REF, A_MIN)
        b = np.clip(np.sqrt(ms2) / SP_REF, B_MIN, B_MAX)
        v = np.empty_like(y)
        B = self.block
        for s in range(0, len(y), B):
            yb = y[s:s + B]
            # inverse of S(f, b): swap the zero and the pole
            b0, b1, a1 = _spectral_coeffs(float(b[s]), self.fs)
            ib0, ib1, ia1 = 1.0 / b0, a1 / b0, b1 / b0
            vb, _ = signal.lfilter((ib0, ib1), (1.0, ia1), yb,
                                   zi=(ib1 * self.sx - ia1 * self.sy,))
            self.sx = yb[-1]
            self.sy = vb[-1]
            v[s:s + len(yb)] = vb
        u = v * a
        return self.Finv(u)


class BtscEncoder:
    """Builds the BTSC composite (aural carrier deviation in Hz).

    Feed it left/right (and optionally SAP) audio sampled at ``fs``
    (normally :data:`constants.BTSC_RATE`).  The pilot and subcarriers are
    generated from the sample index, so they stay phase locked to the
    video line rate that comes from the same clock.
    """

    def __init__(self, fs=C.BTSC_RATE, stereo=True, sap=False, gain_db=0.0,
                 sap_gain_db=0.0, block_s=0.25e-3, pilot_period=C.BTSC_PILOT_PERIOD):
        self.fs = float(fs)
        self.stereo = bool(stereo)
        self.sap = bool(sap)
        self.gain = 10 ** (gain_db / 20.0)
        self.sap_gain = 10 ** (sap_gain_db / 20.0)
        self.period = int(pilot_period)
        if abs(self.fs / self.period - C.FH) > 0.01:
            raise ValueError("BTSC encoder rate must be pilot_period x fH")
        self.n = 0

        dc = signal.butter(2, 15.0, "highpass", fs=self.fs, output="sos")
        self.dcL, self.dcR, self.dcS = Sos(dc), Sos(dc), Sos(dc)

        bl = bandlimit_sos(self.fs, 15000.0, 15700.0)
        # Sum channel: 75 us pre-emphasis, clip, band limit.
        self.pre = Sos(preemphasis_75us_sos(self.fs))
        self.bl = Sos(bl)
        # Difference channel: the same HF pole + band limit inside the dbx loop
        # ("stereophonic sum channel compensation").
        self.dbx = DbxCompressor(self.fs, np.vstack([hf_pole_sos(self.fs), bl]), block_s)
        if self.sap:
            self.dbx_sap = DbxCompressor(self.fs, bandlimit_sos(self.fs, 10000.0, 12000.0), block_s)
            self.sap_phase = 0.0

        k = np.arange(self.period)
        self.pilot_tab = np.sin(2 * np.pi * k / self.period)
        self.sub_tab = np.sin(4 * np.pi * k / self.period)

    def process(self, left, right, sap=None):
        left = np.asarray(left, dtype=float) * self.gain
        right = np.asarray(right, dtype=float) * self.gain
        n = len(left)
        left = self.dcL(left)
        right = self.dcR(right)

        idx = (self.n + np.arange(n)) % self.period
        self.n = (self.n + n) % (self.period * 7)

        m = 0.5 * (left + right)
        m = self.pre(m)
        np.clip(m, -1.0, 1.0, out=m)
        m = self.bl(m)
        dev = C.DEV_MAIN * m

        if self.stereo:
            y = self.dbx.process(0.5 * (left - right))
            dev += C.DEV_PILOT * self.pilot_tab[idx]
            dev += C.DEV_STEREO * y * self.sub_tab[idx]

        if self.sap:
            s = np.zeros(n) if sap is None else np.asarray(sap, dtype=float) * self.sap_gain
            s = self.dcS(s)
            ys = self.dbx_sap.process(s)
            ph = self.sap_phase + np.cumsum(ys) * (2 * np.pi * C.SAP_SUB_DEV / self.fs)
            self.sap_phase = float(np.mod(ph[-1], 2 * np.pi)) if n else self.sap_phase
            carrier = 2 * np.pi * 5.0 * idx / self.period
            dev += C.DEV_SAP * np.cos(carrier + ph)
        return dev


class BtscDecoder:
    """Reference BTSC receiver (composite deviation in Hz -> L, R, SAP).

    Used by the self-test and the IQ verification tool.  It estimates the
    pilot phase from the signal itself, like a real receiver's PLL would.
    """

    def __init__(self, fs=C.BTSC_RATE, period=C.BTSC_PILOT_PERIOD):
        self.fs = float(fs)
        self.period = period
        self.sos_lp = bandlimit_sos(self.fs, 15000.0, 15700.0)
        self.sos_de = deemphasis_75us_sos(self.fs)

    def decode(self, comp, sap=False):
        comp = np.asarray(comp, dtype=float)
        n = np.arange(len(comp))
        w = 2 * np.pi / self.period
        X = np.sum(comp * np.exp(-1j * w * n))
        pilot_dev = 2 * abs(X) / len(comp)
        phi = np.angle(X) + np.pi / 2
        main = signal.sosfilt(self.sos_lp, comp) / C.DEV_MAIN
        main = signal.sosfilt(self.sos_de, main)
        diff = signal.sosfilt(self.sos_lp, 2 * comp * np.sin(2 * (w * n + phi))) / C.DEV_STEREO
        diff = DbxExpander(self.fs).process(diff)
        out = {"left": main + diff, "right": main - diff, "main": main, "diff": diff,
               "pilot_dev": pilot_dev}
        if sap:
            bb = comp * np.exp(-1j * (5 * w * n))
            lp = signal.butter(6, 25e3, fs=self.fs, output="sos")
            bb = signal.sosfilt(lp, bb.real) + 1j * signal.sosfilt(lp, bb.imag)
            inst = np.diff(np.unwrap(np.angle(bb)), prepend=0.0) * self.fs / (2 * np.pi)
            ys = inst / C.SAP_SUB_DEV
            ys = signal.sosfilt(bandlimit_sos(self.fs, 10000.0, 12000.0), ys)
            out["sap"] = DbxExpander(self.fs).process(ys)
        return out
