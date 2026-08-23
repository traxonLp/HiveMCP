"""HiveMCP: PowerPoint, Word and Excel generation for OpenWebUI.

The version lives in ``pyproject.toml`` and is read back from the installed package
metadata rather than written out a second time here. That matters more than it looks:
CI bumps the patch version on every push to main, and a number repeated across several
files is a number that will disagree with itself the first time one of them is missed.
"""

from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("hivemcp")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    # Not an error worth raising. It only happens when the package has not been
    # installed, which is a developer running from a checkout, and every caller of this
    # wants a string to display rather than an exception to handle.
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
