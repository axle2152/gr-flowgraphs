"""The complete NTSC-M + BTSC transmitter as a GNU Radio hierarchical block.

Signal path (complex baseband at 4fsc = 14.318 Msps)::

  media -> NTSC encoder =pipe=> float->complex -> VSB filter [-> shift] -------------+
    |      (envelope, 4fsc)                                                         +-> out
    +----> BTSC encoder ===pipe=> FM -> zero-stuff x26 -> band-pass ----------------+
           (L, R, SAP -> deviation in Hz @ 550.7 kHz)     (at the aural carrier)

The two encoders are numpy code running on their own threads (see
feeders.py); GNU Radio reads their output through pipes, so its own
threads never wait for Python's GIL.

The aural carrier is FM modulated at the composite rate at an alias of its
final frequency, zero-stuffed to 4fsc and band-pass filtered at the real
carrier frequency.  The occupied band (+/- ~170 kHz) is narrower than the
composite rate, so this rebuilds the carrier exactly with one FFT filter.

The visual carrier is negatively amplitude modulated (sync tip = 100 %,
blanking = 75 %, white = 12.5 %) and vestigial-sideband filtered
(-0.75 / +4.2 MHz).  The aural carrier sits exactly 4.5 MHz above it.
Everything comes from one sample clock, so the BTSC pilot is locked to the
line rate as the standard requires.

By default the SDR is tuned to the visual carrier (``channel_offset`` =
+1.75 MHz): the LO leakage of cheap SDRs then hides under the visual carrier
and their I/Q image falls outside the channel.

Other output rates (e.g. 15 Msps for HackRF via SoapySDR, which only takes
whole-MHz rates) are reached without resampling the full-rate complex sum:
the real video envelope goes through a rational resampler before the VSB
filter, and the low-rate BTSC composite is nudged to rate/N (555.6 kHz for
15 Msps, N = 27) so the aural zero-stuffing lands exactly on the output rate.
"""

import shlex
from fractions import Fraction

import numpy as np
from gnuradio import analog, blocks, filter, gr
from scipy import signal

from . import constants as C
from .feeders import AudioFeeder, VideoFeeder, lifecycle_sink, pipe_transport_works
from .media import MediaReader

DEFAULT_OFFSET = C.VISUAL_TO_CENTER    # visual carrier at 0 Hz
MAX_OFFSET = 4.0e6


def _shifted_lowpass(fc, passband, stopband, atten, gain, fs=C.FS):
    numtaps, beta = signal.kaiserord(atten, (stopband - passband) / (fs / 2.0))
    numtaps |= 1
    h = signal.firwin(numtaps, (passband + stopband) / 2.0, window=("kaiser", beta), fs=fs)
    n = np.arange(numtaps) - (numtaps - 1) / 2.0
    return (gain * h * np.exp(2j * np.pi * fc * n / fs)).astype(np.complex64)


def _lowpass(passband, stopband, atten, gain, fs):
    numtaps, beta = signal.kaiserord(atten, (stopband - passband) / (fs / 2.0))
    h = signal.firwin(numtaps, (passband + stopband) / 2.0, window=("kaiser", beta), fs=fs)
    return (gain * h).astype(np.float32)


def vsb_taps(gain=1.0, fs=C.FS, lower=0.75e6, upper=4.2e6, transition=0.3e6, atten=50.0):
    """Complex FIR passing -lower..+upper Hz around the visual carrier (at 0 Hz).

    Zero phase at the carrier.  Upper stop band starts at 4.5 MHz (>= 30 dB
    down at the aural carrier as OET-60 requires); the lower band edge is at
    -1.05 MHz (FCC: -20 dB at -1.25 MHz).
    """
    hw = (upper + lower) / 2.0
    return _shifted_lowpass((upper - lower) / 2.0, hw, hw + transition, atten, gain, fs)


def aural_taps(carrier_hz, gain=1.0, fs=C.FS, stuff=C.BTSC_DECIM, image_spacing=C.BTSC_RATE,
               passband=180e3, atten=60.0):
    """Band-pass at the aural carrier that rebuilds the zero-stuffed FM signal.

    Images of the zero-stuffed carrier are ``image_spacing`` apart (550.7 kHz
    at 4fsc), so the stop band starts at image_spacing - passband."""
    return _shifted_lowpass(carrier_hz, passband, image_spacing - passband, atten,
                            gain * stuff, fs)


