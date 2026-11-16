# crosby-fm-stereo

A GNU Radio transmitter for **Crosby FM-FM stereo**, the 1958 multiplex system
proposed by Murray Crosby. It was one of the systems the FCC considered
before adopting today's pilot-tone stereo in 1961. Instead of a 19 kHz pilot
and an AM subcarrier, the L−R signal frequency-modulates a 50 kHz
subcarrier.

Author: KK4EKN (POWER-FM).

| | |
|---|---|
| Main channel | (L+R)/2 at 50 % of the 75 kHz deviation in stereo, 100 % in mono |
| Stereo subchannel | (L−R)/2 frequency-modulates a 50 kHz subcarrier, ±25 kHz |
| Subcarrier injection | 50 % of the 75 kHz deviation, band-limited to about 19–81 kHz so its sidebands stay out of the mono channel |
| Pilot | none |
| Pre-emphasis | none in the flowgraph; done upstream (e.g. in Stereo Tool) |

Mono listeners hear the L+R channel 6 dB quieter than with a mono
transmitter. That's the price of the Crosby system's 50 % main-channel
injection. The **Stereo** checkbox in the GUI switches to plain mono at full
level and turns the subcarrier off.

## Requirements

- GNU Radio 3.10 with gr-soapy and a HackRF (SoapyHackRF). On MSYS2 that's
  `mingw-w64-ucrt-x86_64-gnuradio` plus `mingw-w64-ucrt-x86_64-soapyhackrf`.
- An audio input at **192 kHz** stereo. The composite is built at the audio
  rate, and the 50 kHz subcarrier with its sidebands needs 192 kHz. Stereo
  Tool into a virtual audio cable works well.

## Running it

Open `crosby_tx.grc` in GNU Radio Companion, set `tx_freq` and `hackrf_vga`,
then run it. The GUI shows the composite spectrum. You should see L+R below
15 kHz and the subcarrier around 50 kHz.

## Signal flow

```
audio 192k ─▶ 15 kHz LPF (L, R) ─┬─▶ (L+R)/2 × main_inj ─▶ delay ─────────────────┐
                                 └─▶ (L−R)/2 ─▶ FM ±25 kHz ─▶ band-limit ±31 kHz ─▶│
                                                 × 50 kHz LO (× sub_inj) ─▶ real ─▶+─▶ composite
composite 192k ─▶ resample to 2 Msps ─▶ FM ±75 kHz ─▶ HackRF (Soapy, whole-MHz rates only)
```

The L+R path is delayed by the subcarrier filter's group delay so the two
line up. The resampling ratio is worked out from `mpx_rate` and `rf_rate`, so
changing either one keeps them consistent.

## Notes

- **Two clocks.** The sound card and the HackRF have separate clocks, so a
  very long run can show an occasional audio overrun or underrun (`aO`/`aU`)
  as they drift apart. That's normal for this kind of flowgraph.
- **Subcarrier bandwidth.** Band-limiting the subcarrier to ±31 kHz is
  narrower than its Carson bandwidth at full deviation (about ±40 kHz).
  Loud, high-frequency stereo content therefore picks up a little
  distortion: about −46 dB THD+N on a 10 kHz left-only tone at 90 % level, in
  simulation. Without that limit the lower sideband would fall into the mono
  channel.
- **Simulated results.** Fed 1 kHz left and 3 kHz right, the output decodes
  with 60–72 dB of stereo separation at the correct levels.

## Legal

Transmit only into a dummy load or a shielded/cabled setup, or within your
local low-power rules. The FM broadcast band is licensed spectrum.
