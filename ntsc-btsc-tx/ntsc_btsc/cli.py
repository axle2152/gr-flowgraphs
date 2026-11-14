"""ntsc-btsc-tx: NTSC-M television transmitter with BTSC stereo, for GNU Radio."""

import argparse
import logging
import os
import signal as pysignal
import sys
import time

from . import channels
from . import constants as C

log = logging.getLogger("ntsc_btsc")

EPILOG = """
input examples:
  movie.mp4                         a file (add --loop to repeat it)
  rtmp://0.0.0.0:1935/live/obs --listen
                                    be the RTMP server OBS streams to
  srt://0.0.0.0:9000?mode=listener  SRT listener (OBS: srt://HOST:9000)
  rtsp://127.0.0.1:8554/live        pull from an RTSP server (e.g. MediaMTX)
  udp://127.0.0.1:1234              MPEG-TS over UDP
  test | bars                       built-in test pattern + 1 kHz L / 400 Hz R

output examples:
  --channel 3                       HackRF via gr-osmosdr on broadcast channel 3
  --band cable --channel 99 --sink soapy --device driver=hackrf
  --sink file -o out.cs8 --format cs8 --samp-rate 15e6 --duration 10
                                    (then: hackrf_transfer -t out.cs8 -s 15000000 -f <Hz>)
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog="ntsc-btsc-tx", description=__doc__, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("input")
    src.add_argument("-i", "--input", default="test",
                     help="file, URL (rtmp/rtsp/srt/udp/http...), 'test' or 'bars'")
    src.add_argument("--listen", action="store_true",
                     help="act as the server for rtmp:// or rtsp:// (OBS pushes to us)")
    src.add_argument("--live", dest="live", action="store_true", default=None,
                     help="treat input as live (jitter buffer, auto-reconnect)")
    src.add_argument("--no-live", dest="live", action="store_false",
                     help="treat a network input as a file (paced by the SDR)")
    src.add_argument("--loop", action="store_true", help="loop a file forever")
    src.add_argument("--start", metavar="TIME", help="start position in the file (ffmpeg -ss)")
    src.add_argument("--latency", type=float, default=0.3,
                     help="live jitter buffer in seconds (default 0.3)")
    src.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable")
    src.add_argument("--ffmpeg-input-args", default="",
                     help='extra ffmpeg options placed before -i; use the = form, e.g. '
                          '--ffmpeg-input-args="-rtsp_transport udp"')

    vid = p.add_argument_group("video")
    vid.add_argument("--aspect", choices=["pad", "crop", "stretch"], default="pad",
                     help="how to fit non-4:3 pictures (default: pad = letter/pillarbox)")
    vid.add_argument("--progressive", action="store_true",
                     help="send each 29.97 fps frame as both fields instead of 59.94i motion")
    vid.add_argument("--setup", type=float, default=7.5,
                     help="black level IRE: 7.5 (NTSC-M, default) or 0 (NTSC-J)")
    vid.add_argument("--white-clip", type=float, default=110.0,
                     help="composite clip level in IRE (default 110)")
    vid.add_argument("--idle", choices=["bars", "black"], default="bars",
                     help="picture shown while a live input is absent")

    aud = p.add_argument_group("audio")
    aud.add_argument("--mono", action="store_true", help="no stereo pilot / L-R subchannel")
    aud.add_argument("--no-audio", action="store_true", help="ignore input audio (silent carrier)")
    aud.add_argument("--audio-gain", type=float, default=0.0,
                     help="dB; 0 dBFS = 100%% modulation at low frequencies (default 0)")
    aud.add_argument("--sap", metavar="INPUT",
                     help="second audio program from another file/URL ('test:sap' = 3 kHz tone)")
    aud.add_argument("--sap-track", type=int, metavar="N",
                     help="second audio program from audio track N of the main input")
    aud.add_argument("--sap-gain", type=float, default=0.0, help="SAP gain in dB")
    aud.add_argument("--aural-level", type=float, default=-10.0,
                     help="aural carrier relative to peak visual carrier, dB (default -10)")

    rf = p.add_argument_group("RF")
    rf.add_argument("-c", "--channel", type=int, help="channel number")
    rf.add_argument("--band", choices=sorted(channels.BANDS), default="broadcast",
                    help="channel plan (default broadcast)")
    rf.add_argument("-f", "--freq", type=float,
                    help="visual carrier frequency in Hz (instead of --channel)")
    rf.add_argument("--offset", type=float, default=None,
                    help="channel centre position relative to the SDR LO in Hz.  Default "
                         "1.75e6 tunes the SDR to the visual carrier, hiding LO leakage under "
                         "it; 0 centres the channel")
    rf.add_argument("--level", type=float, default=0.9, help="peak digital level (default 0.9)")
    rf.add_argument("--list-channels", action="store_true", help="print the channel plan and exit")
    p.add_argument("--install-grc", nargs="?", const="~/.grc_gnuradio", metavar="DIR",
                   help="copy the GNU Radio Companion blocks to DIR (default ~/.grc_gnuradio) "
                        "and exit")

    out = p.add_argument_group("output")
    out.add_argument("--sink", choices=["auto", "osmosdr", "soapy", "file", "null"], default="auto",
                     help="auto = gr-osmosdr if installed, else SoapySDR (MSYS2 has no gr-osmosdr)")
    out.add_argument("-d", "--device", default=None,
                     help="device args (osmosdr default 'hackrf=0', soapy default 'driver=hackrf')")
    out.add_argument("-g", "--gain", type=float, default=0.0,
                     help="TX gain (HackRF: TX VGA 0-47 dB, default 0)")
    out.add_argument("--amp", action="store_true", help="enable the HackRF's +14 dB TX amplifier")
    out.add_argument("--samp-rate", type=float, default=None,
                     help="output sample rate in Hz (default 14318182 = 4fsc; 15e6 for HackRF via "
                          "SoapySDR, which only takes whole-MHz rates).  Other rates are produced "
                          "with an exact rational resampler")
    out.add_argument("--buffer", type=float, default=0.15, metavar="SECONDS",
                     help="extra buffering in front of the SDR so short CPU stalls don't cause "
                          "underruns (default 0.15)")
    out.add_argument("--priority", choices=["normal", "high"], default="normal",
                     help="high: raise process priority (Windows high priority class; Linux "
                          "real-time scheduling, needs an rtprio limit or root)")
    out.add_argument("--bandwidth", type=float, default=0.0,
                     help="SDR analogue filter bandwidth in Hz (0 = driver default)")
    out.add_argument("--ppm", type=float, default=0.0, help="frequency correction (osmosdr)")
    out.add_argument("-o", "--output", help="output file for --sink file")
    out.add_argument("--format", choices=["cf32", "cs8", "cs16"], default="cf32",
                     help="IQ file format (cs8 is hackrf_transfer's format)")
    out.add_argument("--duration", type=float, help="stop after this many seconds")
    out.add_argument("--realtime", action="store_true",
                     help="throttle file/null output to real time (automatic for live input)")
    p.add_argument("--transport", choices=["auto", "pipe", "block"], default="auto",
                   help=argparse.SUPPRESS)   # how encoders feed GNU Radio (troubleshooting)
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("-q", "--quiet", action="store_true", help="no periodic status lines")
    return p


def resolve_frequency(args):
    if args.freq:
        return args.freq
    if args.channel is not None:
        return channels.visual_carrier(args.channel, args.band)
    return None


def default_bandwidth(args):
    """Analogue filter wide enough for the whole channel at the chosen offset."""
    lo_edge = abs(args.offset - C.VISUAL_TO_CENTER - 1.25e6)
    hi_edge = abs(args.offset - C.VISUAL_TO_CENTER + C.AURAL_OFFSET + 0.25e6)
    return 2 * max(lo_edge, hi_edge, 3.5e6) + 1e6


def raise_priority():
    """Raise this process's scheduling priority.  Returns a description, or
    None if it couldn't be done."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetPriorityClass.restype = wintypes.BOOL
        HIGH_PRIORITY_CLASS = 0x00000080
        if k32.SetPriorityClass(k32.GetCurrentProcess(), HIGH_PRIORITY_CLASS):
            return "Windows high priority class"
        return None
    from gnuradio import gr
    if gr.enable_realtime_scheduling() == gr.RT_OK:
        return "real-time scheduling"
    try:
        os.nice(-10)
        return "nice -10"
    except OSError:
        return None


