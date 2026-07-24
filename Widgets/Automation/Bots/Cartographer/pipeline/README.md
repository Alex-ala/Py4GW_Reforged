# Portal-graph pipeline

Offline pipeline that figures out, for every pair of Guild Wars maps that
border each other, exactly where the connecting portal sits — straight
from the game's own archive data, no live game needed.

## What it requires

Only two things come from outside this pipeline:

- **The Gw.dat archive** (read via `lib_gwdat_unpack.py`'s `GwDatArchive`),
  for pathing-mesh geometry (trapezoids), spawn points, and portal-prop
  positions. Path is `GWDAT_PIPELINE_DAT_PATH` if set, else a hardcoded
  default install location.
- **wiki.guildwars.com**, for each map's neighbor list (which maps border
  which — the pipeline never invents adjacency, only locates it).

Everything else the resolver needs is a **cache file scanned once from
those two sources**, by the scripts in `scan/` (numbered, run in order).
Each writes into `output/` and is safe to skip once its output already
exists — only rerun a step if that specific cache file is missing or
needs refreshing (e.g. after a game update changes map data):

| Step | Produces | Needs |
|---|---|---|
| `scan/01_scan_all_spawns.py` | `all_spawns_raw.json` | Gw.dat |
| `scan/02_find_orphan_maps.py` | `orphan_map_files.csv` | Gw.dat |
| `scan/03_scan_orphan_spawns.py` | `orphan_spawns_raw.json` | Gw.dat, step 2 |
| `scan/04_match_orphan_maps.py` | `orphan_map_matches.csv` | steps 1, 3 |
| `scan/05_merge_expanded_spawns.py` | `all_spawns_expanded.json` | steps 1, 3, 4 |
| `scan/06_build_all_portals.py` | `all_portals.csv` — every real portal-marker prop, linked to its nearest spawn tag | Gw.dat |
| `scan/07_find_portal_pairs_by_transform.py` | `candidate_portal_pairs_by_transform.csv` / `_template_default.csv` — decorative portal-gate props whose placement data matches byte-for-byte on both sides of a real connection (an independent corroborating signal) | Gw.dat, caches its own intermediate so a rerun is fast unless `--rescan` is passed |
| `07_fetch_wiki_adjacency.py` (top level, not `scan/`) | `wiki_exits_cache.json` — every map's neighbor list | wiki.guildwars.com |

`scan/02`→`03`→`04`→`05` is a real chain (each needs the previous step's
output); `01`, `06`, `07`, and the top-level wiki fetch are independent of
each other and of that chain.

## What it does

Wiki pages say which maps border each other, but not where the portal
actually is. The pipeline resolves the *position* two ways depending on
whether the pair shares one underlying map file or not:

- **Same file** (e.g. an outpost carved out of its own explorable area):
  no coordinate transform needed — trusts spawn tags directly for a
  simple two-member pair, or refuses to guess for a 3+-way shared file
  where tags can't tell which neighbor a spawn is about.
- **Different files**: matches whole pathing trapezoids by shape
  (top width, bottom width, height) between the two maps' local
  neighborhoods, anchored on a real portal prop when one is close enough
  to trust. A matching trapezoid pair gives the rigid translation between
  the two maps' coordinate grids directly; re-projecting every nearby
  trapezoid through that translation and counting matches gives a
  confidence score. Below a minimum confidence, the pair is left
  unresolved rather than guessed.

## Steps

0. `scan/01`-`07` and `07_fetch_wiki_adjacency.py` (see above) — only if a
   needed cache file in `output/` doesn't exist yet.
1. `01_wiki_graph.py` — builds the map-adjacency skeleton from
   `wiki_exits_cache.json` alone (wiki adjacency is the only source of
   truth for whether an edge *exists*; nothing else is trusted for that).
   → `output/wiki_graph.json`
2. `02_resolve_edges.py` — for every edge from step 1, resolves the actual
   portal position as described above. Cross-file resolution runs in
   parallel across a worker pool. → `output/resolved_v2.csv`,
   `output/unresolved_v2.csv`
3. (optional) `debug/` holds visualization tools that read the resolved
   data afterward — an interactive HTML graph browser, a merged map-chain
   PNG renderer, and a one-off diagnostic renderer. None of these are
   needed to produce the graph itself.

Run everything directly from `pipeline/` (the `scan/` scripts too — they
resolve their own paths up to `pipeline/output/` regardless of current
directory). In practice step 0 essentially never needs rerunning once its
cache files exist; only steps 1-2 (and 3 for visualization) get rerun
regularly as the resolver itself improves.

## Cache data it generates

`output/resolved_v2.csv` is the actual product: one row per direction of
every resolved connection —
`map_id, map_name, neighbor_map_id, neighbor_name, portal_x, portal_y, method, confidence, source`.
`output/unresolved_v2.csv` lists every wiki-documented pair that couldn't
be resolved confidently, with a reason. `output/wiki_graph.json` is the
intermediate adjacency skeleton from step 1.

## How the bot uses this data

`resolved_v2.csv` is not read directly by the bot — it has to be copied
by hand to
`Widgets/Automation/Bots/Cartographer/data/portal_graph_v2.csv`, the file
the widget actually loads at runtime (`lib/portal_graph.py`). That module
exposes the graph as an adjacency lookup: given a map, which other maps
it connects to and at what position. The Cartographer widget uses this
for route planning (shortest path between two maps), no-go-zone placement
around portals during coverage/vanquish walking, and list-mode grouping —
copying a freshly-regenerated CSV into place is a deliberate, manual step,
never automatic.
