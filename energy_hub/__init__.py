"""energy-hub: unified home energy sensor aggregator."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("energy-hub")
except PackageNotFoundError:   # running from a plain checkout without installation
    __version__ = "0+unknown"
