"""Microsoft Publisher (.pub) to Adobe IDML conversion.

Parsing is delegated to libmspub through the bundled `pubdump` binary,
which emits a JSON event stream. This package rebuilds a document model
from that stream and writes an IDML package that Affinity can open.
"""

#: 1.1 is where the document setup arrived: a 3mm bleed, the page trimmed
#: to the standard it was drawn a hair off, and the ruler in the unit the
#: file was laid out in. All three change the output of every file against
#: 1.0, and `--bleed 0` and `--no-page-snap` are how to have the old
#: behaviour back.
__version__ = "1.1.0"