def make_source(args):
    from .flowgraph import tv_source
    return tv_source(
        input=args.input, channel_offset=args.offset, aural_db=args.aural_level,
        level=args.level, stereo=not args.mono, audio=not args.no_audio, sap=args.sap,
        sap_track=args.sap_track, audio_gain_db=args.audio_gain, sap_gain_db=args.sap_gain,
        setup=args.setup, white_clip=args.white_clip, aspect=args.aspect,
        interlaced=not args.progressive, loop=args.loop, live=args.live, listen=args.listen,
        latency=args.latency, idle=args.idle, ffmpeg=args.ffmpeg,
        input_args=args.ffmpeg_input_args, start=args.start, transport=args.transport,
        samp_rate=args.samp_rate, buffer=args.buffer, priority=args.priority)


def resolve_sink(name):
    if name != "auto":
        return name
    import importlib.util
    return "osmosdr" if importlib.util.find_spec("osmosdr") else "soapy"


def make_sink(tb, args, lo):
    from gnuradio import blocks, gr
    if args.sink == "file":
        if not args.output:
            raise SystemExit("--sink file needs -o/--output")
        if args.format == "cf32":
            return [blocks.file_sink(gr.sizeof_gr_complex, args.output, False)]
        if args.format == "cs8":
            conv = blocks.complex_to_interleaved_char(False, 127.0)
            return [conv, blocks.file_sink(gr.sizeof_char, args.output, False)]
        conv = blocks.complex_to_interleaved_short(False, 32767.0)
        return [conv, blocks.file_sink(gr.sizeof_short, args.output, False)]
    if args.sink == "null":
        return [blocks.null_sink(gr.sizeof_gr_complex)]
    if lo is None:
        raise SystemExit("give --channel or --freq for an SDR sink")
    if args.sink == "osmosdr":
        import osmosdr
        s = osmosdr.sink(args.device or "hackrf=0")
        s.set_sample_rate(args.samp_rate)
        s.set_center_freq(lo, 0)
        if args.ppm:
            s.set_freq_corr(args.ppm, 0)
        s.set_gain(14 if args.amp else 0, "RF", 0)
        s.set_gain(args.gain, "IF", 0)
        s.set_bandwidth(args.bandwidth or default_bandwidth(args), 0)
        return [s]
    from gnuradio import soapy
    dev = args.device or "driver=hackrf"
    driver, _, rest = dev.partition(",")
    s = soapy.sink(driver, "fc32", 1, rest, "", [""], [""])
    s.set_sample_rate(0, args.samp_rate)
    s.set_frequency(0, lo)
    names = list(s.list_gains(0)) if hasattr(s, "list_gains") else []
    if "VGA" in names:
        s.set_gain(0, "VGA", args.gain)
        if "AMP" in names:
            s.set_gain(0, "AMP", 14 if args.amp else 0)
    else:
        s.set_gain(0, args.gain)
    s.set_bandwidth(0, args.bandwidth or default_bandwidth(args))
    return [s]


