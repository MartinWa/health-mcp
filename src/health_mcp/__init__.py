"""health-mcp: one MCP endpoint over read-only personal health sources."""

import os
from importlib.metadata import PackageNotFoundError, version

# The release image carries the Git tag in HEALTH_MCP_VERSION (a Docker build arg
# filled from the tag), so the running version names what was deployed even
# though the package metadata is only bumped when a release is cut.
VERSION_ENV = "HEALTH_MCP_VERSION"


def _resolve_version() -> str:
    declared = os.environ.get(VERSION_ENV, "").strip()
    if declared:
        return declared
    try:
        return version("health-mcp")
    except PackageNotFoundError:  # a source tree that was never installed
        return "0.0.0+unknown"


__version__ = _resolve_version()

__all__ = ["VERSION_ENV", "__version__"]
