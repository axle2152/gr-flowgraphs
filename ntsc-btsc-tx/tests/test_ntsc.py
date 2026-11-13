"""NTSC-M composite encoder checks (SMPTE 170M)."""

import numpy as np
import pytest

from ntsc_btsc import constants as C
from ntsc_btsc import ntsc as N
from ntsc_btsc.verify import decode_frame, find_frames


@pytest.fixture(scope="module")
def enc():
    return N.NtscEncoder(output="ire")


@pytest.fixture(scope="module")
def bars_frame(enc):
    return enc.encode(*N.color_bars(), 0).reshape(C.LINES, C.LINE)


def test_timing_constants():
    assert C.LINE * C.LINES == C.FRAME == 477750
    assert C.FS / C.LINE == pytest.approx(C.FH)
    assert C.FRAME_RATE == pytest.approx(30000 / 1001, rel=1e-9)


def test_levels(bars_frame):
    line = bars_frame[N.row_to_line()[100]]
    assert line[20:60].mean() == pytest.approx(-40, abs=0.1)        # sync tip
    assert line[120:130].mean() == pytest.approx(0, abs=0.1)        # back porch
    burst = line[80:108]
    assert burst.max() - burst.min() == pytest.approx(40, abs=1)    # 40 IRE p-p
    white = line[C.ACTIVE_START + 20:C.ACTIVE_START + 80].mean()
    assert white == pytest.approx(7.5 + 0.75 * 92.5, abs=0.5)        # 75 % white


def test_sync_width_and_front_porch(bars_frame):
    line = bars_frame[50]
    assert line[0] == pytest.approx(-20, abs=0.5)              # 0H = 50 % point
    low = np.flatnonzero(line[:72] <= -20)          # stop before the burst
    assert (low[-1] - low[0]) / C.US == pytest.approx(4.7, abs=0.1)
    assert (C.LINE - (C.ACTIVE_START + C.WIDTH)) / C.US == pytest.approx(1.5, abs=0.25)


def test_vertical_interval_structure(bars_frame):
    flat = bars_frame.ravel()
    low = flat < -20
    d = np.diff(low.astype(np.int8))
    starts = np.flatnonzero(d == 1) + 1
    ends = np.flatnonzero(d == -1) + 1
    if ends[0] < starts[0]:
        ends = ends[1:]
    m = min(len(starts), len(ends))
    widths = (ends[:m] - starts[:m]) / C.US
    broad = starts[:m][widths > 20]
    eq = starts[:m][(widths > 1.8) & (widths < 2.8)]
    assert len(broad) == 12                     # 6 serrated pulses per field
    assert len(eq) == 24                        # 6 pre + 6 post per field
    # field 1 broad pulses start at line 4 0H; field 2's half a line later
    assert abs(broad[0] - 3 * C.LINE) <= 1
    assert abs(broad[6] - (265 * C.LINE + C.HALF)) <= 1


def test_burst_absent_in_vertical_sync(bars_frame):
    for ln in (0, 4, 8, 265, 270):
        seg = bars_frame[ln, 80:108]
        assert seg.max() - seg.min() < 1 or seg.min() < -30


def test_colour_frame_alternation(enc):
    a = enc.encode(*N.color_bars(), 0).reshape(C.LINES, C.LINE)
    b = enc.encode(*N.color_bars(), 1).reshape(C.LINES, C.LINE)
    # luma identical, chroma/burst inverted between the two colour frames
    burst_a, burst_b = a[30, 80:108], b[30, 80:108]
    assert np.allclose(burst_a, -burst_b, atol=1e-4)
    # and between consecutive lines (227.5 cycles per line)
    assert np.allclose(a[30, 80:108], -a[31, 80:108], atol=1e-4)


def test_rf_mapping():
    rf = N.NtscEncoder(output="rf").encode(*N.color_bars(), 0).reshape(C.LINES, C.LINE)
    assert rf[50, 20:60].mean() == pytest.approx(1.0, abs=1e-4)       # sync tip 100 %
    assert rf[50, 120:130].mean() == pytest.approx(0.75, abs=1e-4)    # blanking 75 %
    assert rf.min() >= 0.0                                             # never overmodulates


def test_bars_round_trip(enc):
    """Encode colour bars, decode with the reference decoder, compare RGB."""
    frame = enc.encode(*N.color_bars(), 0)
    sig = np.concatenate([frame, enc.encode(*N.color_bars(), 1)])
    starts = find_frames(sig)
    assert starts and abs(starts[0]) <= 1
    rgb = decode_frame(sig, starts[0])
    expected = [(.75, .75, .75), (.75, .75, 0), (0, .75, .75), (0, .75, 0),
                (.75, 0, .75), (.75, 0, 0), (0, 0, .75)]
    edges = np.linspace(0, C.WIDTH, 8).astype(int)
    for k, e in enumerate(expected):
        x0, x1 = edges[k] + 20, edges[k + 1] - 20
        got = rgb[100:200, x0:x1].reshape(-1, 3).mean(axis=0)
        assert np.abs(got - e).max() < 0.04, (k, got, e)


def test_chroma_bandwidths():
    up = N.ChromaUpsampler()
    f = np.fft.rfftfreq(up.n_in, d=4.0 / C.FS)
    hi = np.abs(up.h[0, 0]) / 4
    hq = np.abs(up.h[1, 0]) / 4
    at = lambda h, x: np.interp(x, f, h)
    assert 20 * np.log10(at(hi, 1.3e6)) > -2          # I: < 2 dB down at 1.3 MHz
    assert 20 * np.log10(at(hq, 0.4e6)) > -2          # Q: < 2 dB at 0.4 MHz
    assert 20 * np.log10(at(hq, 0.5e6)) > -6          #    < 6 dB at 0.5 MHz
    assert 20 * np.log10(at(hq, 0.6e6)) < -6          #    > 6 dB at 0.6 MHz
