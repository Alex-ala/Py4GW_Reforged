"""
V2 pipeline, stage 1: build the map-adjacency GRAPH SKELETON from wiki
data alone (per user design 2026-07-19). Deliberately does not look at
spawn tags, portal props, or pathing data at all -- those only ever
identify the wiki graph's own recorded EDGES, never invent new adjacency
("wiki adjacency implies near-perfect geometry" -- the wiki is the map of
truth for "does an edge exist", other sources are just noisy hints about
"where exactly does it sit"). See v1's whole tag-corroboration/nickname-
voting machinery (08_build_connection_graph.py) for what this deliberately
avoids: that approach let numeric/nickname spawn tags define the graph's
EXISTENCE, which is what produced averaging-dilution and same-navmesh
disagreement bugs stage 2 has to work around instead of never causing.

An edge is undirected (map A borders map B) but the wiki source is
directional per-page (A's own page lists B, and usually but not always
B's own page lists A back) -- both directions recorded as one edge, with
a flag for whether the wiki confirmed it from both sides or just one
(one-sided is still trusted, just flagged, consistent with the project's
general "wiki adjacency is close to ground truth" stance).

The wiki data has no way to express "two separate physical connections
between the same map pair" (a page's exit list is a set of named
neighbors, not a count) -- multiplicity, if it's real anywhere, can only
be discovered later, at stage 2, when geometric evidence finds two
clearly-disjoint valid portal positions for one wiki edge. This stage's
output structure allows for that (edges is a growable list, not a fixed
node-pair set), stage 2 is what would actually append a second entry.

Run: python 01_wiki_graph.py
Output: output_v2/wiki_graph.json -- {"edges": [{"map_a", "map_b",
"both_sides_confirmed", "direction_a_to_b", "direction_b_to_a"}, ...]}
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # pipeline/ (this file's own dir, 2026-07-20 v2->standard promotion)

from lib_map_file_ids import MAP_ID_TO_DAT_FILE_ID
from lib_map_names import MAP_ID_TO_NAME, resolve_map_id_by_name

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR = _THIS_DIR  # this file now lives directly in pipeline/, not pipeline/v2/
_WIKI_EXITS_JSON = os.path.join(_PIPELINE_DIR, "output", "wiki_exits_cache.json")
_OUT_DIR = os.path.join(_THIS_DIR, "output")
_OUT_GRAPH = os.path.join(_OUT_DIR, "wiki_graph.json")
_OUT_UNRESOLVED_NAMES = os.path.join(_OUT_DIR, "wiki_unresolved_names.csv")

_KNOWN_IDS = set(MAP_ID_TO_DAT_FILE_ID) | set(MAP_ID_TO_NAME)


def build_graph():
    with open(_WIKI_EXITS_JSON, encoding="utf-8") as f:
        wiki_exits = json.load(f)

    # per-directed-mention: map_id -> {neighbor_id: direction_or_None}
    directed: dict[int, dict[int, str | None]] = {}
    unresolved_names: list[tuple[int, str]] = []

    for map_id_str, entry in wiki_exits.items():
        map_id = int(map_id_str)
        for exit_ in entry.get("exits", []):
            title = exit_["target_title"]
            nid = resolve_map_id_by_name(title, prefer_ids=_KNOWN_IDS)
            if nid is None:
                unresolved_names.append((map_id, title))
                continue
            if nid == map_id:
                continue  # self-reference, not a real edge
            directed.setdefault(map_id, {})[nid] = exit_.get("direction")

    # collapse directed mentions into undirected edges, tracking whether
    # both sides independently confirmed it
    seen_pairs: dict[frozenset, dict] = {}
    for a, neighbors in directed.items():
        for b, direction in neighbors.items():
            key = frozenset((a, b))
            rec = seen_pairs.setdefault(key, {
                "map_a": min(a, b), "map_b": max(a, b),
                "direction_a_to_b": None, "direction_b_to_a": None,
            })
            if a == rec["map_a"]:
                rec["direction_a_to_b"] = direction
            else:
                rec["direction_b_to_a"] = direction

    edges = []
    for rec in seen_pairs.values():
        a_to_b_seen = rec["map_a"] in directed and rec["map_b"] in directed.get(rec["map_a"], {})
        b_to_a_seen = rec["map_b"] in directed and rec["map_a"] in directed.get(rec["map_b"], {})
        edges.append({
            "map_a": rec["map_a"],
            "map_b": rec["map_b"],
            "both_sides_confirmed": a_to_b_seen and b_to_a_seen,
            "direction_a_to_b": rec["direction_a_to_b"],
            "direction_b_to_a": rec["direction_b_to_a"],
        })
    edges.sort(key=lambda e: (e["map_a"], e["map_b"]))

    os.makedirs(_OUT_DIR, exist_ok=True)
    with open(_OUT_GRAPH, "w", encoding="utf-8") as f:
        json.dump({"edges": edges}, f, indent=1)

    with open(_OUT_UNRESOLVED_NAMES, "w", encoding="utf-8", newline="") as f:
        f.write("map_id,map_name,unresolved_target_title\n")
        for map_id, title in unresolved_names:
            name = MAP_ID_TO_NAME.get(map_id, f"map_{map_id}")
            safe = f'"{title}"' if "," in title else title
            f.write(f"{map_id},{name},{safe}\n")

    nodes = {m for e in edges for m in (e["map_a"], e["map_b"])}
    one_sided = sum(1 for e in edges if not e["both_sides_confirmed"])
    print(f"wiki graph: {len(edges)} undirected edges, {len(nodes)} nodes "
          f"(of {len(wiki_exits)} maps with any wiki page data)")
    print(f"  {one_sided} edges confirmed from only one side's page "
          f"(still trusted, just flagged)")
    print(f"  {len(unresolved_names)} exit names failed to resolve to a "
          f"known map_id -> {_OUT_UNRESOLVED_NAMES}")
    print(f"  -> {_OUT_GRAPH}")
    return edges


if __name__ == "__main__":
    build_graph()
