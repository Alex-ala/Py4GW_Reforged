"""
Enumerate every FFNA_Type3 (pathing/map) file in the archive, then find the
ones NOT already linked to a known map_id via MAP_ID_TO_DAT_FILE_ID -- the
"orphans" that might hold data for one of the 260 named maps missing from
that table (see project memory: MAP_ID_TO_DAT_FILE_ID gap, discovered via
Spearhead Peak 2026-07-09).

Cheap peek-only classification, same approach as classify_all_files.py.

Run: python 02_find_orphan_maps.py
Output: output/orphan_map_files.csv (hash, offset, size)
"""
import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR = os.path.dirname(_THIS_DIR)  # this scanner lives in pipeline/scan/; libs + output/ are one level up
sys.path.insert(0, _PIPELINE_DIR)

from lib_gwdat_unpack import GwDatArchive, decompress, GwDatDecompressError
from lib_map_file_ids import MAP_ID_TO_DAT_FILE_ID

_DAT_PATH = os.environ.get(
    "GWDAT_PIPELINE_DAT_PATH",
    "/home/alex/git/gw_wine/drive_c/Program Files (x86)/GUILD WARS/Gw.dat",
)
_OUT_CSV = os.path.join(_PIPELINE_DIR, "output", "orphan_map_files.csv")
_LOG_PATH = os.path.join(_PIPELINE_DIR, "output", "find_orphan_maps_log.txt")


def main():
    log_f = open(_LOG_PATH, "w", encoding="utf-8")

    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log_f.write(line + "\n")
        log_f.flush()

    archive = GwDatArchive(_DAT_PATH)
    entries = archive.entries
    log(f"{len(entries)} total MFT entries")

    known_file_ids = {v for v in MAP_ID_TO_DAT_FILE_ID.values() if v is not None}
    log(f"{len(known_file_ids)} file_ids already linked to a known map_id")

    type3_hashes = []
    t_start = time.time()
    for i, entry in enumerate(entries):
        if i % 20000 == 0:
            log(f"progress {i}/{len(entries)} ({time.time()-t_start:.0f}s elapsed)")
        if not entry.b or entry.size < 8:
            continue
        try:
            archive._f.seek(entry.offset)
            raw = archive._f.read(entry.size)
            peek = decompress(raw, max_output=8) if entry.a else raw[:8]
        except (GwDatDecompressError, Exception):
            continue
        if len(peek) < 5:
            continue
        import struct
        magic = struct.unpack_from("<I", peek, 0)[0]
        sub_type = peek[4]
        if magic == 0x616e6666 and sub_type == 3:  # 'ffna' + pathing subtype
            type3_hashes.append(entry.hash)

    log(f"DONE scanning in {time.time()-t_start:.0f}s. {len(type3_hashes)} FFNA_Type3 files total.")

    orphans = sorted({h for h in type3_hashes if h is not None} - known_file_ids)
    log(f"{len(orphans)} orphan map files (not linked to any known map_id)")

    import csv
    os.makedirs(os.path.join(_PIPELINE_DIR, "output"), exist_ok=True)
    with open(_OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["hash"])
        for h in orphans:
            w.writerow([h])
    log(f"-> {_OUT_CSV}")


if __name__ == "__main__":
    main()
