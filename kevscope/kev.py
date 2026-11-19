"""Raw .kev decoding: not implemented yet, on purpose.

QNX's public documentation names the pieces of the binary format (16-byte
event slots, a 32-bit header word read with _NTO_TRACE_GETEVENT_C /
_NTO_TRACE_GETEVENT / _NTO_TRACE_GETCPU, simple and combine events, a text
attribute header) but does not publish the bit layout, the numeric constants
or how the text header is separated from the binary data. Writing a decoder
from guesses would produce a tool that looks right and is wrong, so this
module refuses until the layout has been confirmed against a real QNX 8.0
capture, event by event, with `traceprinter` output of the same file as the
reference. Until then use the text path:

    traceprinter -f trace.kev -o trace.txt      # on the target or with the SDP host tools
    python3 -m kevscope convert trace.txt -o trace.json
"""
from __future__ import annotations

from .model import Trace


class KevFormatError(Exception):
    pass


def parse(data: bytes) -> Trace:
    raise KevFormatError(
        "this looks like a raw .kev file, which kevscope cannot decode yet. "
        "Convert it first with: traceprinter -f FILE.kev -o FILE.txt")
