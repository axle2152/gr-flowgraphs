# gr-flowgraphs

A collection of GNU Radio flowgraphs and tools. Each project lives in its own
folder with its own README.

| Folder | What it is |
|---|---|
| [ntsc-btsc-tx](ntsc-btsc-tx/) | NTSC-M TV transmitter with BTSC stereo and SAP sound. Plays files or live streams from OBS (RTMP/SRT/RTSP/UDP) to a HackRF or other SDR. Includes GNU Radio Companion blocks. |

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
