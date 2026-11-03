from ..enums import ATSKind
from .ashby import AshbyAdapter
from .base import ATSAdapter, BoardSnapshot
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter

ADAPTERS: dict[ATSKind, ATSAdapter] = {
    ATSKind.GREENHOUSE: GreenhouseAdapter(),
    ATSKind.LEVER: LeverAdapter(),
    ATSKind.ASHBY: AshbyAdapter(),
}


def board_from_url(url: str) -> tuple[ATSKind, str] | None:
    for kind, adapter in ADAPTERS.items():
        board = adapter.board_from_url(url)
        if board:
            return kind, board
    return None


__all__ = ["ADAPTERS", "ATSAdapter", "BoardSnapshot", "board_from_url"]
