"""North American (System M) channel tables: visual carrier frequencies in Hz."""


def _broadcast():
    t = {2: 55.25, 3: 61.25, 4: 67.25, 5: 77.25, 6: 83.25}
    for n in range(7, 14):
        t[n] = 175.25 + 6 * (n - 7)
    for n in range(14, 84):
        t[n] = 471.25 + 6 * (n - 14)
    return {k: v * 1e6 for k, v in t.items()}


def _cable_std():
    """EIA-542 / CEA-542 'Standard' cable plan."""
    t = {1: 73.25, 2: 55.25, 3: 61.25, 4: 67.25, 5: 77.25, 6: 83.25}
    for n in range(7, 14):
        t[n] = 175.25 + 6 * (n - 7)
    for n in range(14, 23):
        t[n] = 121.25 + 6 * (n - 14)
    for n in range(23, 95):
        t[n] = 217.25 + 6 * (n - 23)
    for n in range(95, 100):
        t[n] = 91.25 + 6 * (n - 95)
    for n in range(100, 159):
        t[n] = 649.25 + 6 * (n - 100)
    return {k: v * 1e6 for k, v in t.items()}


def _cable_irc():
    t = {k: v + 12.5e3 for k, v in _cable_std().items()}
    t[5] = 79.2625e6
    t[6] = 85.2625e6
    t.pop(1, None)
    return t


def _cable_hrc():
    # Harmonically related carriers: multiples of 6.0003 MHz
    return {k: round((v - 1.25e6) / 6e6) * 6.0003e6 for k, v in _cable_irc().items()}


BANDS = {
    "broadcast": _broadcast(),
    "cable": _cable_std(),
    "cable-irc": _cable_irc(),
    "cable-hrc": _cable_hrc(),
}


def visual_carrier(channel, band="broadcast"):
    try:
        return BANDS[band][int(channel)]
    except KeyError:
        raise ValueError(f"channel {channel} is not defined for band '{band}'") from None


def describe(band):
    rows = []
    for ch, f in sorted(BANDS[band].items()):
        rows.append(f"{ch:4d}  visual {f / 1e6:9.4f} MHz   aural {(f + 4.5e6) / 1e6:9.4f} MHz")
    return "\n".join(rows)
