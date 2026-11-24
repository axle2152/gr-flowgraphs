# titanic-spark-gap

An RF replica of the **RMS Titanic's wireless transmitter** (call sign MGY):
a Marconi 5 kW synchronous rotary-spark set. The flowgraph makes the damped
waves a spark transmitter actually radiated, spark by spark, keys them in
Morse, and sends them to a HackRF. It also plays them through a 1912-style
envelope detector, so you can hear the result without a receiver.

| | Titanic (1912) | This replica |
|---|---|---|
| Transmitter | Marconi 5 kW synchronous rotary spark | modelled spark by spark, as complex baseband |
| Spark rate | 840 a second: a 300 V, 420 Hz alternator (8-pole, 6300 rpm) and a 16-stud disc discharger, sparking at both peaks of each cycle | 840/s (adjustable) |
| Note heard on a detector | "musical 840 Hz" | 840 Hz and harmonics |
| Wavelength | 600 m (500 kHz), also 325 m | 500 kHz (`tx_freq`) |
| Aerial coupling | jigger (oscillation transformer) | two coupled circuits, coupling k |
| Damping | not recorded; the 1912 US law limited ships to a decrement of 0.2 | decrement 0.2 (adjustable) |
| Keying | the key switched the alternator circuit (about 17 A at 300 V, through a relay) | the key starts and stops whole sparks |

Sources are listed at the end.

## Requirements

- GNU Radio 3.10 with gr-soapy and SoapyHackRF. On MSYS2 that's
  `mingw-w64-ucrt-x86_64-gnuradio` plus `mingw-w64-ucrt-x86_64-soapyhackrf`.
