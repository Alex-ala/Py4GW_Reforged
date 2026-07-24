"""
Scan spawn points (tag, x, y) for every "orphan" map file found by
02_find_orphan_maps.py -- FFNA_Type3 pathing files not linked to any known
map_id. Mirrors 01_scan_all_spawns.py's extraction, just keyed by raw file
hash instead of map_id, since orphans have no map_id yet.

This was previously a reproducibility gap: output/orphan_spawns_raw.json
was read by 04_match_orphan_maps.py, 05_merge_expanded_spawns.py and
06_build_all_maps_spawns_csv.py, but no checked-in script generated it
(the file on disk came from an untracked one-off run). This script fills
that gap.

Run: python 03_scan_orphan_spawns.py
Output: output/orphan_spawns_raw.json -- {hash: [[tag, x, y], ...]}
"""
import csv
import json
import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR = os.path.dirname(_THIS_DIR)  # this scanner lives in pipeline/scan/; libs + output/ are one level up
sys.path.insert(0, _PIPELINE_DIR)

from lib_gwdat_unpack import GwDatArchive
from lib_ffna_pure import parse_ffna_spawns

_DAT_PATH = os.environ.get(
    "GWDAT_PIPELINE_DAT_PATH",
    "/home/alex/git/gw_wine/drive_c/Program Files (x86)/GUILD WARS/Gw.dat",
)
_ORPHAN_HASHES_CSV = os.path.join(_PIPELINE_DIR, "output", "orphan_map_files.csv")
_OUT_JSON = os.path.join(_PIPELINE_DIR, "output", "orphan_spawns_raw.json")
_LOG_PATH = os.path.join(_PIPELINE_DIR, "output", "scan_orphan_spawns_log.txt")


def main():
    log_f = open(_LOG_PATH, "w", encoding="utf-8")

    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log_f.write(line + "\n")
        log_f.flush()

    with open(_ORPHAN_HASHES_CSV, newline="", encoding="utf-8") as f:
        hashes = [int(row["hash"]) for row in csv.DictReader(f)]
    log(f"{len(hashes)} orphan file hashes to scan")

    archive = GwDatArchive(_DAT_PATH)

    result = {}
    t_start = time.time()
    for i, h in enumerate(hashes):
        if i % 50 == 0:
            log(f"scanning: {i}/{len(hashes)} ({time.time()-t_start:.0f}s elapsed)")
        try:
            data = archive.read_by_hash(h)
        except Exception as e:
            log(f"  hash={h}: read error {e!r}")
            continue
        if not data:
            continue
        spawn_result = parse_ffna_spawns(data)
        spawns = (spawn_result[0] + spawn_result[1]) if spawn_result else []
        result[str(h)] = [[sp.tag, sp.x, sp.y] for sp in spawns]

    log(f"DONE in {time.time()-t_start:.0f}s. {len(result)} orphan files, "
        f"{sum(len(v) for v in result.values())} total spawns.")

    os.makedirs(os.path.join(_PIPELINE_DIR, "output"), exist_ok=True)
    with open(_OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f)
    log(f"-> {_OUT_JSON}")


if __name__ == "__main__":
    main()
