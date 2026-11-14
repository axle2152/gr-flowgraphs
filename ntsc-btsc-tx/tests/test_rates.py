"""Output-rate planning, and the librtmp check."""

import os
import stat

import pytest

pytest.importorskip("gnuradio.gr")

from ntsc_btsc import constants as C  # noqa: E402
from ntsc_btsc.flowgraph import aural_plan, is_native_rate, resample_ratio  # noqa: E402
from ntsc_btsc.media import MediaReader, ffmpeg_uses_librtmp  # noqa: E402


def test_integer_4fsc_is_native():
    assert is_native_rate(14318182) and is_native_rate(C.FS)
    assert resample_ratio(14318182) == (1, 1)
    assert aural_plan(14318182) == (C.BTSC_DECIM, C.BTSC_RATE, (1, 1))


@pytest.mark.parametrize("rate", [15e6, 16e6, 20e6, 10e6])
def test_aural_plan_lands_exactly_on_rate(rate):
    stuff, comp_rate, (p, q) = aural_plan(rate)
    assert comp_rate * stuff == pytest.approx(rate, abs=1e-6)
    assert C.BTSC_RATE * p / q == pytest.approx(comp_rate, rel=1e-12)
    assert comp_rate >= 500e3               # room for the +/-180 kHz FM band


def test_15msps_uses_small_ratios():
    assert resample_ratio(15e6) == (22, 21)
    assert aural_plan(15e6)[2] == (22, 21)


def test_librtmp_listen_is_refused(tmp_path):
    fake = tmp_path / "ffmpeg"
    fake.write_text('#!/bin/sh\necho "--enable-gpl --enable-librtmp"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    if os.name == "nt":
        pytest.skip("shell-script stand-in for ffmpeg")
    assert ffmpeg_uses_librtmp(str(fake))
    with pytest.raises(ValueError, match="SRT"):
        MediaReader("rtmp://0.0.0.0:1935/live/obs", listen=True, ffmpeg=str(fake))
    # SRT and RTMP pulls are fine with such an ffmpeg
    MediaReader("srt://0.0.0.0:9000?mode=listener", ffmpeg=str(fake))
    MediaReader("rtmp://127.0.0.1/live/obs", ffmpeg=str(fake))