def install_grc(dest):
    import glob
    import os
    import shutil
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grc")
    dest = os.path.expanduser(dest)
    os.makedirs(dest, exist_ok=True)
    for f in sorted(glob.glob(os.path.join(src, "*.block.yml"))):
        shutil.copy(f, dest)
        print(f"installed {os.path.join(dest, os.path.basename(f))}")
    print("Restart GNU Radio Companion; the blocks are under 'NTSC BTSC'.")
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose > 1 else logging.INFO,
                        format="%(message)s")
    if args.list_channels:
        print(channels.describe(args.band))
        return 0
    if args.install_grc:
        return install_grc(args.install_grc)

    from gnuradio import blocks, gr
    from .blocks import status_probe
    from .media import ffmpeg_available

    if not ffmpeg_available(args.ffmpeg):
        raise SystemExit(f"ffmpeg not found ('{args.ffmpeg}'); install ffmpeg first")

    from .flowgraph import DEFAULT_OFFSET
    if args.offset is None:
        args.offset = DEFAULT_OFFSET
    args.sink = resolve_sink(args.sink)
    if args.samp_rate is None:
        hackrf_soapy = args.sink == "soapy" and "hackrf" in (args.device or "driver=hackrf")
        args.samp_rate = 15e6 if hackrf_soapy else C.FS
    vis = resolve_frequency(args)
    lo = None if vis is None else vis + C.VISUAL_TO_CENTER - args.offset

    if args.priority == "high":
        how = raise_priority()
        if how:
            log.info("priority: %s", how)
        else:
            log.warning("couldn't raise priority (Linux needs an rtprio limit or root)")

    tb = gr.top_block("NTSC BTSC transmitter")
    try:
        src = make_source(args)
    except ValueError as e:
        raise SystemExit(f"error: {e}")
    chain = [src]
    if args.duration:
        chain.append(blocks.head(gr.sizeof_gr_complex, int(args.duration * args.samp_rate)))
    if args.sink in ("file", "null") and (args.realtime or src.reader.live):
        chain.append(blocks.throttle(gr.sizeof_gr_complex, args.samp_rate, True))
    chain += make_sink(tb, args, lo)
    tb.connect(*chain)

    r = src.reader
    log.info("input: %s (%s%s)", args.input, "live" if r.live else "file",
             ", listening" if args.listen else "")
    audio_desc = "mono" if args.mono else "BTSC stereo"
    if r.has_sap:
        audio_desc += " + SAP"
    if not r.has_audio:
        audio_desc += " (no input audio)"
    log.info("audio: %s", audio_desc)
    log.info("output: %s%s", args.sink, "" if src.transport == "pipe" else
             " (Python-block transport)")
    if vis is not None:
        log.info("visual %.4f MHz, aural %.4f MHz, SDR LO %.4f MHz, %.6f Msps",
                 vis / 1e6, (vis + C.AURAL_OFFSET) / 1e6, lo / 1e6, args.samp_rate / 1e6)

    stop = {"flag": False}

    def on_signal(signum, frame):
        if stop["flag"]:
            sys.exit(1)
        stop["flag"] = True
        log.info("stopping...")
        src.stop_input()
        tb.stop()

    pysignal.signal(pysignal.SIGINT, on_signal)
    pysignal.signal(pysignal.SIGTERM, on_signal)

    probe = None if args.quiet else status_probe(r, src)
    t0 = time.monotonic()
    tb.start()
    if probe:
        probe.start()
    try:
        tb.wait()
    finally:
        if probe:
            probe.halt.set()
        src.stop_input()
    log.info("done: %d frames in %.1f s", src.frames_out, time.monotonic() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
