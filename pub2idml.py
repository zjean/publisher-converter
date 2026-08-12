"""Entry point for both direct execution and PyInstaller bundling.

PyInstaller runs its entry script as __main__, which breaks the package
relative imports in pubidml/cli.py. Going through this shim keeps the
package context intact.
"""

from pubidml.cli import main

if __name__ == "__main__":
    main()
