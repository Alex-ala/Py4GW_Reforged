"""
Merge the 404 known maps' spawn data with the 18 newly-recovered maps
(matched to an orphan file, see 04_match_orphan_maps.py) into one expanded
spawn dataset, keyed by real map_id -- so 08_build_connection_graph.py can
run its numeric-tag validation + nickname triangulation over the larger,
more complete corpus without any other changes.

Run: python 05_merge_expanded_spawns.py
Output: output/all_spawns_expanded.json
"""
import csv
import json
import os
import sys
from collections import defaultdict

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR = os.path.dirname(_THIS_DIR)  # this scanner lives in pipeline/scan/; libs + output/ are one level up
sys.path.insert(0, _PIPELINE_DIR)

_ALL_SPAWNS_JSON = os.path.join(_PIPELINE_DIR, "output", "all_spawns_raw.json")
_ORPHAN_SPAWNS_JSON = os.path.join(_PIPELINE_DIR, "output", "orphan_spawns_raw.json")
_MATCHES_CSV = os.path.join(_PIPELINE_DIR, "output", "orphan_map_matches.csv")
_OUT_JSON = os.path.join(_PIPELINE_DIR, "output", "all_spawns_expanded.json")

MIN_SCORE = 1


def main():
    with open(_ALL_SPAWNS_JSON, encoding="utf-8") as f:
        merged = json.load(f)
    with open(_ORPHAN_SPAWNS_JSON, encoding="utf-8") as f:
        orphan_spawns = json.load(f)

    matches = list(csv.DictReader(open(_MATCHES_CSV, encoding="utf-8")))
    by_orphan = defaultdict(list)
    for r in matches:
        if r["best_orphan_hash"] and int(r["match_score"]) >= MIN_SCORE:
            by_orphan[r["best_orphan_hash"]].append(r)

    added = 0
    for h, claimants in by_orphan.items():
        if len(claimants) != 1:
            continue  # collision, excluded -- same rule as build_all_maps_spawns_csv.py
        map_id = claimants[0]["map_id"]
        merged[map_id] = orphan_spawns.get(h, [])
        added += 1

    with open(_OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(merged, f)

    print(f"{len(merged)} maps in expanded dataset ({added} newly added) -> {_OUT_JSON}")


if __name__ == "__main__":
    main()
