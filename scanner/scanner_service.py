from __future__ import annotations

from dataclasses import asdict

from scanner.scanner_repository import ScannerRepository


class ScannerService:
    def __init__(self, repository: ScannerRepository) -> None:
        self._repository = repository

    def snapshot(self) -> dict:
        return {"items": [asdict(asset) for asset in self._repository.list_assets()]}
