# ntsc-btsc-tx

An NTSC-M analogue television transmitter built on GNU Radio, with **BTSC
(MTS) stereo sound** and optional **SAP** (second audio program). It takes a
video file or a live stream (OBS Studio over RTMP, SRT, RTSP or UDP) and
produces complex baseband for a HackRF or any other SDR that gr-osmosdr or
SoapySDR supports.

What it adds compared with HackTV:

- **Real BTSC stereo.** The pilot is locked to the line rate, L−R is DSB-SC
  on 2fH, and the L−R channel is encoded with **dbx-TV**, implemented from the
  equations in FCC OET Bulletin 60. TVs show "STEREO" and separate the
  channels properly.
- **SAP** on the 5fH FM subcarrier, also dbx-TV encoded. It can come from a
  second audio track or from another file or URL.
- **Live input from OBS.** Each live input gets a jitter buffer and
  reconnects automatically. A colour-bar idle card is shown while nothing is
  connected.
- **GNU Radio native.** There's a command-line tool, a hierarchical block
  for your own flowgraphs, and GNU Radio Companion blocks.
- **A reference receiver** decodes the IQ back to pictures and stereo audio,
  so you can check everything without hardware.

## Quick start

### Windows (MSYS2)

Open the **MSYS2 UCRT64** shell and install the dependencies:

```bash
pacman -S --needed mingw-w64-ucrt-x86_64-gnuradio mingw-w64-ucrt-x86_64-ffmpeg mingw-w64-ucrt-x86_64-python-scipy mingw-w64-ucrt-x86_64-python-pillow mingw-w64-ucrt-x86_64-soapyhackrf mingw-w64-ucrt-x86_64-hackrf
```

MSYS2 has no gr-osmosdr, so the HackRF is driven through SoapySDR, which is
part of the GNU Radio package. The transmitter picks Soapy automatically.

Check the radio is visible:

```bash
SoapySDRUtil --find
```

