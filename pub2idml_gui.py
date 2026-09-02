"""Entry point for the windowed build.

PyInstaller runs its entry script as __main__, which breaks the package
relative imports in pubidml/gui/app.py. Going through this shim keeps the
package context intact, exactly as pub2idml.py does for the console
build.
"""

import sys

from pubidml.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
