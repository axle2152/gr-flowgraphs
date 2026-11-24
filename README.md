# gr-flowgraphs

A collection of GNU Radio flowgraphs and tools. Each project lives in its own
folder with its own README.

| Folder | What it is |
|---|---|
| [ntsc-btsc-tx](ntsc-btsc-tx/) | NTSC-M TV transmitter with BTSC stereo and SAP sound. Plays files or live streams from OBS (RTMP/SRT/RTSP/UDP) to a HackRF or other SDR. Includes GNU Radio Companion blocks. |
| [crosby-fm-stereo](crosby-fm-stereo/) | Crosby FM-FM stereo transmitter (the 1958 multiplex system): L−R on a 50 kHz FM subcarrier, no pilot. GNU Radio Companion flowgraph for a HackRF. |
| [titanic-spark-gap](titanic-spark-gap/) | RF replica of the RMS Titanic's Marconi 5 kW synchronous rotary-spark transmitter: damped waves at 840 sparks/s, keyed in Morse, for a HackRF, with a 1912-style detector to listen on. |
| [fm-multipath-sim](fm-multipath-sim/) | FM multipath / picket-fencing simulator with a CMA equalizer and stereo receiver, for testing channel equalization. Runs on a PC, no SDR needed. |

## Before you transmit

ntsc-btsc-tx, crosby-fm-stereo and titanic-spark-gap are transmitters. Test
them into coax with attenuators (30-60 dB) or a dummy load, not an antenna.
The FM and TV bands are licensed spectrum, and the
frequencies around them carry aircraft, marine, public-safety and cellular
radio. TV channels 37 and up (608 MHz and up) are radio astronomy, cellular
and public-safety radio now, whatever an old channel chart says.

All three warn you before they start when a frequency is one of those,
or when the HackRF's gain is high enough (above 20 dB, or with its +14 dB amp)
that an antenna would put the signal on the air. They don't stop you: what
you transmit, and where it goes, is your responsibility.

## Setting up GNU Radio

**Windows (MSYS2):** use the **UCRT64** shell.

```bash
pacman -S --needed mingw-w64-ucrt-x86_64-gnuradio
```

MSYS2 has no gr-osmosdr. Use the Soapy blocks for hardware instead, e.g.
`mingw-w64-ucrt-x86_64-soapyhackrf` for a HackRF.

**Linux (Ubuntu/Debian):**

```bash
sudo apt install gnuradio
```

Individual projects list anything extra they need.

## License

GPL-3.0, see [LICENSE](LICENSE), unless a folder says otherwise.
