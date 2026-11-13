"""End-to-end: GNU Radio flowgraph -> IQ file -> reference receiver.

Needs GNU Radio and ffmpeg; skipped otherwise.
"""

import shutil

import numpy as np
import pytest

gr = pytest.importorskip("gnuradio.gr")
if shutil.which("ffmpeg") is None:
    pytest.skip("ffmpeg not installed", allow_module_level=True)

from gnuradio import blocks  # noqa: E402

from ntsc_btsc import constants as C  # noqa: E402
from ntsc_btsc.btsc import BtscDecoder  # noqa: E402
from ntsc_btsc.flowgraph import tv_source  # noqa: E402
from ntsc_btsc.verify import (IqFile, RatedIq, decode_frame, demod_aural, demod_video,  # noqa: E402
                              find_frames, tone_level)


def run_tx(path, seconds, **kw):
    tb = gr.top_block()
    src = tv_source(**kw)
    tb.connect(src, blocks.head(gr.sizeof_gr_complex, int(seconds * src.samp_rate)),
               blocks.file_sink(gr.sizeof_gr_complex, str(path), False))
    tb.run()
    src.stop_input()


def check_rf(iq, offset):
    """Picture levels, colour and stereo from a 4fsc view of a recording."""
    seg = iq.read(10 * C.FRAME, 3 * C.FRAME)
    assert np.abs(seg).max() < 1.0
    ire, tip, blank = demod_video(seg, offset)
    assert blank / tip == pytest.approx(0.75, abs=0.01)
    frames = find_frames(ire)
    assert frames
    rgb = decode_frame(ire, frames[0])
    yellow = rgb[60:120, 160:200].reshape(-1, 3).mean(axis=0)   # SMPTE bars: 75 % yellow
    assert np.abs(yellow - [0.75, 0.75, 0.0]).max() < 0.08

    dev = demod_aural(iq, offset)[int(0.3 * C.BTSC_RATE):]
    out = BtscDecoder().decode(dev)
    assert out["pilot_dev"] == pytest.approx(5000, rel=0.02)
    L, R = out["left"][int(0.3 * C.BTSC_RATE):], out["right"][int(0.3 * C.BTSC_RATE):]
    l1k, r1k = tone_level(L, 1000, C.BTSC_RATE), tone_level(R, 1000, C.BTSC_RATE)
    r400, l400 = tone_level(R, 400, C.BTSC_RATE), tone_level(L, 400, C.BTSC_RATE)
    assert l1k == pytest.approx(0.125, rel=0.05)       # ffmpeg sine = 1/8 full scale
    assert 20 * np.log10(l1k / r1k) > 40
    assert 20 * np.log10(r400 / l400) > 40


@pytest.mark.parametrize("offset", [1.75e6, 0.0])
def test_bars_and_stereo_over_rf(tmp_path, offset):
    path = tmp_path / "tx.cf32"
    run_tx(path, 1.5, input="bars", channel_offset=offset)
    iq = IqFile(str(path))
    assert len(iq) == int(1.5 * C.FS)
    check_rf(iq, offset)


def test_whole_mhz_output_rate(tmp_path):
    """15 Msps for HackRF via SoapySDR: exact 22/21 resampling of 4fsc."""
    path = tmp_path / "tx15.cf32"
    run_tx(path, 1.5, input="bars", samp_rate=15e6)
    raw = IqFile(str(path))
    assert len(raw) == int(1.5 * 15e6)
    check_rf(RatedIq(raw, 15e6), 1.75e6)


def test_python_block_transport(tmp_path):
    """The fallback used when GNU Radio can't read Python's pipes (Windows)."""
    path = tmp_path / "txq.cf32"
    run_tx(path, 1.5, input="bars", transport="block")
    check_rf(IqFile(str(path)), 1.75e6)


def test_file_input_ends_flowgraph(tmp_path):
    clip = tmp_path / "clip.mp4"
    import subprocess
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=25:duration=1", "-f", "lavfi", "-i",
                    "sine=frequency=700:duration=1", "-shortest", "-pix_fmt", "yuv420p",
                    str(clip)], check=True)
    path = tmp_path / "tx.cf32"
    tb = gr.top_block()
    src = tv_source(input=str(clip))
    tb.connect(src, blocks.file_sink(gr.sizeof_gr_complex, str(path), False))
    tb.run()                                   # must terminate on its own at EOF
    src.stop_input()
    frames = len(IqFile(str(path))) / C.FRAME
    assert 28 <= frames <= 31
