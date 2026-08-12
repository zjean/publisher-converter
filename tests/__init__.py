"""Test suite.

Run with `python -m unittest discover` (or `make test`). Standard library
only, matching the project's own constraint.
"""

import logging

# The converter logs failures deliberately; tests provoke plenty of them
# and the tracebacks would drown the results.
logging.getLogger("pubidml").setLevel(logging.CRITICAL)
logging.getLogger("pubidml").addHandler(logging.NullHandler())
