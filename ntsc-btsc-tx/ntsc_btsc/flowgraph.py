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


def vsb_taps(gain=1.0, lower=0.75e6, upper=4.2e6, transition=0.3e6, atten=50.0):
    """Complex FIR passing -lower..+upper Hz around the visual carrier (at 0 Hz).

    Zero phase at the carrier.  Upper stop band starts at 4.5 MHz (>= 30 dB
    down at the aural carrier as OET-60 requires); the lower band edge is at
    -1.05 MHz (FCC: -20 dB at -1.25 MHz).
    """
    hw = (upper + lower) / 2.0
    return _shifted_lowpass((upper - lower) / 2.0, hw, hw + transition, atten, gain)


def aural_taps(carrier_hz, gain=1.0, passband=180e3, stopband=370e3, atten=60.0):
    """Band-pass at the aural carrier that rebuilds the zero-stuffed FM signal.

    Images of the zero-stuffed carrier are spaced 550.7 kHz apart, so the
    stop band must begin before 550.7 - 180 = 370 kHz from the carrier."""
    return _shifted_lowpass(carrier_hz, passband, stopband, atten, gain * C.BTSC_DECIM)


def resample_ratio(rate):
    """(interp, decim) taking 4fsc (= 315/22 MHz exactly) to ``rate``."""
    r = Fraction(int(round(rate)) * 22, 315_000_000)
    if abs(float(r) * C.FS - rate) > 1e-3:
        raise ValueError("sample rate must be a whole number of Hz")
    return r.numerator, r.denominator


def resampler_taps(interp, out_rate, f_lo, f_hi, atten=60.0):
    """Low-pass for 4fsc -> out_rate that keeps f_lo..f_hi (the channel) and
    rejects every image that would alias onto it."""
    edge = max(abs(f_lo), abs(f_hi)) + 0.1e6
    stop = min(C.FS, out_rate) - edge
    if stop <= edge + 0.2e6:
        raise ValueError("output rate %.3f Msps is too low for this channel placement"
                         % (out_rate / 1e6))
    rate = C.FS * interp
    numtaps, beta = signal.kaiserord(atten, (stop - edge) / (rate / 2.0))
    h = signal.firwin(numtaps, (edge + stop) / 2.0, window=("kaiser", beta), fs=rate)
    return (h * interp).astype(np.float32)


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
                 transport="auto", samp_rate=None):
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
        self.samp_rate = float(samp_rate or C.FS)
        resample = abs(self.samp_rate - C.FS) > 1e-3
        if resample:
            interp, decim = resample_ratio(self.samp_rate)
            rs_taps = resampler_taps(interp, self.samp_rate, f_vis - 1.3e6, f_aur + 0.25e6)
        if isinstance(input_args, str):
            input_args = shlex.split(input_args)
        if sap == "":
            sap = None
        if sap_track is not None and sap_track < 0:
            sap_track = None
        self.reader = reader or MediaReader(
            input, live=live, loop=loop, listen=listen, aspect=aspect, interlaced=interlaced,
            audio=audio, sap=sap, sap_track=sap_track, latency=latency, ffmpeg=ffmpeg,
            input_args=input_args, start=start)
        aural_amp = 10 ** (aural_db / 20.0)
        scale = level / (1.0 + aural_amp)
        if transport == "auto":
            transport = "pipe" if pipe_transport_works() else "block"
        self.transport = transport

        # -- vision ----------------------------------------------------
        self.video = VideoFeeder(self.reader, setup=setup, white_clip=white_clip, idle=idle,
                                 transport=transport)
        self.video_src = self.video.gr_source()
        self.f2c = blocks.float_to_complex(1)
        self.vsb = filter.fft_filter_ccc(1, vsb_taps(scale).tolist())
        vchain = [self.video_src, self.f2c, self.vsb]
        if abs(f_vis) > 0.5:
            self.vrot = blocks.rotator_cc(2 * np.pi * f_vis / C.FS)
            vchain.append(self.vrot)

        # -- sound -----------------------------------------------------
        self.audio = AudioFeeder(self.reader, stereo=stereo, gain_db=audio_gain_db,
                                 sap_gain_db=sap_gain_db, transport=transport)
        self.audio_src = self.audio.gr_source()
        self.lifecycle = lifecycle_sink(self.reader, [self.video, self.audio])
        f_alias = f_aur - round(f_aur / C.BTSC_RATE) * C.BTSC_RATE
        self.carrier = blocks.add_const_ff(f_alias)
        self.fm = analog.frequency_modulator_fc(2 * np.pi / C.BTSC_RATE)
        self.stuff = blocks.vector_insert_c([0j] * (C.BTSC_DECIM - 1), C.BTSC_DECIM, 0)
        self.aural_bpf = filter.fft_filter_ccc(1, aural_taps(f_aur, aural_amp * scale).tolist())

        self.add = blocks.add_cc()
        self.connect(*vchain, (self.add, 0))
        self.connect(self.audio_src, self.carrier, self.fm, self.stuff, self.aural_bpf,
                     (self.add, 1))
        # the lifecycle block only needs a trickle of samples
        self.trickle = blocks.keep_one_in_n(gr.sizeof_float, 8192)
        self.connect(self.audio_src, self.trickle, self.lifecycle)
        if resample:
            self.resampler = filter.rational_resampler_ccf(interp, decim, rs_taps.tolist())
            self.connect(self.add, self.resampler, self)
        else:
            self.connect(self.add, self)

    @property
    def frames_out(self):
        return self.video.frames_out

    def stop_input(self):
        """Stop ffmpeg and the encoders (also unblocks the pipe readers)."""
        self.video.stop()
        self.audio.stop()
        self.reader.stop()
