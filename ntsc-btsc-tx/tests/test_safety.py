"""The warnings before transmitting (ntsc_btsc.safety): no GNU Radio needed."""
import io

import pytest

from ntsc_btsc import channels, safety


@pytest.mark.parametrize("ch", [2, 6, 7, 13, 14, 24, 36])
def test_tv_channels_up_to_36_are_quiet(ch):
    vis = channels.visual_carrier(ch)
    assert safety.channel_services(vis) == []
    assert safety.report(vis) == []


@pytest.mark.parametrize("ch, word", [(37, "radio astronomy"), (38, "600 MHz cellular"),
                                      (51, "600 MHz cellular"), (52, "700 MHz"), (69, "FirstNet"),
                                      (70, "800 MHz"), (83, "800 MHz")])
def test_channels_37_and_up_warn(ch, word):
    lines = safety.report(channels.visual_carrier(ch), label=f"Channel {ch}")
    assert lines and lines[0].startswith(f"Channel {ch} covers")
    assert any(word in line for line in lines)
    assert any("Never into an antenna" in line for line in lines)


def test_channel_36_stops_at_608_mhz():
    lo, hi = safety.channel_span(channels.visual_carrier(36))
    assert (lo, hi) == (602e6, 608e6)


@pytest.mark.parametrize("ch, word", [(1, "marker beacons"), (14, "121.5 MHz"), (18, "marine VHF"),
                                      (99, "aircraft navigation")])
def test_cable_channels_on_aircraft_and_marine_bands_warn(ch, word):
    vis = channels.visual_carrier(ch, "cable")
    assert any(word in s for s in safety.channel_services(vis))


def test_gain():
    vis = channels.visual_carrier(24)
    assert safety.report(vis, 20) == []                       # at the limit: quiet
    lines = safety.report(vis, 21)
    assert lines[0] == "HackRF gain 21 dB."
    assert safety.report(vis, 0, amp=True)[0] == "HackRF gain 0 dB with the +14 dB amp."
    assert safety.report(vis, 40, hackrf=False) == []         # another SDR's gain means something else


def test_lines_fit_the_warning_bar():
    vis = channels.visual_carrier(16, "cable")                # two services, and a high gain
    lines = safety.report(vis, 47, amp=True, label="Channel 16")
    assert all(len(line) <= 76 for line in lines), [line for line in lines if len(line) > 76]


def test_warn_prints_and_never_waits_off_a_terminal():
    out = io.StringIO()
    assert safety.warn(["something"], out=out) is True
    text = out.getvalue()
    assert "WARNING" in text and "something" in text and "Starting in" not in text
    assert safety.warn([], out=out) is False


def test_channel_list_marks_them():
    text = channels.describe("broadcast")
    rows = {int(r.split()[0]): r for r in text.splitlines() if r.strip()[:1].isdigit()}
    assert "!" not in rows[36] and "! also radio astronomy" in rows[37]
    assert "! also 600 MHz cellular" in rows[45]