def is_native_rate(rate):
    """True for 4fsc, including integer approximations like 14318182 Hz."""
    return abs(rate - C.FS) < 1.0


def resample_ratio(rate):
    """(interp, decim) taking 4fsc (= 315/22 MHz exactly) to ``rate``."""
    if is_native_rate(rate):
        return 1, 1
    r = Fraction(int(round(rate)) * 22, 315_000_000)
    if abs(float(r) * C.FS - rate) > 1e-3:
        raise ValueError("sample rate must be a whole number of Hz")
    return r.numerator, r.denominator


def envelope_resampler_taps(interp, out_rate, video_bw=4.3e6, atten=60.0):
    """Low-pass for the real video envelope, 4fsc -> out_rate.

    Only 0..4.3 MHz matters (the VSB filter after it keeps -0.75..+4.2 MHz
    around the carrier); everything that could alias back into that band is
    rejected."""
    stop = min(C.FS, out_rate) - video_bw
    if stop <= video_bw + 0.3e6:
        raise ValueError("output rate %.3f Msps is too low for NTSC video" % (out_rate / 1e6))
    return _lowpass(video_bw, stop, atten, interp, C.FS * interp)


def aural_plan(rate):
    """How the aural carrier reaches ``rate``.

    Returns (stuff, composite_rate, (interp, decim)): the BTSC composite is
    resampled by interp/decim to composite_rate = rate / stuff, FM
    modulated there and zero-stuffed by ``stuff``.  At 4fsc that is
    (26, 550.7 kHz, (1, 1)); at 15 Msps (27, 555.6 kHz, (572, 567))."""
    if is_native_rate(rate):
        return C.BTSC_DECIM, C.BTSC_RATE, (1, 1)
    centre = int(round(rate / C.BTSC_RATE))
    best = None
    for n in range(max(2, centre - 2), centre + 3):
        fb = rate / n
        if fb < 500e3:              # the FM band (+/- 180 kHz) needs room between images
            continue
        r = Fraction(int(round(rate)) * C.BTSC_DECIM * 22, n * 315_000_000)
        if best is None or r.numerator < best[2][0]:
            best = (n, fb, (r.numerator, r.denominator))
    if best is None:
        raise ValueError("output rate %.3f Msps is too low" % (rate / 1e6))
    return best


def composite_resampler_taps(interp, out_rate, audio_bw=110e3, atten=60.0):
    """Low-pass for the BTSC composite (deviation, up to ~100 kHz with SAP)."""
    stop = min(C.BTSC_RATE, out_rate) - audio_bw
    return _lowpass(audio_bw, stop, atten, interp, C.BTSC_RATE * interp)


