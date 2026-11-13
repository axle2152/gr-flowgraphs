"""BTSC encoder checks against FCC OET Bulletin 60 Rev. A."""

import numpy as np
import pytest

from ntsc_btsc import btsc as B
from ntsc_btsc import constants as C

FS = C.BTSC_RATE


def tone(x, f):
    n = np.arange(len(x))
    return 2 * abs(np.mean(x * np.exp(-2j * np.pi * f * n / FS)))


def compressor():
    bl = np.vstack([B.hf_pole_sos(FS), B.bandlimit_sos(FS, 15000, 15700)])
    return B.DbxCompressor(FS, bl)


def steady_output(freq, amp, secs=0.6):
    t = np.arange(int(secs * FS)) / FS
    y = compressor().process(amp * np.sin(2 * np.pi * freq * t))
    return np.sqrt(2 * np.mean(y[-int(0.2 * FS):] ** 2))


def test_reference_constants_follow_from_weighting_filters():
    # Figure 1's 0.062490 / 0.035655 are the ERMS of 8.99 % at 300 Hz through
    # P(f) and 5.16 % at 8 kHz through Q(f): checks our P and Q transcriptions.
    p = abs(B.analog_response(B.wideband_weighting_zpk(), 300.0))
    q = abs(B.analog_response(B.spectral_weighting_zpk(), 8000.0))
    assert 0.0899 * p / np.sqrt(2) == pytest.approx(B.WB_REF, rel=1e-4)
    assert 0.0516 * q / np.sqrt(2) == pytest.approx(B.SP_REF, rel=1e-4)


def test_fixed_preemphasis_shape():
    f = B.analog_response(B.fixed_preemphasis_zpk(), [0.1, 1e6])
    assert abs(f[0]) == pytest.approx(1.0, abs=1e-3)
    # high-frequency asymptote 5.23 * 62.5 / (0.408 * 2.19) = 51.3 dB (Fig. 1)
    assert 20 * np.log10(abs(f[1])) == pytest.approx(51.3, abs=0.2)


def test_spectral_compression_is_flat_at_b_equal_1():
    assert np.allclose(abs(B.spectral_response([100, 5000, 15000], 1.0)), 1.0)


@pytest.mark.parametrize("freq,out_level,gain_db", [(300, 0.141, 0.0), (8000, 0.320, 18.4)])
def test_oet60_calibration_points(freq, out_level, gain_db):
    """OET-60 B(c)(2)(ii)(c) and (iii)(c): 0 dB gain at ~14.1 % / 300 Hz and
    +18.4 dB at ~32 % / 8 kHz."""
    lo, hi = 1e-3, 1.0
    for _ in range(25):
        mid = np.sqrt(lo * hi)
        if steady_output(freq, mid) < out_level:
            lo = mid
        else:
            hi = mid
    measured = 20 * np.log10(out_level / mid)
    assert measured == pytest.approx(gain_db, abs=0.5)


def _encode(left, right, sap=None, **kw):
    enc = B.BtscEncoder(sap=sap is not None, **kw)
    out = []
    for i in range(0, len(left), 8192):
        out.append(enc.process(left[i:i + 8192], right[i:i + 8192],
                               None if sap is None else sap[i:i + 8192]))
    return np.concatenate(out)


@pytest.mark.parametrize("freq", [100, 1000, 5000, 12000])
def test_stereo_separation_through_reference_decoder(freq):
    t = np.arange(int(0.8 * FS)) / FS
    left = 0.3 * np.sin(2 * np.pi * freq * t)
    comp = _encode(left, np.zeros_like(left))
    out = B.BtscDecoder().decode(comp)
    s = slice(int(0.4 * FS), None)
    L, R = tone(out["left"][s], freq), tone(out["right"][s], freq)
    assert L == pytest.approx(0.3, rel=0.05)
    assert 20 * np.log10(L / R) > 40      # OET-60 asks for 30 dB


def test_pilot_and_deviation_limits():
    t = np.arange(int(0.5 * FS)) / FS
    x = np.sin(2 * np.pi * 1000 * t)
    comp = _encode(x, -x)                  # full-scale L-R only
    assert tone(comp, C.FH) == pytest.approx(C.DEV_PILOT, rel=0.01)
    # main channel empty, L-R subchannel at most 50 kHz + 5 kHz pilot (+ BL overshoot)
    assert np.abs(comp).max() < 1.12 * (C.DEV_STEREO + C.DEV_PILOT)
    assert tone(comp[: int(0.1 * FS)], 1000) < 0.01 * C.DEV_MAIN


def test_pilot_is_locked_to_line_rate():
    comp = _encode(np.zeros(C.BTSC_PILOT_PERIOD * 1000), np.zeros(C.BTSC_PILOT_PERIOD * 1000))
    # exact period of 35 samples = fH; and nothing else present with silence
    assert np.allclose(comp[:35], comp[35:70], atol=1e-6)
    assert tone(comp, C.FH) == pytest.approx(C.DEV_PILOT, rel=1e-3)


def test_mono_mode_has_no_pilot():
    x = np.zeros(20000)
    comp = _encode(x, x, stereo=False)
    assert np.abs(comp).max() < 1.0


def test_left_only_gives_upward_main_deviation_and_positive_subcarrier():
    """OET-60 C(a)(4): left-only positive -> upward deviation, and the
    subcarrier crosses zero going positive at each pilot zero crossing."""
    n = int(0.5 * FS)
    left = np.full(n, 0.2)
    left[: n // 2] = 0.0
    enc = B.BtscEncoder()
    enc.sos_dc = None
    enc.dcL = enc.dcR = lambda v: np.asarray(v, float)   # keep the DC step
    comp = np.concatenate([enc.process(left[i:i + 4096], np.zeros(len(left[i:i + 4096])))
                           for i in range(0, n, 4096)])
    k = np.arange(n) % C.BTSC_PILOT_PERIOD
    sub = comp - C.DEV_PILOT * np.sin(2 * np.pi * k / C.BTSC_PILOT_PERIOD)
    late = slice(int(0.45 * FS), int(0.5 * FS))
    main = np.mean(sub[late])
    assert main > 0
    # correlate with sin(2 theta): positive for a positive L-R
    corr = np.mean((sub[late] - main) * np.sin(4 * np.pi * k[late] / C.BTSC_PILOT_PERIOD))
    assert corr > 0


def test_sap_round_trip():
    t = np.arange(int(1.0 * FS)) / FS
    s = 0.25 * np.sin(2 * np.pi * 3000 * t)
    z = np.zeros_like(s)
    comp = _encode(z, z, sap=s)
    out = B.BtscDecoder().decode(comp, sap=True)
    assert tone(out["sap"][int(0.5 * FS):], 3000) == pytest.approx(0.25, rel=0.1)


def test_fast_sos_matches_scipy():
    from scipy import signal
    sos = B.bandlimit_sos(FS, 15000, 15700)
    x = np.random.default_rng(1).standard_normal(5000)
    f = B.Sos(sos)
    y = np.concatenate([f(x[:1234]), f(x[1234:])])
    assert np.allclose(y, signal.sosfilt(sos, x), atol=1e-9)
