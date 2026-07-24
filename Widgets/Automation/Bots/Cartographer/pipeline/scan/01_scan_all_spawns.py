"""
One authoritative full scan of every spawn point (tag, x, y) on every one of
the 404 known maps -- ALL tags this time, including numeric and the
'0000'/self-name-prefix default markers previously excluded. Earlier work
(legacy/decode_nickname_tags.py, superseded) excluded numeric tags on the assumption that a
numeric tag always means "target map's own id" -- disproven by the Aurora
Glade outpost -> Diessa Lowlands false positive (2026-07-09): that '0013'
tag was a coincidental collision, not a real connection, and slipped
through because numeric tags were never cross-validated the way nickname
tags were.

This is the one shared, reusable cache everything else should build from.

Run: python 01_scan_all_spawns.py
Output: output/all_spawns_raw.json -- {map_id: [[tag, x, y], ...]}
"""
import json
import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR = os.path.dirname(_THIS_DIR)  # this scanner lives in pipeline/scan/; libs + output/ are one level up
sys.path.insert(0, _PIPELINE_DIR)

from lib_gwdat_unpack import GwDatArchive
from lib_ffna_pure import parse_ffna_spawns
from lib_map_file_ids import MAP_ID_TO_DAT_FILE_ID

_DAT_PATH = os.environ.get(
    "GWDAT_PIPELINE_DAT_PATH",
    "/home/alex/git/gw_wine/drive_c/Program Files (x86)/GUILD WARS/Gw.dat",
)
_OUT_JSON = os.path.join(_PIPELINE_DIR, "output", "all_spawns_raw.json")
_LOG_PATH = os.path.join(_PIPELINE_DIR, "output", "scan_all_spawns_log.txt")


def main():
    log_f = open(_LOG_PATH, "w", encoding="utf-8")

    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log_f.write(line + "\n")
        log_f.flush()

    archive = GwDatArchive(_DAT_PATH)
    map_ids = sorted(MAP_ID_TO_DAT_FILE_ID.keys())

    result = {}
    t_start = time.time()
    for i, map_id in enumerate(map_ids):
        if i % 50 == 0:
            log(f"scanning: {i}/{len(map_ids)} ({time.time()-t_start:.0f}s elapsed)")
        fid = MAP_ID_TO_DAT_FILE_ID[map_id]
        try:
            data = archive.read_by_hash(fid)
        except Exception as e:
            log(f"  map_id={map_id}: read error {e!r}")
            continue
        if not data:
            continue
        spawn_result = parse_ffna_spawns(data)
        spawns = (spawn_result[0] + spawn_result[1]) if spawn_result else []
        result[map_id] = [[sp.tag, sp.x, sp.y] for sp in spawns]

    log(f"DONE in {time.time()-t_start:.0f}s. {len(result)} maps, "
        f"{sum(len(v) for v in result.values())} total spawns.")

    os.makedirs(os.path.join(_PIPELINE_DIR, "output"), exist_ok=True)
    with open(_OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f)
    log(f"-> {_OUT_JSON}")


if __name__ == "__main__":
    main()
