"""System constants for NTSC-M video and BTSC (MTS) audio.

Everything is derived from the colour subcarrier so that the line rate,
the BTSC pilot (= line rate) and the 4.5 MHz intercarrier are all locked
to one sample clock, as the standards require.
"""

# --- NTSC (SMPTE 170M) ------------------------------------------------------
FSC = 315e6 / 88.0              # colour subcarrier, 3.579545... MHz
FS = 4.0 * FSC                  # master sample rate, 14.318181... MHz (4fsc)
FH = 2.0 * FSC / 455.0          # line rate, 15734.2657... Hz
LINE = 910                      # samples per line at 4fsc
LINES = 525                     # lines per frame
FRAME = LINE * LINES            # samples per frame (477750)
HALF = LINE // 2                # samples per half line (455)
FRAME_RATE = FS / FRAME         # 29.97002997... Hz

US = FS / 1e6                   # samples per microsecond

# Picture geometry handed to us by ffmpeg (4:1:1 so chroma is already
# reduced to a quarter of the luma sample rate, ~3.58 Msps).
WIDTH = 752                     # active samples per line (52.52 us)
HEIGHT = 480                    # active lines per frame
ACTIVE_START = 136              # first active sample, relative to 0H

# RF / channel
AURAL_OFFSET = 4.5e6            # aural carrier above the visual carrier
VISUAL_TO_CENTER = 1.75e6       # channel centre above the visual carrier

# --- BTSC -------------------------------------------------------------------
BTSC_DECIM = 26                 # composite rate = FS / 26
BTSC_RATE = FS / BTSC_DECIM     # 550699.3 Hz, exactly 35 x fH
BTSC_PILOT_PERIOD = 35          # pilot period in composite samples
# ffmpeg resamples the audio straight to the composite rate.  It only takes
# integer rates, so this is 0.55 ppm slow (about 2 ms per hour of A/V drift
# on a file; live inputs are re-synchronised by the jitter buffer anyway).
AUDIO_RATE = int(BTSC_RATE)     # 550699 Hz

DEV_MAIN = 25e3                 # (L+R) 100% deviation, Hz
DEV_PILOT = 5e3                 # pilot deviation, Hz
DEV_STEREO = 50e3               # (L-R) subchannel 100% deviation, Hz
DEV_SAP = 15e3                  # SAP subcarrier injection, Hz
SAP_SUB_DEV = 10e3              # SAP subcarrier FM deviation, Hz

assert abs(BTSC_RATE - 35 * FH) < 1e-6
