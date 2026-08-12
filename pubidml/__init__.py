"""Microsoft Publisher (.pub) to Adobe IDML conversion.

Parsing is delegated to libmspub through the bundled `pubdump` binary,
which emits a JSON event stream. This package rebuilds a document model
from that stream and writes an IDML package that Affinity can open.
"""

__version__ = "0.1.0"