If the HackRF doesn't show up, install the WinUSB driver for it with
[Zadig](https://zadig.akeo.ie/).

Then run everything below from the same UCRT64 shell (`python` and
`python3` both work there). If Ctrl-C doesn't stop the transmitter in the
MSYS2 terminal, run it from Windows Terminal instead.

Two things are different on Windows:

- **Use SRT, not RTMP, for OBS.** MSYS2's ffmpeg is built with librtmp, which
  can't act as an RTMP server, so `--listen` with `rtmp://` won't work (the
  transmitter tells you so). SRT works out of the box; see
  [Setting up OBS](#setting-up-obs).
- **Add `--priority high`** so background programs don't starve the
  transmitter.

### Linux (Ubuntu/Debian)

```bash
sudo apt install gnuradio gr-osmosdr ffmpeg python3-scipy python3-pil
```

### Running it

Run from the source tree:

```bash
cd ntsc-btsc-tx
python3 -m ntsc_btsc --help
```

or install it. Use a virtualenv created with `--system-site-packages` so it
can see GNU Radio:

```bash
pip install .
```

That gives you `ntsc-btsc-tx` and `ntsc-btsc-verify`. With radioconda,
`pip install .` inside the environment works as is.

Test without hardware. This writes 5 s of IQ and decodes it again:

```bash
python3 -m ntsc_btsc -i bars --sink file -o test.cf32 --duration 5
```

```bash
python3 -m ntsc_btsc.verify test.cf32 --png frame.png --wav audio.wav
```

The verifier prints the levels it measured and the stereo separation.

Transmit on channel 3 with a HackRF:

```bash
python3 -m ntsc_btsc -i movie.mp4 -c 3 -g 0
```

On MSYS2 this goes through SoapySDR at 15 Msps; on Linux with gr-osmosdr it
runs at 14.318 Msps. See [sample rates](#sample-rates).

> **Read the [legal note](#legal-and-safety) first.** Use a coax cable and
> attenuators into the TV's antenna input, not an antenna.

## Inputs

Anything ffmpeg can open works as `-i`:

| Input | Command |
|---|---|
| File (any format) | `-i movie.mkv`, add `--loop` to repeat, `--start 1:23` to seek |
| OBS → SRT (lowest latency; use this on MSYS2) | `-i "srt://0.0.0.0:9000?mode=listener"` |
| OBS → us as RTMP server (not with MSYS2's ffmpeg) | `-i rtmp://0.0.0.0:1935/live/obs --listen` |
| RTSP server (e.g. MediaMTX) | `-i rtsp://127.0.0.1:8554/live` |
| Something pushes RTSP to us | `-i rtsp://0.0.0.0:8554/live --listen` |
| MPEG-TS over UDP | `-i udp://127.0.0.1:1234` |
| Built-in test | `-i test` (moving pattern) or `-i bars`, both with 1 kHz left / 400 Hz right |

Extra ffmpeg input options go in `--ffmpeg-input-args="..."`. Use the `=` form,
because the value starts with a dash. For example,
`-i "testsrc2=s=1280x720" --ffmpeg-input-args="-f lavfi" --no-audio` uses any
lavfi source.

Pictures are converted to BT.601 and fitted to 4:3 (`--aspect pad|crop|stretch`).
They're sent as true 59.94i, so each field is its own moment in time; use
`--progressive` to send each frame as both fields instead.

Files are paced by the SDR's sample clock, so sound and picture stay locked
however long the programme is. Live inputs run free. A jitter buffer
(`--latency`, default 0.3 s) absorbs the clock difference between OBS and the
SDR, and drops or repeats when it has to.

### Setting up OBS

**SRT (recommended, and the one to use on MSYS2):**

1. Start the transmitter:

   ```bash
   python3 -m ntsc_btsc -i "srt://0.0.0.0:9000?mode=listener" -c 3
   ```

   No `--listen` is needed; `mode=listener` does that. Keep the quotes,
   because of the `?`.
2. In OBS, go to Settings → Stream. Choose Service **Custom**, set Server to
   `srt://127.0.0.1:9000?mode=caller` (or this PC's IP address if OBS runs on
   another machine), and leave Stream Key empty.
3. Click **Start Streaming**.

The transmitter shows colour bars until OBS connects, and again a couple of
seconds after OBS stops. It is then ready for the next connection. If OBS is
on another machine, allow ffmpeg through the firewall when Windows asks.

**RTMP (Linux and other ffmpeg builds without librtmp):**

1. Start the transmitter with `-i rtmp://0.0.0.0:1935/live/obs --listen`.
2. In OBS, set Server to `rtmp://127.0.0.1:1935/live` and Stream Key to `obs`.
3. Click **Start Streaming**.

If the transmitter says its ffmpeg is built with librtmp, use SRT.

**RTSP:** OBS can't serve RTSP itself. Either stream RTMP to
[MediaMTX](https://github.com/bluenviron/mediamtx) and pull
`rtsp://host:8554/<path>`, or use the obs-rtspserver plugin.

**UDP MPEG-TS:** In OBS, go to Settings → Output → Recording. Set Type to
Custom Output (FFmpeg), Output type to Output to URL, the URL to
`udp://127.0.0.1:1234?pkt_size=1316` and the container to `mpegts`. Then
click Start Recording.

Recommended OBS settings:

- **Video:** set the FPS to **59.94** (or 29.97) so frames map 1:1 onto
  fields with no judder. 1280×720 or 640×480 canvases are plenty.
- **Encoder:** x264 with tune `zerolatency`, keyframe interval 1 s, CBR
  3–6 Mbps.
- **Audio:** 48 kHz stereo.

## Choosing the channel and output

- `-c N` picks a channel; `--band broadcast|cable|cable-irc|cable-hrc` picks
  the plan. `--list-channels` prints the table.
- `-f HZ` sets the visual carrier directly instead.
- `--sink auto` is the default: gr-osmosdr (`-d hackrf=0`) if it's
  installed, otherwise SoapySDR (`-d driver=hackrf`). `-g` sets the HackRF TX
  VGA (0–47 dB) and `--amp` turns on the +14 dB amp.
- `--sink soapy -d driver=plutosdr` (or `driver=lime`, …) picks another
  SoapySDR radio, and `--sink osmosdr` forces gr-osmosdr.
- `--sink file -o out.cf32` writes an IQ recording (`--format cs8` is the
  HackRF format). For HackRF playback see [sample rates](#sample-rates).
- `--priority high` raises the process priority (the Windows high priority
  class, or real-time scheduling on Linux, which needs root or an rtprio
  limit). It's worth using on a busy PC.
- `--buffer` (seconds, default 0.15) is the extra buffering in front of the
  SDR, so short CPU stalls don't reach the radio. It adds the same latency to
  picture and sound.

### Sample rates

The signal is generated at 4 × fsc = **14.318182 Msps**, which every timing
relationship in NTSC is built on. `--samp-rate` gives another output rate
through exact rational resampling, so line rate, pilot and intercarrier stay
locked. To keep the cost down, only the real video envelope is resampled at
full rate (before the VSB filter). The sound is generated directly at the
output rate, with only its low-rate composite resampled.

The HackRF's SoapySDR driver only accepts whole-MHz rates, so with
`--sink soapy` and a HackRF the default output is **15 Msps**. That's the
cheapest whole-MHz rate: exactly 22/21 × 4fsc. Other choices such as
`--samp-rate 16e6` or `20e6` work too, at a little more CPU cost. gr-osmosdr, PlutoSDR,
LimeSDR and IQ files use 14.318182 Msps unless you say otherwise.

For HackRF playback of a recording, write it at a whole-MHz rate:

```bash
python3 -m ntsc_btsc -i movie.mp4 --sink file -o out.cs8 --format cs8 --samp-rate 15e6 --duration 60
```

```bash
hackrf_transfer -t out.cs8 -f 61250000 -s 15000000 -x 0
```

Decode a resampled file with
`python3 -m ntsc_btsc.verify out.cs8 --format cs8 --rate 15e6`.

### Where the SDR is tuned

By default the SDR is tuned **to the visual carrier**: the HackRF's LO leakage then hides under the
carrier, and its I/Q image falls outside the channel. `--offset 0` centres the
channel on the LO instead.

Levels:

- `--audio-gain` (dB): 0 dBFS = 100 % modulation of the main channel at low
  frequencies. Pre-emphasis raises treble, and the main channel is clipped at
  100 % before band limiting.
- `--aural-level`: aural carrier relative to peak visual carrier. The default
  is −10 dB (10 % power).
- `--setup 0` gives NTSC-J black level. Japan used FM-FM stereo, not BTSC, so
  use `--mono` there.

## GNU Radio Companion

```bash
python3 -m ntsc_btsc --install-grc
```

This copies the blocks to `~/.grc_gnuradio`. Alternatively, set
`GRC_BLOCKS_PATH=.../ntsc_btsc/grc`. Either way, `ntsc_btsc` must be
importable, so install it or set `PYTHONPATH`. The blocks appear under **NTSC
BTSC**:

- **NTSC/BTSC TV Source** is the whole transmitter. It has no inputs and
  outputs complex samples at its "Output sample rate" (default
  `ntsc_btsc.FS` = 14.318 Msps; use `15e6` for the Soapy HackRF Sink).
  Connect it to a sink with the same sample rate and frequency
  `ntsc_btsc.visual_carrier(3)`.
- **BTSC Encoder** takes L, R and optionally SAP at `ntsc_btsc.BTSC_RATE`
  (550.699 kHz = 35 fH). It outputs the aural deviation in Hz, ready for a
  Frequency Mod block with sensitivity `2*pi/ntsc_btsc.BTSC_RATE`.
- **NTSC Composite Source** outputs composite video in IRE, or as an AM
  envelope, at 14.318 Msps from any ffmpeg input.

There are examples in `examples/`:

- `tv_hackrf_soapy.grc`: Soapy HackRF Sink at 15 Msps. Use this one on
  MSYS2.
- `tv_hackrf.grc`: osmocom Sink at 14.318 Msps.
- `tv_to_file.grc`: writes 10 s of IQ for the verifier.

When run as generated scripts they take `-i` (input); the HackRF ones also
take `-c` (channel) and `-g` (gain), and the file one `-o` (output file).

## How it works

```
             ┌──────── one ffmpeg process ────────┐  stdout
 file / URL ─┤ decode, scale to 752x480 4:3,      ├─ yuv411p 29.97i ─▶ NTSC encoder ─┐
             │ BT.601, 59.94p -> 59.94i (bff)     ├─ stereo f32 ─┐   (numpy thread) │ pipe
             └────────────────────────────────────┘  loopback    ▼                  ▼
                                                     TCP   BTSC encoder ──pipe──▶ GNU Radio (C++ blocks only)
                                                           (numpy thread)
GNU Radio:  envelope ─▶ [resample] ─▶ float→complex ─▶ VSB FIR (-0.75/+4.2 MHz) ───┐
            deviation ─▶ [resample] ─▶ +carrier alias ─▶ FM ─▶ zero-stuff ─▶ band-pass ┴─▶ + ─▶ buffer ─▶ SDR
```

Audio reaches Python over loopback TCP rather than extra pipes. That keeps it
to a single ffmpeg process on every OS, which matters when ffmpeg itself is
the RTMP/RTSP server for OBS. On Windows, a self-test at start-up checks that
GNU Radio can read Python's pipes. If it can't, the encoders feed GNU Radio
through plain Python blocks instead (slower, but works anywhere).

Everything is derived from one sample clock (4 fsc). Line rate, colour
subcarrier, BTSC pilot and the 4.5 MHz intercarrier are therefore exactly
related, as the standards require.

**Video (SMPTE 170M)**

- **Line timing:** 910 samples per line. Sync is 4.7 µs and burst is 9
  cycles at 5.3 µs (40 IRE p-p, 180° phase). Front porch is 1.5 µs, and the
  active line is 52.5 µs.
- **Vertical interval:** full field structure with 6 pre-equalising, 6
  serrated broad and 6 post-equalising pulses. Field 2 is offset by half a
  line, and burst is blanked in lines 1–9 and 264–272.
- **Colour encoding:** I and Q are band-limited separately (I < 2 dB down at
  1.3 MHz; Q < 2 dB at 0.4 MHz and > 6 dB down at 0.6 MHz). The 4-field colour
  sequence alternates correctly between frames.
- **Levels:** 7.5 IRE setup. 140 IRE maps to negative AM: sync tip 100 %,
  blanking 75 %, white 12.5 %. Composite is clipped at 110 IRE so the visual
  carrier never collapses, which would cause intercarrier buzz.
- **Edges:** sync, burst and picture edges are raised-cosine shaped (about
  140 ns rise).

**Sound (OET-60)**

- **Main channel:** (L+R)/2 with 75 µs pre-emphasis; 100 % = 25 kHz.
- **Pilot:** fH at 5 kHz deviation.
- **Stereo subchannel:** dbx-TV (L−R)/2 as DSB-SC on 2fH; 100 % = 50 kHz.
  The subcarrier crosses zero going positive at every pilot zero crossing.
- **SAP:** dbx-TV audio on FM at 5fH (±10 kHz), injected at 15 kHz.
- **dbx-TV encoder:** a direct implementation of OET-60. It covers the fixed
  pre-emphasis F(f), the wideband compressor (P(f), 34.7 ms ERMS) and the
  spectral compressor (S(f,b) with Q(f), 11.4 ms ERMS). Overmodulation
  protection and band limiting sit inside the feedback loop, as in Figure 1.
  The detectors update every 0.25 ms, a negligible delay against their
  time constants.
- **Band limiting:** a 12th-order elliptic filter, 15.0 kHz pass, > 65 dB
  down at fH. It's applied identically to L+R so separation is preserved.

### Measured results

These come from the test suite and `ntsc_btsc.verify`, through the full RF
chain unless noted.

| Check | Result | Requirement |
|---|---|---|
| dbx-TV gain, 300 Hz tone at 14.1 % (encoder only) | −0.09 dB | ≈ 0 dB (OET-60) |
| dbx-TV gain, 8 kHz tone at 32 % (encoder only) | +18.37 dB | ≈ +18.4 dB (OET-60) |
| Weighting filters reproduce Fig. 1 references (filters only) | 0.062490 / 0.035655 | 0.062490 / 0.035655 |
| Stereo separation, 1 kHz L / 400 Hz R (14.318 and 15 Msps) | 52–56 dB | ≥ 30 dB |
| Separation, encoder only, 100 Hz–12 kHz | > 40 dB | ≥ 30 dB (20 dB at 14 kHz) |
| Pilot deviation | 5000 Hz | 5.0 ± 0.5 kHz |
| Blanking / sync tip | 0.748–0.749 | 0.75 |
| 75 % colour bars after RF round trip | within 1.6 % RGB | — |
| Speed, 4-vCPU 2.1 GHz cloud VM, Linux: 14.318 Msps / 15 Msps | 2.4× / 2.0× real time | ≥ 1× |
| Speed, i7-6700K, Windows 10/11 + MSYS2: 14.318 Msps | 1.5× real time | ≥ 1× |

Separation stays high in steady state. During the first ~2 ms after an abrupt
onset, the compressor overshoots into its overmodulation clipper. That is
inherent to dbx-TV, and hardware encoders behave the same way.

## Tests

```bash
python3 -m pytest
```

There are 56 tests. They cover the OET-60 calibration points, separation,
pilot phase, SAP, SMPTE 170M timing and levels, a colour round trip, the
channel tables, and end-to-end GNU Radio runs. The tests that need GNU Radio
or ffmpeg are skipped if those aren't installed.

## Troubleshooting

- **Picture glitches and audio pops.** First work out which of two causes it
  is:
  - **`U` printed by the SDR driver** means the CPU couldn't keep up for a
    moment. Use `--priority high`, raise `--buffer` (e.g. `0.3`), and close
    background programs. If OBS runs on the same PC, use its hardware
    encoder (NVENC/QuickSync/AMF) and a 640×480 or 854×480 output.
  - **The `underruns` count in the status line rising** means the live stream
    arrived late, not the CPU. Use a bigger jitter buffer, e.g.
    `--latency 0.5`.

  To see what your machine manages, benchmark it with
  `--sink null --duration 30` (add `--samp-rate 15e6` for the HackRF via
  Soapy). The fps must be comfortably above 29.97.
- **No stereo indicator on the TV:** make sure you didn't pass `--mono`, and
  that the aural carrier isn't attenuated (try `--aural-level -7`). Some sets
  only show stereo with a strong, clean signal.
- **Fine diagonal pattern with `--offset 0`:** that's LO leakage at the
  channel centre. Use the default offset.
- **Sound but no colour:** check the TV is set to NTSC (multi-standard sets).
- **Soapy rejects the sample rate:** use a whole-MHz `--samp-rate` (15e6 is
  the default for HackRF via Soapy).
- **"using the slower Python-block transport" on Windows:** GNU Radio and
  Python aren't sharing a C runtime (mixed MSYS2 environments, for example).
  It still works; make sure everything comes from the UCRT64 packages.

## Limitations

- NTSC-M (and NTSC-J levels) only. No PAL, SECAM, closed captions or
  teletext yet.
- Output rates other than 14.318 Msps cost some CPU for resampling (about
  15–20 % fewer fps at 15 Msps).
- Audio is resampled by ffmpeg to 550 699 Hz, 0.55 ppm from the exact
  composite rate: about 2 ms of A/V drift per hour on a file. Live inputs are
  resynchronised continuously.
- Developed on Linux (GNU Radio 3.10.9, ffmpeg 6.1). Also run on Windows with
  MSYS2 UCRT64 (GNU Radio 3.10.12, Python 3.14, ffmpeg 9): the IQ verifies
  identically there, and OBS streaming over SRT to a HackRF works. macOS
  should work but hasn't been tried.
- `--listen` with RTMP needs an ffmpeg without librtmp, so it doesn't work on
  MSYS2; use SRT there.

## Legal and safety

Analogue TV channels are not unused spectrum. VHF and UHF TV channels carry
ATSC digital stations, and 600 MHz and above is now mobile broadband.
Transmitting there without a licence is illegal in most countries. Connect
the SDR to the TV with coax through attenuators (30–60 dB), keep the gain
low, and never connect an antenna. You are responsible for staying within
your local rules, such as FCC Part 15 in the US.

## License

GPL-3.0-or-later, the same as GNU Radio and HackTV. See the repository's
[LICENSE](../LICENSE).
