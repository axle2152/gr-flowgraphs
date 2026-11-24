# fm-multipath-sim

A GNU Radio test bench for **FM multipath and picket-fencing**, and for
repairing it with a blind channel equalizer. It puts an FM broadcast signal
through a simulated two-path channel (direct path plus a moving reflection),
adds noise, then receives it: CMA equalizer, FM demodulator, stereo decoder,
sound card. You hear the result and watch the nulls sweep across the band,
before and after the equalizer.

| | |
|---|---|
| Source | live 192 kHz composite (MPX) from a sound card, FM-modulated at ±75 kHz, 2 Msps. A 2 Msps IQ recording can be used instead |
| Channel | direct path + one reflection with adjustable delay, depth and Doppler (flutter), plus AWGN |
| Receiver | 200 kHz channel filter to 400 kHz, AGC, CMA equalizer, quadrature demod |
| Stereo decoder | pilot-squaring (no PLL), DSB-SC demod of L−R, matrix, 75 µs de-emphasis |
| Audio out | 48 kHz stereo |

## Requirements

- GNU Radio 3.10 with the Qt GUI. On MSYS2 that's
  `mingw-w64-ucrt-x86_64-gnuradio`. No SDR hardware is needed.
- A 192 kHz mono composite MPX signal on an audio input, e.g. Stereo Tool's
  MPX output into a virtual audio cable. The flowgraph's `audio_source_0` is
  set to `Hi-Fi Cable Output`; change `device_name` to suit your system.

## Running it

Open `fm_multipath_sim.grc` in GNU Radio Companion and run it. The GUI has:

| Control | What it does |
|---|---|
| Reflection Depth | strength of the reflection: 0 = none, 1 = as strong as the direct path (full nulls) |
| Flutter Rate (Hz) | Doppler offset of the reflection, so how many times a second each frequency is swept by a null. Negative sweeps the other way |
| Reflection Delay (µs) | extra path length of the reflection. The nulls are spaced 1/delay apart: 4 µs puts them 250 kHz apart. 0.5 µs steps (one sample at 2 Msps) |
| Noise Voltage | AWGN floor |
| Signal | level of the signal (both paths) before the noise |
| LPR Delay | samples that line L+R up with L−R. Leave at 513 unless you change the decoder's filters |
| Stereo Level | L−R gain. 1 = correct stereo, 0 = mono |

The two spectrum plots show the faded signal at the channel output and the
signal after the equalizer. Start with Reflection Depth at 0, then raise it
to 0.5–0.9 and listen for picket-fencing (a fast flutter or swish, worst on
stereo).

### Using an IQ recording instead

`file_source` reads a 2 Msps complex IQ recording (`E:\iqrecord.iq`; point it
at your own). It's disabled. To use it, enable `file_source` and disable
`audio_source_0`, `rational_resampler_xxx_1` and
`analog_frequency_modulator_fc_0`. The recording needs a single FM station
centred at 0 Hz.

## Signal flow

```
MPX 192k ─▶ resample 125/12 ─▶ FM ±75 kHz (2 Msps) ─▶ × Signal ─┬──────────────────────────────▶ + ─▶ AWGN ─▶ channel out
   (or IQ file, 2 Msps) ────────────────────────────────────────┘ ─▶ delay ─▶ × depth·e^{j2π·flutter·t} ─┘

channel out ─▶ 200 kHz LPF, ÷5 (400k) ─▶ AGC ─▶ CMA equalizer ─▶ quad demod ─▶ stereo decoder ─▶ 48 kHz L/R

stereo decoder (400k):
  composite ─▶ 18–20 kHz BPF ─▶ Hilbert ─▶ z² ─▶ Im ÷ |z|² × −2 ─▶ 38 kHz carrier ──┐
  composite ─▶ 22 kHz HPF ─▶ 32-sample delay ─────────────────────────────────────▶ × ─▶ 15 kHz LPF ÷8 ─▶ L−R
  composite ─▶ 513-sample delay ─▶ 15 kHz LPF ÷8 ─▶ L+R
  (L+R ± L−R)/2 ─▶ 75 µs de-emphasis ─▶ resample 24/25 ─▶ sound card
```

The reflection is the same signal, delayed and multiplied by a rotating
phasor. The rotation is a Doppler shift, which is what makes the nulls in the
channel's frequency response sweep across the band rather than sit still.

## The equalizer

`epy_block_0` is an embedded Python block-adaptive CMA (constant modulus)
equalizer. An FM signal has a constant envelope; multipath makes the envelope
dip and ripple, and CMA adjusts its taps to make it constant again. Settings:
21 taps, mu 1.0, 64-sample blocks, leak 1e-7, modulus 1.0.

It's hardened against blowing up: the error is clipped, the step size has a
floor on its denominator during deep fades, the taps are rescaled above a
norm of 4 and reset to a passthrough above 8. The freeze (`freeze_thresh`,
off at 0) has hysteresis so it can't latch itself on.

With `debug` at 200 it prints its tap norm, average error, freeze state and
reset count about 30 times a second:

- `|w|` climbing above about 2 means the equalizer is the problem.
- `|w|` staying near 1 while the audio misbehaves means the AGC ahead of it
  is (see below).

Set `debug` to 0 to turn the printing off.

## Stereo decoder

The 38 kHz carrier comes from squaring the pilot instead of a PLL. The pilot
is band-passed, made analytic with a Hilbert filter (z), and squared, which
doubles its frequency. The imaginary part of z² is the carrier, inverted.
Dividing by |z|² makes its level independent of how much pilot the station
sends (anything from 8 % to 10 % is normal), and the −2 in `lmr_gain` turns it
into 2·sin(38 kHz), the carrier a DSB-SC demodulator needs.

The composite going into the DSB multiplier has to arrive at the same time as
the carrier: the 22 kHz high-pass and the pilot band-pass are both 963 taps,
so `lmr_align` adds the Hilbert filter's 32 samples. The L+R path is then
delayed by the full 481 + 32 = 513. One sample out at 38 kHz is 34° of
carrier phase, so these have to be exact.

Simulated, with a left-only tone and no channel impairments, the decoder on
its own gives 75–78 dB separation from 1 to 14 kHz, at any pilot level and
phase. The whole flowgraph (RF, noise at 0.02, equalizer) gives about 42 dB at
1 kHz. With a 4 µs reflection flickering at 6 Hz, separation falls to about
28 dB at a depth of 0.5 and 14 dB at 0.9: that's the picket-fencing.

## Notes

- **AGC wind-up.** `analog_agc_xx_0` has `max_gain` 65536. During a deep
  fade it can wind up and overshoot when the signal comes back. If you see
  that (with `|w|` near 1), cap it at about 8.
- **No throttle.** The sound card sets the pace. When using the IQ file,
  the audio sink sets it, so the file plays in real time.
- **Fixes since the first version.** The direct path used to come from the IQ
  file while the reflection came from the live MPX, so the two paths carried
  different signals. Now both come from one source. The stereo decoder had
  three faults that together gave about −5 dB separation (partly swapped
  channels): the 22 kHz high-pass was designed for 2 Msps but runs at 400 kHz
  (so it cut at 4.4 kHz and had 4819 taps), the doubled pilot was 90° off
  the subcarrier, and L+R was 2146 samples (5.4 ms) out of line with L−R. The
  Stereo Level slider wasn't connected; it now sets the L−R gain.
