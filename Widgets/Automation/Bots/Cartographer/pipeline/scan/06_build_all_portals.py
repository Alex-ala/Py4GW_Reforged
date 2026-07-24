"""
Build a CSV listing every portal marker prop found in every known map,
independent of whether it could be matched to a named wiki exit.

For each portal marker this records the map it's on, its coordinates, its
marker "portal id" (really: the shared marker *model* id -- portal marker
props carry no unique per-instance id in the map data, see
gwdat_tools/output/all_portals.csv notes / project memory), and the nearest
spawn point (if any sits within _SPAWN_TAG_MATCH_RADIUS). That nearest-spawn
link is a proximity heuristic, not a stored relationship -- there is no
field on either record that references the other.

Run: python 06_build_all_portals.py [--limit N] [--dat PATH]
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR = os.path.dirname(_THIS_DIR)  # this scanner lives in pipeline/scan/; libs + output/ are one level up
sys.path.insert(0, _PIPELINE_DIR)

from lib_gwdat_unpack import GwDatArchive
from lib_ffna_pure import parse_ffna_prop_positions, parse_ffna_prop_filenames, parse_ffna_spawns, _PORTAL_MODEL_FILE_IDS
from lib_map_file_ids import MAP_ID_TO_DAT_FILE_ID
from lib_map_names import MAP_ID_TO_NAME

# Same radius used by 02_resolve_edges.py's nearest_portal_prop(), so the
# "nearest spawn" reported here is consistent with what the resolver
# considers "close enough to belong to this portal".
_SPAWN_MATCH_RADIUS = 2000.0

_DEFAULT_DAT_PATH = os.environ.get(
    "GWDAT_PIPELINE_DAT_PATH",
    "/home/alex/git/gw_wine/drive_c/Program Files (x86)/GUILD WARS/Gw.dat",
)
_OUTPUT_CSV = os.path.join(_PIPELINE_DIR, "output", "all_portals.csv")
_LOG_PATH = os.path.join(_PIPELINE_DIR, "output", "all_portals_log.txt")


def get_portal_markers(data: bytes):
    positions = parse_ffna_prop_positions(data)
    if not positions:
        return []
    filenames = parse_ffna_prop_filenames(data)
    markers = []
    for fi, x, y, z in positions:
        if fi >= len(filenames):
            continue
        fid = filenames[fi]
        if fid in _PORTAL_MODEL_FILE_IDS:
            markers.append((x, y, z, fid))
    return markers


def nearest_spawn(px, py, spawns):
    best, best_d = None, None
    for sp in spawns:
        d = math.hypot(sp.x - px, sp.y - py)
        if best_d is None or d < best_d:
            best, best_d = sp, d
    return best, best_d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Only process the first N maps (for testing)")
    ap.add_argument("--dat", default=_DEFAULT_DAT_PATH)
    args = ap.parse_args()

    os.makedirs(os.path.join(_PIPELINE_DIR, "output"), exist_ok=True)
    log_f = open(_LOG_PATH, "w", encoding="utf-8")

    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log_f.write(line + "\n")
        log_f.flush()

    log(f"Opening archive: {args.dat}")
    archive = GwDatArchive(args.dat)
    log(f"MFT loaded: {len(archive.hash_to_entry)} hashes")

    map_ids = sorted(MAP_ID_TO_DAT_FILE_ID.keys())
    if args.limit:
        map_ids = map_ids[: args.limit]

    csv_f = open(_OUTPUT_CSV, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_f, fieldnames=[
        "map_id", "map_name",
        "portal_x", "portal_y", "portal_z",
        "portal_type_id", "portal_type_name",
        "spawn_x", "spawn_y", "spawn_tag", "spawn_distance",
    ])
    writer.writeheader()

    stats = {"maps_ok": 0, "maps_decompress_failed": 0, "maps_no_markers": 0,
              "portals_total": 0, "portals_with_spawn": 0}

    t_start = time.time()
    for i, map_id in enumerate(map_ids):
        map_name = MAP_ID_TO_NAME.get(map_id, f"map_{map_id}")
        file_id = MAP_ID_TO_DAT_FILE_ID[map_id]

        if i % 20 == 0:
            elapsed = time.time() - t_start
            log(f"progress {i}/{len(map_ids)} ({elapsed:.0f}s elapsed) -- {map_name!r}")

        try:
            data = archive.read_by_hash(file_id)
        except Exception as e:
            log(f"  map_id={map_id} {map_name!r}: decompress error: {e!r}")
            stats["maps_decompress_failed"] += 1
            continue

        if not data:
            log(f"  map_id={map_id} {map_name!r}: no data for file_id={file_id}")
            stats["maps_decompress_failed"] += 1
            continue

        markers = get_portal_markers(data)
        if not markers:
            stats["maps_no_markers"] += 1
            stats["maps_ok"] += 1
            continue

        spawn_result = parse_ffna_spawns(data)
        spawns = (spawn_result[0] + spawn_result[1]) if spawn_result else []

        for x, y, z, fid in markers:
            sp, d = nearest_spawn(x, y, spawns)
            if sp is not None and d is not None and d <= _SPAWN_MATCH_RADIUS:
                spawn_x, spawn_y, spawn_tag, spawn_dist = sp.x, sp.y, sp.tag, round(d, 1)
                stats["portals_with_spawn"] += 1
            else:
                spawn_x = spawn_y = spawn_tag = spawn_dist = ""

            writer.writerow({
                "map_id": map_id,
                "map_name": map_name,
                "portal_x": x, "portal_y": y, "portal_z": z,
                "portal_type_id": hex(fid),
                "portal_type_name": _PORTAL_MODEL_FILE_IDS[fid],
                "spawn_x": spawn_x, "spawn_y": spawn_y,
                "spawn_tag": spawn_tag, "spawn_distance": spawn_dist,
            })
            stats["portals_total"] += 1

        stats["maps_ok"] += 1

        if i % 25 == 0:
            csv_f.flush()

    csv_f.close()
    log(f"DONE in {time.time()-t_start:.0f}s. stats={stats}")
    log_f.close()


if __name__ == "__main__":
    main()
