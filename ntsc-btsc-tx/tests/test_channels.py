import pytest

from ntsc_btsc.channels import visual_carrier


@pytest.mark.parametrize("band,ch,mhz", [
    ("broadcast", 2, 55.25), ("broadcast", 3, 61.25), ("broadcast", 4, 67.25),
    ("broadcast", 5, 77.25), ("broadcast", 7, 175.25), ("broadcast", 13, 211.25),
    ("broadcast", 14, 471.25), ("broadcast", 36, 603.25),
    ("cable", 1, 73.25), ("cable", 14, 121.25), ("cable", 23, 217.25),
    ("cable", 95, 91.25), ("cable", 99, 115.25), ("cable", 100, 649.25), ("cable", 158, 997.25),
    ("cable-irc", 5, 79.2625), ("cable-irc", 3, 61.2625),
    ("cable-hrc", 2, 54.0027), ("cable-hrc", 5, 78.0039),
])
def test_channel_frequencies(band, ch, mhz):
    assert visual_carrier(ch, band) == pytest.approx(mhz * 1e6, abs=1.0)


def test_unknown_channel():
    with pytest.raises(ValueError):
        visual_carrier(1, "broadcast")
