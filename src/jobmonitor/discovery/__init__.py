from .a16z import A16ZSource
from .base import DiscoveryResult, DiscoverySource
from .techstars import TechstarsSource
from .yc import YCSource

SOURCES: dict[str, DiscoverySource] = {
    s.name: s for s in (YCSource(), TechstarsSource(), A16ZSource())
}

__all__ = ["SOURCES", "DiscoveryResult", "DiscoverySource"]
