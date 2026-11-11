"""Home Automation core package."""
import sys

# Interpreter guard. Every test and CLI
# imports this package, so the wrong interpreter fails here, once and clearly, instead of producing
# scattered spurious errors. `unittest discover -s tests` never imports tests/__init__.py, so the
# guard cannot live there.
if sys.version_info < (3, 12):
    raise RuntimeError(
        f"Home Automation needs python3.12; running {sys.executable} {sys.version.split()[0]}"
    )

__version__ = "0.1.0"
