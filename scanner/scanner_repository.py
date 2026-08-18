from __future__ import annotations

from collections.abc import Iterable

from scanner.scanner_models import ScannerAsset


class ScannerRepository:
    def __init__(self, assets: Iterable[ScannerAsset] | None = None) -> None:
        self._assets = list(assets or [])

    def list_assets(self) -> list[ScannerAsset]:
        return self._assets
