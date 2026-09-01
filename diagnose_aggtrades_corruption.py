"""Diagnose multi-member gzip corruption in the aggTrades dataset (read-only)."""
from __future__ import annotations

import zlib
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent / "data" / "binance_spot_aggtrades_btc_eth_2025-01.jsonl.gz"
MAGIC = b"\x1f\x8b\x08"


def main() -> int:
    raw = DATA_PATH.read_bytes()
    print(f"FILE_SIZE_BYTES: {len(raw)}", flush=True)
    view = memoryview(raw)

    offset = 0
    member_index = 0
    total_lines = 0
    last_good_line = None
    CHUNK = 4 * 1024 * 1024
    while offset < len(raw):
        if bytes(view[offset:offset + 3]) != MAGIC:
            print(f"NO_GZIP_MAGIC_AT_OFFSET_{offset}: bytes={bytes(view[offset:offset + 8])!r}")
            break
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        pos = offset
        pending_tail = b""
        member_lines = 0
        member_failed = None
        while pos < len(raw):
            piece = bytes(view[pos:pos + CHUNK])
            try:
                data = decompressor.decompress(piece)
            except zlib.error as exc:
                member_failed = (pos, exc)
                break
            pos += len(piece) - len(decompressor.unused_data)
            lines = (pending_tail + data).split(b"\n")
            pending_tail = lines.pop()
            for line in lines:
                if line.strip():
                    total_lines += 1
                    member_lines += 1
                    last_good_line = line.decode("utf-8", errors="replace")
            if decompressor.unused_data:
                break
            if not piece:
                break
        if member_failed is not None:
            fail_pos, exc = member_failed
            print(f"MEMBER {member_index}: offset={offset} FAILED near byte {fail_pos}: {exc}")
            print(f"LAST_GOOD_LINE_BEFORE_CORRUPTION: {last_good_line}")
            print(f"TOTAL_VALID_LINES_BEFORE_CORRUPTION: {total_lines}")
            print(f"CORRUPTION_OFFSET_BYTES: approx {fail_pos} of {len(raw)}")
            return 1
        print(f"MEMBER {member_index}: offset={offset} consumed_bytes={pos - offset} lines={member_lines} cumulative={total_lines}", flush=True)
        if pos <= offset:
            print(f"NO_PROGRESS_AT_OFFSET_{offset}, stopping to avoid infinite loop")
            break
        offset = pos
        member_index += 1

    print(f"ALL_MEMBERS_VALID: True")
    print(f"TOTAL_LINES: {total_lines}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
