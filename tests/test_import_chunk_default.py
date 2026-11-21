"""Game memory is imported for the GPU in 4 MB chunks by default (gpu/vk/vk_memory.cpp import_chunk; no game/GPU required).
A pool's VRAM mirror covers its import's whole range: with 16 MB chunks 28-39% of the mirrors' bytes were outside every game
pool. RX_VK_IMPORT_CHUNK_MB still sets it, and a pool across a chunk's end is still taken into one import (span_game_pools).

python -I -B tests/test_import_chunk_default.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def block(text, marker):
    start = text.index(marker)
    end = text.index("{", start) + 1
    depth = 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def main():
    mem = (ROOT / "gpu/vk/vk_memory.cpp").read_text(encoding="utf-8")
    chunk = block(mem, "uint64_t import_chunk()")
    if 'getenv("RX_VK_IMPORT_CHUNK_MB")' not in chunk:
        raise SystemExit("vk_memory.cpp: import_chunk() no longer reads RX_VK_IMPORT_CHUNK_MB")
    if ": mirror_on() || kImportBacks ? 4 : 16) << 20;" not in chunk:
        raise SystemExit("vk_memory.cpp: import_chunk()'s default is not 4 MB where the pools have VRAM mirrors or an import "
                         "backs its range in memory (the PS5), and 16 elsewhere (16 left 28-39% of the mirrors outside the "
                         "game's pools; without mirrors and without backing 4 only adds imports)")
    # (the PS5 backs an import's whole range in the title's memory, hos_ps5_commit_now: a chunk's padding costs memory there)
    if "#ifdef __PROSPERO__\nconstexpr bool kImportBacks = true;\n#else\nconstexpr bool kImportBacks = false;\n#endif" not in mem:
        raise SystemExit("vk_memory.cpp: the PS5 no longer imports by 4 MB chunks, or another platform does (kImportBacks)")
    if ": kImportBacks ? 16 : 64) << 20;" not in block(mem, "uint64_t import_span()"):
        raise SystemExit("vk_memory.cpp: import_span()'s default is not 16 MB on the PS5 and 64 elsewhere")
    if "span_game_pools(" not in block(mem, "Pool* pool_lookup(uint64_t guest, uint64_t size) {"):
        raise SystemExit("vk_memory.cpp: pool_lookup() no longer takes a game pool across a chunk's end into one import")
    print("imports by 4 MB chunk by default: ok")


if __name__ == "__main__":
    main()
