from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("waboros")
except PackageNotFoundError:  # run from a source tree that is not installed
    __version__ = "0.0.0"