- A HackRF, or none (see [Without a HackRF](#without-a-hackrf)).
- A sound card, for the monitor.

## Running it

Connect the HackRF to the receiver or analyser by coax, through attenuators
(this is closed loop only, see [Legal](#legal)). Open `titanic_spark_gap.grc`
in GNU Radio Companion and run it. Before it starts it prints a warning
saying what else is in the range the replica covers.

The spark goes out at `tx_freq`, 500 kHz, the Titanic's 600 m wavelength.
The HackRF is specified from 1 MHz, but it works at 500 kHz with less
output, which is plenty into coax. It's tuned to `lo_freq` (1 MHz), and
a rotator moves the spark down to `tx_freq`. That keeps the HackRF's own
carrier leak, which sits at the frequency it's tuned to, 500 kHz away from
the spark instead of in the middle of it. A spark has no carrier, and a
leak there would put one back. For the 325 m wavelength, set `tx_freq` to
923e3.

| Control | What it does |
|---|---|
| HackRF VGA (dB) | transmit gain, 0 by default. Keep it low into coax and attenuators |
| Spark rate (/s) | sparks per second, and so the pitch of the note. 840 for the Titanic |
| Speed (WPM) | Morse speed (PARIS timing: a dot is 1.2/WPM seconds) |
| Decrement | how fast each spark dies away: the logarithmic decrement per RF cycle at 500 kHz. 0.2 = the 1912 limit; lower is a sharper, longer ring |
| Jigger coupling k | 0 = one tuned circuit (one hump); higher = tighter coupling, with two humps further apart |
| Spark timing jitter | RMS spark timing error as a fraction of the period. Near 0 is a synchronous disc (a clear note); 0.3 and up sounds like a plain or non-synchronous gap (a rasp) |
| Monitor RX bandwidth, volume | the listening receiver |
| Keying | **Message** loops the text; **Hand key** sparks while you hold the Key button |
| Message | what to send. Press Enter to update. `<SOS>` sends a run-together prosign |

The default message is the first distress call as logged:
`CQD CQD CQD DE MGY 41.46N 50.14W`. Another was
`MKC MKC SOS SOS SOS DE MGY MGY MGY` (MKC was the Olympic). After each pass
it waits 2 s (`repeat_gap` on the block) and starts again.

The **Spectrum** tab shows the signal sent to the HackRF (computed, so it
works without one, but it doesn't show what the HackRF adds). In Message mode
the hump rises and falls with the keying and sits lower than with the key
held, because the plot averages in dB and the key-up gaps pull it down. Tick
**Max Hold** in the side panel to see the whole shape. The plot's input has a
noise floor far below anything visible added (`plot_floor`, display only):
key-up silence is exact zeros, which is minus infinity in dB, and one silent
frame would otherwise blank the averaged plot for good.

The **Scope** tab shows one spark's envelope and a train of them.

### Listening to it

A 1912 operator heard the spark on a crystal, magnetic or valve detector:
an envelope detector. To hear the replica the same way on another radio,
use **AM** with 10 to 30 kHz of bandwidth: an 840 Hz note keyed in Morse.
In SSB or CW mode there's no carrier to beat against, so you hear a rough
hiss or buzz. That's authentic too: a spark is not a carrier.

The flowgraph's own monitor does the AM detection for you. It's a low-pass
filter (the receiver's tuning), an envelope detector and the sound card.

### Without a HackRF

Disable `hackrf_sink`, then enable `throttle` and `no_hackrf`. The throttle
paces the flowgraph at the sample rate, so the monitor and the plots run in
real time.

## How the spark is modelled

Each spark discharges the condenser through the gap into the primary
circuit. The jigger couples that to the aerial circuit, and the aerial
rings with a damped wave that has died away well before the next spark. With
coupling k the two circuits trade energy back and forth, so the wave splits
into two frequencies, f0/√(1+k) and f0/√(1−k), and the aerial current beats
between them:

```
i(t) = e^(−δ·f0·t) · (cos ω1 t − cos ω2 t)     k > 0
i(t) = e^(−δ·f0·t) · cos ω0 t                  k = 0
```

δ is the decrement and f0 is 500 kHz. At δ = 0.2 the amplitude falls by
1/e every 10 µs, so a spark lasts about 90 µs, then there's nothing for
the rest of the 1.19 ms until the next one. The coupled form starts from
zero, because the energy has to cross the jigger first, which is why its
spectrum falls off faster than a single circuit's.

Each spark starts the oscillation afresh, so its RF phase is set by the
moment it fires. A timing error of a few microseconds is many cycles at
500 kHz, so spark to spark the phase is effectively random. The sparks
add up to a smooth, broad hump with no carrier. The note is in the
envelope, which repeats 840 times a second. With the jitter set to 0 you
can see the other case: a comb of lines 840 Hz apart.

The decay and the jigger split are worked out at `orig_freq` (500 kHz), so
the replica takes up the bandwidth the original did wherever `tx_freq` is.
At the defaults (δ 0.2, k 0.1)
it measures 68 kHz wide at −3 dB, 183 kHz at −20 dB and 587 kHz at −40 dB.
With k at 0 it's 31 kHz at −3 dB, but the sharp start of each spark
spreads it much further: −32 dB at ±1 MHz.

The flowgraph is one embedded Python block (`spark_tx`) feeding the HackRF,
the plots and the monitor. The whole flowgraph runs at about 6× real time in
simulation.

## What changed from the first version

The first version (2025) worked differently, and two things were wrong.

**The Morse.** The CW keyer looped its message with no gap at the end. The
last element ran straight into the first: with `CAN YOU READ THIS`, the last
dot of the S joined the first dash of the C into one 4-unit key-down, so the
loop read as "...THIS" + garbled "CAN". There was also a throttle on the
keying path. With a HackRF in the flowgraph that's a second clock: the
throttle runs on the PC's clock and the HackRF on its own, and when they
drift the HackRF underruns and drops pieces of the signal. The new keyer
always ends with at least a word gap (2 s by default) and needs no throttle.

**The RF.** It was Gaussian noise, low-passed to about ±45 kHz, multiplied
by an 860 Hz train of exponentials with a 300 µs time constant, plus 500,
1000 and 1500 Hz tones at 7 %. Two problems:

- **The ring-down was about 30 times too slow.** A 300 µs time constant
  only falls to 2 % by the next spark, so the signal never stopped. It was
  continuous noise with an 860 Hz buzz on it. A real spark at 500 kHz with a
  decrement of 0.2 dies in about 90 µs and is then silent for over a
  millisecond.
- **Noise isn't a damped wave.** Each real spark is a decaying sinusoid at
  the circuit's frequency, not a burst of noise. The spectrum's shape comes
  from the decay and the coupling. The added tones had no physical source.

The spark rate is now 840 a second (it was 860), the keying starts and
stops whole sparks, and there's a hand key.

## What it can't reproduce

- **The far skirts.** A spark's spectrum keeps going. The replica carries
  only what the sample rate holds: 0 to 2 MHz at 2 Msps. To carry more,
  raise `samp_rate` (in whole MHz) and `lo_freq` with it, keeping
  `lo_freq` at `samp_rate`/2 or above so the band doesn't run below 0 Hz.
- **Unrecorded numbers.** The set's decrement, coupling and spark-to-spark
  variation weren't recorded. The defaults are reasonable estimates, and
  each is a slider.
- **The rest of the station.** The 1.5 kW emergency set (an induction coil
  with a plain gap) isn't modelled, though high jitter gets the flavour.
  Neither is the aerial's own response.
- **Power.** A spark is peaky: at the defaults the RMS is about 26 dB below
  the peak, so at the same HackRF gain it's much weaker on average than a
  carrier.

## Legal

Spark (damped-wave) emissions are banned on the air, so this is a
closed-loop project: the HackRF into a dummy load, or into a receiver or
analyser by coax with attenuators (30-60 dB). Never an antenna. The
replica's skirts also fill everything from 0 to 2 MHz: aeronautical and
maritime beacons and maritime radio below 535 kHz, and the AM broadcast
band from 530 kHz.
The flowgraph warns you before it starts (a Python Snippet block,
`tx_check`), saying what the replica covers and whether the gain is high
enough (above 20 dB, or with the +14 dB amp) for an antenna to put it on
the air. It only warns; on a terminal it waits 5 seconds first when the
gain is high.

## Sources

- Titanic radio talk slides (the set, the 420 Hz alternator, 840 Hz, the
  wavelengths, the jigger and the distress calls):
  [TitanicRadio1.pdf](https://naturewind846.github.io/2025/titanic-radio/TitanicRadio1.pdf)
- [The RMS Titanic Radio Page](https://www.qsl.net/ik2wsj/Titanic_radiopage.html)
  (5 kW rotary spark, MGY)
- The 1912 Radio Act's 0.2 decrement limit:
  [1912 act text](https://www.earlyradiohistory.us/1912act.htm), and
  Kolster, Bureau of Standards, 1915
  ([PDF](https://archive.org/download/dir114214551915235235kols/dir114214551915235235kols.pdf))
