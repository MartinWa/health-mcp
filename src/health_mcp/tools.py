"""Print exactly what the server registers for an exposure set.

The only sanctioned source for the README tool table and the ZeroClaw
`auto_approve` list. Names come from the source modules and the composites,
never from a hand-maintained copy.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from . import sources as registry
from .config import Settings
from .server import COMPOSITE_EXPOSURE


def all_exposure() -> dict[str, frozenset[str]]:
    merged: dict[str, frozenset[str]] = dict(registry.exposure())
    merged.update(COMPOSITE_EXPOSURE)
    return merged


def exposure_for(settings: Settings, *, all_sources: bool = False) -> dict[str, frozenset[str]]:
    if all_sources:
        return all_exposure()
    modules = registry.configured_modules(settings)
    if not modules:
        return {}
    merged: dict[str, frozenset[str]] = {}
    for spec in modules:
        merged.update(spec.exposure)
    merged.update(COMPOSITE_EXPOSURE)
    return merged


def names(
    settings: Settings,
    *,
    agent: str | None = None,
    prefix: str = "",
    all_sources: bool = False,
) -> list[str]:
    exposure = exposure_for(settings, all_sources=all_sources)
    selected = [name for name, agents in exposure.items() if agent is None or agent in agents]
    return sorted(f"{prefix}{name}" for name in selected)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m health_mcp.tools")
    parser.add_argument(
        "--names",
        action="store_true",
        help="print the registered tool names for this deployment",
    )
    parser.add_argument("--agent", choices=registry.AGENTS, default=None)
    parser.add_argument("--prefix", default="")
    parser.add_argument("--json", action="store_true", help="print a JSON array")
    parser.add_argument(
        "--all-sources",
        action="store_true",
        help="ignore which sources are configured and list the full surface",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    tool_names = names(
        settings,
        agent=args.agent,
        prefix=args.prefix,
        all_sources=args.all_sources,
    )
    if not args.all_sources and not registry.configured_modules(settings):
        print(
            "health_mcp.tools: no sources configured; use --all-sources for the full surface",
            file=sys.stderr,
        )
    if args.json:
        print(json.dumps(tool_names, indent=2))
    else:
        print("\n".join(tool_names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
