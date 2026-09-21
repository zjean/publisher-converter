"""Microsoft Publisher (.pub) to Adobe IDML conversion.

Parsing is delegated to libmspub through the bundled `pubdump` binary,
which emits a JSON event stream. This package rebuilds a document model
from that stream and writes an IDML package that Affinity can open.
"""

#: 1.2 changes nothing about how a file converts: every .pub comes out of
#: this release exactly as it came out of 1.1. What changed is the handing
#: over. The windowed build ships zipped as well as bare, because the
#: browser warns before it will save an .exe and a mail system or a shared
#: drive refuses one often enough that the download never arrives at the
#: person it was written for.
__version__ = "1.2.0"
