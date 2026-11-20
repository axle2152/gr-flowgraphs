"""Warnings before transmitting.

Two checks, warnings only (into coax and attenuators, any channel is fine):

* what else uses the frequencies a channel covers. TV channels 37 and up (608 MHz and up) are
  radio astronomy, cellular and public-safety radio since the 2017 repack, and the cable channel
  plans land on aircraft, marine and public-safety radio if sent over the air;
* whether the gain is high enough that an antenna would put the signal on the air.

US allocations (FCC), simplified to the services where interference is serious.
"""

import sys
import time

# (low Hz, high Hz, what's there)
SERVICES = (
    (72e6, 76e6, "aircraft marker beacons (75 MHz) and radio control"),
    (108e6, 118e6, "aircraft navigation (ILS localizers, VOR)"),
    (118e6, 137e6, "aircraft voice radio, with the 121.5 MHz emergency channel"),
    (137e6, 144e6, "weather satellites and government radio"),
    (144e6, 148e6, "the 2 m amateur band"),
    (148e6, 174e6, "public-safety and business radio, marine VHF (156-162 MHz) and "
                   "NOAA Weather Radio (162.4-162.55 MHz)"),
    (216e6, 225e6, "the 1.25 m amateur band"),
    (225e6, 400e6, "military aircraft radio"),
    (400e6, 420e6, "rescue beacons (406 MHz) and government radio"),
    (420e6, 450e6, "the 70 cm amateur band and government radar"),
    (450e6, 470e6, "police, fire and EMS radio, FRS and GMRS"),
    (608e6, 614e6, "radio astronomy (channel 37)"),
    (614e6, 698e6, "600 MHz cellular (old TV channels 38 to 51)"),
    (698e6, 806e6, "700 MHz cellular and FirstNet public-safety radio (old channels 52 to 69)"),
    (806e6, 902e6, "800 MHz public-safety radio and cellular (old channels 70 to 83)"),
    (902e6, 928e6, "the 33 cm amateur band and ISM devices"),
    (928e6, 960e6, "paging and cellular"),
    (960e6, 1215e6, "aircraft navigation and transponders (DME, 1030/1090 MHz, ADS-B)"),
)

CHANNEL_BELOW = 1.25e6          # an NTSC channel runs from 1.25 MHz below the visual carrier
CHANNEL_ABOVE = 4.75e6          # to 4.75 MHz above it (6 MHz)
HIGH_GAIN_DB = 20.0             # HackRF TX VGA (0-47 dB): above this, or with the +14 dB amp
PAUSE_S = 5


def services(lo_hz, hi_hz):
    """What else uses any part of lo_hz .. hi_hz."""
    return [what for a, b, what in SERVICES if a < hi_hz and b > lo_hz]


def channel_span(visual_hz):
    return visual_hz - CHANNEL_BELOW, visual_hz + CHANNEL_ABOVE


def channel_services(visual_hz):
    return services(*channel_span(visual_hz))


def gain_is_high(gain_db, amp):
    return bool(amp) or gain_db > HIGH_GAIN_DB


def report(visual_hz, gain_db=0.0, amp=False, hackrf=True, label=None):
    """The warning, as lines (none when there's nothing to warn about)."""
    lines = []
    lo, hi = channel_span(visual_hz)
    found = services(lo, hi)
    name = label or f"{visual_hz / 1e6:.2f} MHz"
    if found:
        lines.append(f"{name} covers {lo / 1e6:.2f} to {hi / 1e6:.2f} MHz, which is also:")
        lines += [f"  - {what}" for what in found]
        lines.append("Interference there can put lives at risk, and it gets tracked down fast.")
    if hackrf and gain_is_high(gain_db, amp):
        lines.append(f"HackRF gain {gain_db:g} dB" + (" with the +14 dB amp." if amp else "."))
        lines.append("With an antenna attached, that goes out over the air far stronger than")
        lines.append("anything you may send on a TV channel without a licence.")
    if lines:
        lines.append("Transmit only into coax with 30-60 dB of attenuators, or into a dummy load.")
        lines.append("Never into an antenna.")
    return lines


def warn(lines, out=None, pause=PAUSE_S):
    """Print the warning; on a terminal, count down so it can't scroll past unseen."""
    if not lines:
        return False
    out = out or sys.stderr
    bar = "!" * 76
    print(bar, file=out)
    print("WARNING", file=out)
    for line in lines:
        print(line, file=out)
    print(bar, file=out)
    if pause and getattr(out, "isatty", lambda: False)():
        try:
            for n in range(int(pause), 0, -1):
                print(f"Starting in {n} s (Ctrl-C to stop)", file=out, flush=True)
                time.sleep(1)
        except KeyboardInterrupt:
            raise SystemExit("stopped")
    return True
