"""GNU Radio NTSC-M transmitter with BTSC (MTS) stereo and SAP audio.

Handy names for GNU Radio Companion expressions::

    ntsc_btsc.FS                      14318181.8 (4 x fsc) - the output sample rate
    ntsc_btsc.BTSC_RATE               550699.3 - BTSC encoder input/output rate
    ntsc_btsc.visual_carrier(3)       61.25e6  (broadcast channel 3)
    ntsc_btsc.visual_carrier(99, "cable")
"""

from .channels import visual_carrier
from .constants import BTSC_RATE, FH, FS, FSC

__version__ = "0.1.0"
__all__ = ["tv_source", "composite_source", "visual_carrier", "FS", "FSC", "FH", "BTSC_RATE"]


def tv_source(*args, **kwargs):
    """Hierarchical block: media in, 14.318 Msps complex baseband out."""
    from .flowgraph import tv_source as _tv
    return _tv(*args, **kwargs)


def composite_source(input="test", output="ire", setup=7.5, aspect="pad", interlaced=True,
                     loop=False, listen=False, live=None, latency=0.3, idle="bars",
                     white_clip=110.0, input_args=None):
    """NTSC composite video (float, 4fsc) from any ffmpeg input, no sound."""
    from .blocks import ntsc_video_source
    from .media import MediaReader
    reader = MediaReader(input, live=live, loop=loop, listen=listen, aspect=aspect,
                         interlaced=interlaced, audio=False, latency=latency,
                         input_args=input_args)
    return ntsc_video_source(reader, setup=setup, white_clip=white_clip, idle=idle,
                             output=output)
