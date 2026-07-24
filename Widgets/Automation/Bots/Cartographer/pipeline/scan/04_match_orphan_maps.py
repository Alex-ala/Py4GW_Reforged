"""
Match orphan map files (FFNA_Type3 files not linked to any known map_id --
see 02_find_orphan_maps.py) to one of the 260 named maps missing from
MAP_ID_TO_DAT_FILE_ID.

First attempt used each missing map's wiki-documented neighbor set as the
target fingerprint -- but wiki_exits_cache.json was only ever built for the
404 already-known maps, so all 260 missing ones had an empty neighbor set
and every match scored 0 (not a real absence of connections, just missing
wiki data for this specific subset).

Better signal, no new wiki data needed: scan the 404 KNOWN maps' own spawn
tags for numeric tags whose value is one of the 260 missing map ids (e.g.
Tasca's Demise and The Granite Citadel both have a spawn tagged '0093' --
direct evidence Spearhead Peak, id 93, is a real connected map). Call the
map declaring this a "voter" for that missing id. Then for each orphan,
check whether ITS OWN numeric tags point back at any of a missing map's
voters -- a real reciprocal handshake (voter says "I connect to T", orphan
says "I connect to voter") is strong evidence the orphan IS T.

Run: python 04_match_orphan_maps.py
Output: output/orphan_map_matches.csv -- one row per missing map with its
best candidate orphan and score, for manual review before trusting any.
"""
import csv
import json
import os
import sys
from collections import defaultdict

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR = os.path.dirname(_THIS_DIR)  # this scanner lives in pipeline/scan/; libs + output/ are one level up
sys.path.insert(0, _PIPELINE_DIR)

from lib_map_file_ids import MAP_ID_TO_DAT_FILE_ID
from lib_map_names import MAP_ID_TO_NAME

_ALL_SPAWNS_JSON = os.path.join(_PIPELINE_DIR, "output", "all_spawns_raw.json")
_ORPHAN_SPAWNS_JSON = os.path.join(_PIPELINE_DIR, "output", "orphan_spawns_raw.json")
_OUT_CSV = os.path.join(_PIPELINE_DIR, "output", "orphan_map_matches.csv")


def main():
    with open(_ALL_SPAWNS_JSON, encoding="utf-8") as f:
        all_spawns = json.load(f)
    with open(_ORPHAN_SPAWNS_JSON, encoding="utf-8") as f:
        orphan_spawns = json.load(f)  # {hash_str: [[tag,x,y], ...]}

    missing_map_ids = [mid for mid in MAP_ID_TO_NAME if mid not in MAP_ID_TO_DAT_FILE_ID]
    print(f"{len(missing_map_ids)} missing map_ids to try to match")
    print(f"{len(orphan_spawns)} orphans available")

    # voters_of[missing_id] = set of KNOWN map_ids with a numeric tag == missing_id
    voters_of = defaultdict(set)
    for map_id_str, spawns in all_spawns.items():
        map_id = int(map_id_str)
        for tag, x, y in spawns:
            if tag.isdigit() and int(tag) in set(missing_map_ids):
                voters_of[int(tag)].add(map_id)

    print(f"{len(voters_of)} missing maps have at least one known voter")

    # orphan_targets[hash] = set of numeric tag targets found in that orphan's own spawns
    orphan_targets = {}
    for h, spawns in orphan_spawns.items():
        targets = set()
        for tag, x, y in spawns:
            if tag.isdigit():
                targets.add(int(tag))
        orphan_targets[h] = targets

    rows = []
    matched_orphans = set()
    for mid in missing_map_ids:
        voters = voters_of.get(mid, set())
        best_hash, best_score, best_voters_hit = None, 0, set()
        for h, targets in orphan_targets.items():
            overlap = targets & voters
            score = len(overlap)
            if score > best_score:
                best_hash, best_score, best_voters_hit = h, score, overlap
        rows.append({
            "map_id": mid, "map_name": MAP_ID_TO_NAME[mid],
            "known_voters": ";".join(f"{MAP_ID_TO_NAME.get(v,v)}({v})" for v in voters),
            "best_orphan_hash": best_hash or "",
            "match_score": best_score,
            "matched_voter_names": ";".join(f"{MAP_ID_TO_NAME.get(v,v)}({v})" for v in best_voters_hit),
            "orphan_spawn_count": len(orphan_spawns.get(best_hash, [])) if best_hash else 0,
        })
        if best_hash and best_score > 0:
            matched_orphans.add(best_hash)

    rows.sort(key=lambda r: -r["match_score"])
    with open(_OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "map_id", "map_name", "known_voters", "best_orphan_hash",
            "match_score", "matched_voter_names", "orphan_spawn_count",
        ])
        w.writeheader()
        w.writerows(rows)

    confident = sum(1 for r in rows if r["match_score"] >= 2)
    weak = sum(1 for r in rows if r["match_score"] == 1)
    none_ = sum(1 for r in rows if r["match_score"] == 0)
    print(f"\n{confident} missing maps matched with score>=2 (confident)")
    print(f"{weak} matched with score==1 (weak, single-voter overlap)")
    print(f"{none_} matched with score==0 (no voter found or no orphan matches)")
    print(f"-> {_OUT_CSV}")


if __name__ == "__main__":
    main()