class tv_source(gr.hier_block2):
    """NTSC/BTSC TV transmitter source: media in, complex baseband out.

    Output: complex64 at ``samp_rate`` (default :data:`constants.FS`,
    14.318 Msps; the signal is generated at that rate and resampled when
    another rate is asked for, e.g. 15e6 for HackRF via SoapySDR, which only
    takes whole-MHz rates).  The channel centre sits ``channel_offset`` Hz
    above 0 Hz, so tune the SDR to ``visual_carrier + 1.75 MHz -
    channel_offset``.  With the default offset of 1.75 MHz that is simply the
    visual carrier frequency.
    """

    def __init__(self, input="test", channel_offset=DEFAULT_OFFSET, aural_db=-10.0, level=0.9,
                 stereo=True, audio=True, sap=None, sap_track=None, audio_gain_db=0.0,
                 sap_gain_db=0.0, setup=7.5, white_clip=110.0, aspect="pad",
                 interlaced=True, loop=False, live=None, listen=False, latency=0.3,
                 idle="bars", ffmpeg="ffmpeg", input_args="", start=None, reader=None,
                 transport="auto", samp_rate=None, buffer=0.15, priority="normal"):
        gr.hier_block2.__init__(self, "tv_source", gr.io_signature(0, 0, 0),
                                gr.io_signature(1, 1, gr.sizeof_gr_complex))
        f_vis = channel_offset - C.VISUAL_TO_CENTER
        f_aur = f_vis + C.AURAL_OFFSET
        if f_vis < -C.FS / 2 + 1.4e6 or f_aur > C.FS / 2 - 0.4e6:
            raise ValueError("channel_offset %.3f MHz puts the channel outside the %.3f Msps "
                             "band (allowed about %.2f to %.2f MHz)" % (
                                 channel_offset / 1e6, C.FS / 1e6,
                                 (-C.FS / 2 + 1.4e6 + C.VISUAL_TO_CENTER) / 1e6,
                                 (C.FS / 2 - 0.4e6 - C.AURAL_OFFSET + C.VISUAL_TO_CENTER) / 1e6))
        rate = float(samp_rate or C.FS)
        if is_native_rate(rate):
            rate = C.FS
        self.samp_rate = rate
        if max(abs(f_vis - 1.3e6), abs(f_aur + 0.25e6)) > rate / 2 - 0.1e6:
            raise ValueError("output rate %.3f Msps is too low for this channel placement; "
                             "use a higher rate or --offset 0" % (rate / 1e6))
        resample = not is_native_rate(rate)
        if resample:
            v_interp, v_decim = resample_ratio(rate)
            v_taps = envelope_resampler_taps(v_interp, rate)
        stuff, comp_rate, (a_interp, a_decim) = aural_plan(rate)
        if isinstance(input_args, str):
            input_args = shlex.split(input_args)
        if sap == "":
            sap = None
        if sap_track is not None and sap_track < 0:
            sap_track = None
        self.reader = reader or MediaReader(
            input, live=live, loop=loop, listen=listen, aspect=aspect, interlaced=interlaced,
            audio=audio, sap=sap, sap_track=sap_track, latency=latency, ffmpeg=ffmpeg,
            input_args=input_args, start=start, priority=priority)
        aural_amp = 10 ** (aural_db / 20.0)
        scale = level / (1.0 + aural_amp)
        if transport == "auto":
            transport = "pipe" if pipe_transport_works() else "block"
        self.transport = transport

        # -- vision ----------------------------------------------------
        self.video = VideoFeeder(self.reader, setup=setup, white_clip=white_clip, idle=idle,
                                 transport=transport)
        self.video_src = self.video.gr_source()
        vchain = [self.video_src]
        if resample:
            self.v_resampler = filter.rational_resampler_fff(v_interp, v_decim, v_taps.tolist())
            vchain.append(self.v_resampler)
        self.f2c = blocks.float_to_complex(1)
        self.vsb = filter.fft_filter_ccc(1, vsb_taps(scale, fs=rate).tolist())
        vchain += [self.f2c, self.vsb]
        if abs(f_vis) > 0.5:
            self.vrot = blocks.rotator_cc(2 * np.pi * f_vis / rate)
            vchain.append(self.vrot)

        # -- sound -----------------------------------------------------
        self.audio = AudioFeeder(self.reader, stereo=stereo, gain_db=audio_gain_db,
                                 sap_gain_db=sap_gain_db, transport=transport)
        self.audio_src = self.audio.gr_source()
        self.lifecycle = lifecycle_sink(self.reader, [self.video, self.audio])
        achain = [self.audio_src]
        if (a_interp, a_decim) != (1, 1):
            self.a_resampler = filter.rational_resampler_fff(
                a_interp, a_decim, composite_resampler_taps(a_interp, comp_rate).tolist())
            achain.append(self.a_resampler)
        f_alias = f_aur - round(f_aur / comp_rate) * comp_rate
        self.carrier = blocks.add_const_ff(f_alias)
        self.fm = analog.frequency_modulator_fc(2 * np.pi / comp_rate)
        self.stuff = blocks.vector_insert_c([0j] * (stuff - 1), stuff, 0)
        self.aural_bpf = filter.fft_filter_ccc(1, aural_taps(
            f_aur, aural_amp * scale, fs=rate, stuff=stuff, image_spacing=comp_rate).tolist())
        achain += [self.carrier, self.fm, self.stuff, self.aural_bpf]

        self.add = blocks.add_cc()
        if buffer and buffer > 0:
            # a deep last buffer lets the SDR ride through short CPU stalls
            self.add.set_min_output_buffer(int(buffer * rate))
        self.connect(*vchain, (self.add, 0))
        self.connect(*achain, (self.add, 1))
        # the lifecycle block only needs a trickle of samples
        self.trickle = blocks.keep_one_in_n(gr.sizeof_float, 8192)
        self.connect(self.audio_src, self.trickle, self.lifecycle)
        self.connect(self.add, self)

    @property
    def frames_out(self):
        return self.video.frames_out

    def stop_input(self):
        """Stop ffmpeg and the encoders (also unblocks the pipe readers)."""
        self.video.stop()
        self.audio.stop()
        self.reader.stop()
