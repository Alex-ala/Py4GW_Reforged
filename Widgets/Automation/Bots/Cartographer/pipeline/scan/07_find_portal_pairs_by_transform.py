"""
Two-stage portal-pair finder.

Stage 1 (collect): scan every known map file's PropInfo chunk, keep every
instance of the known/candidate "portal gate" model family, and record its
full raw transform block alongside its position -- writes/reads
output/all_portal_props.csv, the complete "all portal data" collected.
Cached: if that CSV already exists it's loaded directly instead of
re-reading Gw.dat (a full scan takes several minutes). Pass --rescan to
force a fresh scan (e.g. after changing CANDIDATE_MODEL_FILE_IDS).

Stage 2 (pair): group those collected props by (model_fid, core_bytes) --
i.e. every number matches, not just the transform bytes alone (an earlier
version grouped by core_bytes only, which let two unrelated pairs using
different models but a coincidentally-shared transform collide into one
false 4-member group). Clean cross-map 2-member groups are the pair
candidates; everything else (singletons, same-map dupes, 3+ groups) is a
leftover to investigate by hand.

Template-default filter (added 2026-07-12, see below): even a clean
2-member group can be a coincidence, not a real pair, if BOTH its scale and
mystery-trailing-float are a reused placement-tool default rather than
values chosen for that specific instance. Clean pairs are split into
`output/candidate_portal_pairs_by_transform.csv` (unique scale+mystery --
high confidence) and `output/candidate_portal_pairs_template_default.csv`
(reused scale+mystery -- still worth checking, but a coincidental 2-way
collision on a common default is far more plausible than on a bespoke one).

Rationale (found 2026-07-12, live in-game investigation): confirmed real
portal pairs (Henge of Denravi<->Tangle Root, Beetletun<->Nebo Terrace,
Droknar's Forge<->Talus Chute, D'Alessio Seaboard<->North Kryta Province,
Lion's Arch<->North Kryta Province) all have BYTE-IDENTICAL core_bytes on
both sides -- same rotation, same scale, same mystery trailing float, to
full float32 precision. Working hypothesis: level designers copy the same
instance transform when placing a matching portal-gate pair, and that shows
up as an exact match on this byte blob even though there is no explicit
link-ID field anywhere in the format.

This is NOT proven as a universal mechanism -- treat results as strong
CANDIDATES to verify (e.g. against wiki adjacency or by walking there), not
gospel. Genuine same-model coincidental collisions happen at a rate tied
directly to how often a (scale, mystery) combo is reused as a placement
default -- see the Droknar's Forge prop 28 / Ice Caves of Sorrow prop 28 /
Talus Chute prop 64 case, and the D'Alessio/Lion's Arch/North Kryta Province
case: both share their scale+mystery values with OTHER, unrelated props
elsewhere in the corpus at completely different rotations -- confirming
those specific numbers are a reused template default, not evidence of a
link. (model, core_bytes) grouping alone does not catch this; the
template-default filter below does.

Stage 3 (reachability disambiguation of 3+-member groups, added 2026-07-12
after two of these groups were resolved live in-game): a transform group is
actually ONE physical gate in the shared world-grid -- mapping every
member's position through the group's grid-aligned translations lands them
all on the SAME world coordinate. Each map file whose world-window covers
that region gets its own copy of the gate prop (outposts carve out
surrounding explorable geometry), so a 3-member group = one real gate + one
decorative carve-out copy. The record data of the copies is identical by
construction, and even the surrounding navmesh can be copied too (the
Droknar's Forge case scored a perfect narrow-radius pathing match against
both real members), so neither the prop record nor local geometry can tell
them apart. What DOES tell them apart, validated on both live-confirmed
ground-truth groups: distance from the prop to its own map's
spawn-reachable pathing mesh (trapezoid-adjacency BFS from the map's spawn
points). Every confirmed-functional portal prop sits <=~400u from its map's
reachable navmesh; both confirmed decorative copies sit 6,201u and 17,826u
away. Groups where exactly 2 members survive this filter are emitted as
resolved pairs in output/candidate_portal_pairs_disambiguated.csv.
"""
import argparse
import csv
import math
import os
import struct
import sys
from collections import defaultdict, deque

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR = os.path.dirname(_THIS_DIR)  # this scanner lives in pipeline/scan/; libs + output/ are one level up
sys.path.insert(0, _PIPELINE_DIR)

from lib_gwdat_unpack import GwDatArchive
from lib_ffna_pure import (
    _parse_chunks, is_ffna_pathing, CHUNK_TYPE_PROPINFO,
    parse_ffna_prop_filenames, parse_ffna_pathing, parse_ffna_spawns,
)
from lib_map_file_ids import MAP_ID_TO_DAT_FILE_ID
from lib_map_names import MAP_ID_TO_NAME

_DAT_PATH = os.environ.get(
    "GWDAT_PIPELINE_DAT_PATH",
    "/home/alex/git/gw_wine/drive_c/Program Files (x86)/GUILD WARS/Gw.dat",
)
_OUT_ALL = os.path.join(_PIPELINE_DIR, "output", "all_portal_props.csv")
_OUT_PAIRS = os.path.join(_PIPELINE_DIR, "output", "candidate_portal_pairs_by_transform.csv")
_OUT_PAIRS_TEMPLATE = os.path.join(_PIPELINE_DIR, "output", "candidate_portal_pairs_template_default.csv")
_OUT_PAIRS_DISAMBIGUATED = os.path.join(_PIPELINE_DIR, "output", "candidate_portal_pairs_disambiguated.csv")
_OUT_PAIRS_GEOMETRIC = os.path.join(_PIPELINE_DIR, "output", "candidate_portal_pairs_geometric.csv")
_OUT_PAIRS_REJECTED = os.path.join(_PIPELINE_DIR, "output", "candidate_portal_pairs_rejected.csv")
_OUT_LEFTOVERS = os.path.join(_PIPELINE_DIR, "output", "portal_props_unmatched.csv")

_ALL_FIELDNAMES = [
    "map_id", "map_name", "sub_map_id", "sub_map_name", "prop_id", "model_fid", "x", "y", "z",
    "rot_cos", "rot_sin", "scale", "mystery", "core_bytes_hex",
]

# The full portal-gate model family, matching GWToolboxpp's
# IsPortalModelFileId whitelist (PathingMapDataLoader.cpp) exactly:
# 0xA825 (classic Prophecies/Factions gate), 0xE723 (variant, 98%
# byte-identical file; also common as plain scenery -- the (model,
# core_bytes) match is the real filter, not the model id alone), 0x4E6B2
# (EotN Asura Gate), 0x3C5AC (EotN/Nightfall), plus 0x858B / 0x1C533 /
# 0x5E77A (added 2026-07-12 from the GWToolboxpp list after several known
# real connections -- Snake Dance<->Grenth's Footprint, Deldrimor
# Bowl<->Anvil Rock, Sage Lands<->Mamnoon Lagoon -- turned out to use gate
# variants missing from the original 4-model set).
CANDIDATE_MODEL_FILE_IDS = {0x0A825, 0xE723, 0x4E6B2, 0x3C5AC, 0x858B, 0x1C533, 0x5E77A}

# file_id -> every map_id that shares that file (shared-navmesh sub-region
# groups like Cursed Lands/Bergen/Nebo Terrace or Camp Rankor/Snake Dance).
FILE_TO_MAP_IDS: dict[int, list[int]] = defaultdict(list)
for _mid, _fid in MAP_ID_TO_DAT_FILE_ID.items():
    FILE_TO_MAP_IDS[_fid].append(_mid)

# If a (scale, mystery) combo co-occurs with at least this many DISTINCT
# rotation values across the whole corpus, treat it as a reused
# placement-tool default rather than a value chosen for one specific
# instance -- see module docstring for the two live-confirmed cases this
# is based on.
TEMPLATE_ROTATION_THRESHOLD = 3

# A prop counts as FUNCTIONAL in its own map only if it sits within this
# distance of the map's spawn-reachable pathing mesh. Ground truth: all
# live-confirmed functional portal props measured 22-363u; both confirmed
# decorative carve-out copies measured 6,201u and 17,826u -- a huge gap, so
# the exact threshold isn't sensitive. Note the raw "is the nearest
# trapezoid in the BFS set" flag alone is too strict (the adjacency BFS
# only follows same-plane adjacent1-4 links, not cross-plane portal
# records, so reachable sets are underestimates) -- distance to the
# nearest reachable trapezoid is the robust form. Raised 1000 -> 2500
# after The Black Curtain's real Kessex Peak gate measured 1801u (BFS
# underestimate on a big multi-plane explorable); false copies still
# start at 6201u, so the gap holds.
REACHABLE_DIST_THRESHOLD = 2500.0

# Stage-4 narrow-radius geometric match: local navmesh edges within this
# radius of each prop are tested for a single grid-snapped translation
# mapping one side onto the other (a real gate pair sits at the same
# world-grid point, so the surrounding walkable geometry matches exactly).
# Calibration on ground truth at radius 500: real pair 1.000 (30/30
# edges), unrelated gates 0.067 (2/30) and 0.250 (2/8) -- so require both
# a high agreement ratio AND a minimum number of edges considered (the
# 2/8=0.25 case shows tiny samples can inflate the ratio; at radius 200 a
# degenerate 1/1=1.000 was observed).
GEOM_RADII = (500.0, 1000.0, 2000.0)  # some gates sit 1-3k from the nearest
                                      # copied navmesh (same reason the v2
                                      # pipeline needed EDGE_SEARCH_RADIUS=
                                      # 6000), so escalate until a radius
                                      # passes
GEOM_MIN_CONF = 0.8
GEOM_MIN_EDGES = 8
# Dilution-tolerant alternate gate: at larger radii a REAL match's
# agreement ratio has a natural ceiling well below 1.0 (asymmetric terrain
# with no counterpart on the other side -- same effect the v2 pipeline hit,
# where a hand-verified perfect match scored 0.476 at radius 6000), while
# FALSE matches keep tiny absolute match counts (measured 2-4 edges at
# every radius). So accept moderate ratio + solid absolute count too:
# ground truth Sage Lands<->Mamnoon Lagoon scores 0.762 (16/21) at r=1000
# and 0.653 (49/75) at r=2000, both passing this gate; no measured false
# combo comes close on matched-count.
GEOM_ALT_MIN_CONF = 0.55
GEOM_ALT_MIN_MATCHED = 12


def parse_props_raw(data: bytes):
    if not is_ffna_pathing(data):
        return []
    chunks = _parse_chunks(data)
    chunk_data = None
    for ct, _cl, cd in chunks:
        if ct == CHUNK_TYPE_PROPINFO:
            chunk_data = cd
            break
    if chunk_data is None or len(chunk_data) < 12:
        return []
    off = 10
    num_props = struct.unpack_from("<H", chunk_data, off)[0]
    off += 2
    props = []
    for idx in range(num_props):
        if off + 48 > len(chunk_data):
            break
        filename_index = struct.unpack_from("<H", chunk_data, off)[0]
        x, y, z = struct.unpack_from("<fff", chunk_data, off + 2)
        core_bytes = chunk_data[off + 14 : off + 47]
        num_trailing = chunk_data[off + 47]
        trailing_start = off + 48
        off = trailing_start + num_trailing * 8
        props.append((idx, filename_index, x, y, z, core_bytes))
    return props


# Numeric-tagged spawns farther than this from the prop don't attribute --
# fall back to the file's primary (lowest-id, usually the explorable) map.
# Guards against files where a sub-map has NO numeric spawns of its own
# (e.g. the Grenth's Footprint/Deldrimor War Camp file contains only
# '0206' War Camp spawns, so nearest-tag attribution mislabeled every
# gate on that file as War Camp, including Grenth's own Snake Dance gate
# on the far side of the map).
ATTRIB_MAX_DIST = 5000.0


def _attribute_sub_map(file_id: int, primary_map_id: int, spawns, x: float, y: float) -> int:
    """For files hosting multiple map_ids (shared-navmesh sub-regions),
    attribute a prop to the sub-map whose numeric-tagged arrival spawn is
    nearest (each sub-map's own arrival spawns carry that sub-map's id as
    their tag, e.g. '0155' for Camp Rankor inside the Rankor/Snake Dance
    shared file), but only within ATTRIB_MAX_DIST. Heuristic: arrival
    spawns cluster right at gates, and the two facing spawns of one gate
    sit close together, so attribution near a boundary can be off by one --
    fine for labeling, don't over-trust."""
    hosted = FILE_TO_MAP_IDS.get(file_id, [])
    if len(hosted) < 2 or not spawns:
        return primary_map_id
    hosted_set = set(hosted)
    best_mid, best_d = primary_map_id, None
    for s in spawns:
        tag = s.tag or ""
        if not tag.isdigit():
            continue
        mid = int(tag)
        if mid not in hosted_set:
            continue
        d = (s.x - x) ** 2 + (s.y - y) ** 2
        if best_d is None or d < best_d:
            best_d, best_mid = d, mid
    if best_d is not None and math.sqrt(best_d) > ATTRIB_MAX_DIST:
        return primary_map_id
    return best_mid


def _scan_all_portal_props() -> list[dict]:
    archive = GwDatArchive(_DAT_PATH)
    seen_files: dict[int, int] = {}
    rows: list[dict] = []

    unique_map_ids = sorted(set(MAP_ID_TO_DAT_FILE_ID.keys()))
    total = len(unique_map_ids)
    for n, map_id in enumerate(unique_map_ids, 1):
        file_id = MAP_ID_TO_DAT_FILE_ID[map_id]
        if file_id in seen_files:
            continue
        seen_files[file_id] = map_id
        try:
            data = archive.read_by_hash(file_id)
        except Exception:
            continue
        if not data:
            continue
        filenames = parse_ffna_prop_filenames(data)
        if not filenames or not (set(filenames) & CANDIDATE_MODEL_FILE_IDS):
            continue
        sp = parse_ffna_spawns(data)
        spawns = (sp[0] + sp[1]) if sp else []
        for idx, fidx, x, y, z, core_bytes in parse_props_raw(data):
            if fidx >= len(filenames):
                continue
            model_fid = filenames[fidx]
            if model_fid not in CANDIDATE_MODEL_FILE_IDS:
                continue
            axis_x, axis_y, axis_z, rot_cos, rot_sin, axis_w, scale, mystery = struct.unpack_from(
                "<8f", core_bytes, 0
            )
            sub_map_id = _attribute_sub_map(file_id, map_id, spawns, x, y)
            rows.append({
                "map_id": map_id,
                "map_name": MAP_ID_TO_NAME.get(map_id, f"map_{map_id}"),
                "sub_map_id": sub_map_id,
                "sub_map_name": MAP_ID_TO_NAME.get(sub_map_id, f"map_{sub_map_id}"),
                "prop_id": idx,
                "model_fid": model_fid,
                "x": x, "y": y, "z": z,
                "rot_cos": rot_cos, "rot_sin": rot_sin, "scale": scale, "mystery": mystery,
                "core_bytes_hex": core_bytes.hex(),
            })
        if n % 100 == 0:
            print(f"  ...{n}/{total} maps scanned, {len(seen_files)} unique files", flush=True)

    print(f"Scanned {len(seen_files)} distinct map files, {len(rows)} candidate-model prop instances collected.")
    return rows


def _load_cached_portal_props() -> list[dict] | None:
    """Returns None if the cache header doesn't match the current schema
    (e.g. after adding columns), forcing a fresh scan."""
    rows = []
    with open(_OUT_ALL, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != _ALL_FIELDNAMES:
            print(f"Cache {_OUT_ALL} has a stale column layout -- rescanning.")
            return None
        for row in reader:
            rows.append({
                "map_id": int(row["map_id"]),
                "map_name": row["map_name"],
                "sub_map_id": int(row["sub_map_id"]),
                "sub_map_name": row["sub_map_name"],
                "prop_id": int(row["prop_id"]),
                "model_fid": int(row["model_fid"]),
                "x": float(row["x"]), "y": float(row["y"]), "z": float(row["z"]),
                "rot_cos": float(row["rot_cos"]), "rot_sin": float(row["rot_sin"]),
                "scale": float(row["scale"]), "mystery": float(row["mystery"]),
                "core_bytes_hex": row["core_bytes_hex"],
            })
    print(f"Loaded {len(rows)} cached rows from {_OUT_ALL} (pass --rescan to force a fresh Gw.dat scan).")
    return rows


def collect_all_portal_props(force_rescan: bool = False) -> list[dict]:
    """Stage 1: every candidate-model prop instance across the whole map
    corpus, with its full raw transform decoded to individual numbers.
    Cached in output/all_portal_props.csv -- loads from there unless
    force_rescan, the cache doesn't exist yet, or its column layout is
    stale."""
    if not force_rescan and os.path.exists(_OUT_ALL):
        cached = _load_cached_portal_props()
        if cached is not None:
            return cached
    return _scan_all_portal_props()


def find_template_default_keys(rows: list[dict]) -> set[tuple[float, float]]:
    """(scale, mystery) combos that co-occur with TEMPLATE_ROTATION_THRESHOLD+
    distinct rotations across the whole corpus -- i.e. reused placement
    defaults, not values picked for one specific instance."""
    scale_mystery_to_rotations: dict[tuple, set] = defaultdict(set)
    for row in rows:
        key = (row["scale"], row["mystery"])
        scale_mystery_to_rotations[key].add((row["rot_cos"], row["rot_sin"]))
    return {k for k, rots in scale_mystery_to_rotations.items() if len(rots) >= TEMPLATE_ROTATION_THRESHOLD}


def find_pairs(rows: list[dict]) -> tuple[list[list[dict]], list[list[dict]]]:
    """Stage 2: group by (model_fid, core_bytes_hex) -- every number must
    match, not just the transform bytes alone. Returns (clean_pairs,
    leftovers). A clean pair is an exactly-2-member group whose members are
    in different map files OR in the same file but attributed to different
    sub-maps (shared-navmesh files like Camp Rankor/Snake Dance host gates
    between their own sub-regions -- both props of such a gate live in ONE
    file, so a strict different-file rule silently dropped them)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        key = (row["model_fid"], row["core_bytes_hex"])
        groups[key].append(row)

    clean_pairs = []
    leftovers = []
    for members in groups.values():
        if len(members) == 2:
            a, b = members
            cross_file = a["map_id"] != b["map_id"]
            cross_sub = a["sub_map_id"] != b["sub_map_id"]
            if cross_file or cross_sub:
                clean_pairs.append(members)
                continue
        leftovers.append(members)
    return clean_pairs, leftovers


# map_id -> list of (cx, cy) centers of spawn-reachable trapezoids, or None
# if the map has no usable pathing/spawn data. Populated lazily -- pathing
# parse is the expensive part (~0.5-2s per map), so only maps that actually
# appear in an ambiguous group ever get loaded.
_reach_cache: dict[int, list[tuple[float, float]] | None] = {}


def _get_reachable_centers(archive: GwDatArchive, map_id: int):
    """CAUTION: trapezoid adjacent1-4 indices are PLANE-LOCAL, not global
    (verified on Lion's Arch: every plane's adjacency values max out at its
    own trap count). BFS must therefore stay within the seed trapezoid's
    plane; treating the indices as global fabricates cross-plane
    connectivity and (real bug hit 2026-07-12) made a decorative gate copy
    18k units away look reachable. Cross-plane portal-record links are NOT
    followed, so the reachable set is an underestimate -- fine here, since
    functional props measure <=~400u and decorative copies 6k-18k."""
    if map_id in _reach_cache:
        return _reach_cache[map_id]
    result = None
    try:
        data = archive.read_by_hash(MAP_ID_TO_DAT_FILE_ID[map_id])
        planes = parse_ffna_pathing(data) or []
        sp = parse_ffna_spawns(data)
        spawns = (sp[0] + sp[1]) if sp else []
        # (plane_idx, local_idx) -> center; adjacency stays plane-local
        centers: dict[tuple[int, int], tuple[float, float]] = {}
        for pi, pl in enumerate(planes):
            for li, t in enumerate(pl.trapezoids):
                centers[(pi, li)] = ((t.xtl + t.xtr + t.xbl + t.xbr) / 4, (t.yt + t.yb) / 2)
        if centers and spawns:
            reach: set[tuple[int, int]] = set()
            for spn in spawns:
                best_key, best_d = None, None
                for key, (cx, cy) in centers.items():
                    d = (cx - spn.x) ** 2 + (cy - spn.y) ** 2
                    if best_d is None or d < best_d:
                        best_d, best_key = d, key
                if best_key is None or math.sqrt(best_d) > 2000 or best_key in reach:
                    continue
                pi = best_key[0]
                plane_traps = planes[pi].trapezoids
                n = len(plane_traps)
                q = deque([best_key[1]])
                reach.add(best_key)
                while q:
                    li = q.popleft()
                    t = plane_traps[li]
                    for a in (t.adjacent1, t.adjacent2, t.adjacent3, t.adjacent4):
                        if 0 <= a < n and (pi, a) not in reach:
                            reach.add((pi, a))
                            q.append(a)
            if reach:
                result = [centers[k] for k in reach]
    except Exception:
        result = None
    _reach_cache[map_id] = result
    return result


def prop_reach_distance(archive: GwDatArchive, map_id: int, x: float, y: float):
    """Distance from (x, y) to the nearest spawn-reachable trapezoid center
    of map_id's own pathing mesh, or None if unknown (no pathing/spawns)."""
    centers = _get_reachable_centers(archive, map_id)
    if not centers:
        return None
    return min(math.hypot(cx - x, cy - y) for cx, cy in centers)


def disambiguate_multi_groups(leftovers: list[list[dict]], archive: GwDatArchive):
    """Stage 3: for every cross-map 3+-member group, keep only members that
    sit near their own map's spawn-reachable navmesh (functional gates) and
    drop the far-away decorative carve-out copies. Groups reduced to exactly
    2 cross-map members become resolved pairs. Returns (resolved_pairs,
    reach_annotations, resolved_group_ids) where reach_annotations maps
    id(member-dict) -> distance (for the leftovers CSV) and
    resolved_group_ids holds id(group-list) for groups stage 4 should skip."""
    resolved = []
    annotations: dict[int, float | None] = {}
    resolved_group_ids: set[int] = set()
    for members in leftovers:
        if len(members) < 3:
            continue
        map_ids = {m["map_id"] for m in members}
        if len(map_ids) < 2:
            continue
        functional = []
        for m in members:
            d = prop_reach_distance(archive, m["map_id"], m["x"], m["y"])
            annotations[id(m)] = d
            if d is not None and d <= REACHABLE_DIST_THRESHOLD:
                functional.append(m)
        if len(functional) == 2:
            a, b = functional
            if a["map_id"] != b["map_id"] or a["sub_map_id"] != b["sub_map_id"]:
                resolved.append(functional)
                resolved_group_ids.add(id(members))
    return resolved, annotations, resolved_group_ids


def _narrow_geom_match(bpg, archive: GwDatArchive, a: dict, b: dict, radius: float):
    """Grid-snapped translation test between the navmesh edges within
    `radius` of two props (in each prop's own map). A real gate pair
    occupies the same world-grid point, so the local geometry matches
    exactly; unrelated gates sharing a default transform don't. Returns
    (confidence, matched, total)."""
    edges_a = bpg.nearby_edges(archive, a["map_id"], (a["x"], a["y"]), radius=radius)
    edges_b = bpg.nearby_edges(archive, b["map_id"], (b["x"], b["y"]), radius=radius)
    if len(edges_a) < GEOM_MIN_EDGES or len(edges_b) < GEOM_MIN_EDGES:
        return 0.0, 0, min(len(edges_a), len(edges_b))
    hist_a = bpg.length_histogram(archive, a["map_id"])
    hist_b = bpg.length_histogram(archive, b["map_id"])
    candidates = []
    for ea in edges_a:
        la = bpg.edge_len(ea)
        for eb in edges_b:
            lb = bpg.edge_len(eb)
            if abs(la - lb) > bpg.LENGTH_MATCH_TOL:
                continue
            candidates.append((hist_a.get(round(la), 1) + hist_b.get(round(lb), 1), ea, eb))
    candidates.sort(key=lambda c: c[0])
    best = (0.0, 0, len(edges_a))
    for _, ea, eb in candidates:
        t = bpg.try_translation(ea, eb)
        if t is None:
            continue
        dx, dy = bpg.snap_to_grid(*t)
        matched, total = bpg.score_translation(edges_a, edges_b, dx, dy)
        conf = matched / total if total else 0.0
        if conf > best[0]:
            best = (conf, matched, total)
        if conf >= 0.95:
            break
    return best


def geometric_pair_multi_groups(leftovers, annotations, resolved_group_ids, archive: GwDatArchive):
    """Stage 4: for multi-member groups stage 3 could NOT reduce to one pair
    (typically template-default transforms shared by many unrelated real
    gates -- e.g. the ~29-member default-rotation group holding the Black
    Curtain<->Kessex Peak gate), reachability-filter the members and then
    pair the surviving functional gates by narrow-radius geometric match,
    greedily accepting the best-scoring combos. Each prop is used at most
    once."""
    import lib_build_portal_graph as bpg
    results = []
    for members in leftovers:
        if len(members) < 3 or id(members) in resolved_group_ids:
            continue
        functional = []
        for m in members:
            d = annotations.get(id(m))
            if id(m) not in annotations:
                d = prop_reach_distance(archive, m["map_id"], m["x"], m["y"])
                annotations[id(m)] = d
            if d is not None and d <= REACHABLE_DIST_THRESHOLD:
                functional.append(m)
        if len(functional) < 2:
            continue
        combos = []
        for i in range(len(functional)):
            for j in range(i + 1, len(functional)):
                a, b = functional[i], functional[j]
                if a["map_id"] == b["map_id"] and a["sub_map_id"] == b["sub_map_id"]:
                    continue
                for radius in GEOM_RADII:
                    conf, matched, total = _narrow_geom_match(bpg, archive, a, b, radius=radius)
                    tight = conf >= GEOM_MIN_CONF and total >= GEOM_MIN_EDGES
                    dilution_tolerant = conf >= GEOM_ALT_MIN_CONF and matched >= GEOM_ALT_MIN_MATCHED
                    if tight or dilution_tolerant:
                        combos.append((conf, total, a, b))
                        break
        combos.sort(key=lambda c: (-c[0], -c[1]))
        used: set[int] = set()
        for conf, total, a, b in combos:
            if id(a) in used or id(b) in used:
                continue
            used.add(id(a))
            used.add(id(b))
            results.append([a, b])
    return results


_wiki_neighbors_cache = None


def _get_wiki_neighbors():
    """map_id -> set of wiki-documented neighbor map_ids (either direction
    checked by callers), from the v2 pipeline's wiki_exits_cache.json.
    Empty dict if the cache file isn't present."""
    global _wiki_neighbors_cache
    if _wiki_neighbors_cache is not None:
        return _wiki_neighbors_cache
    import json
    from lib_map_names import resolve_map_id_by_name
    neighbors = defaultdict(set)
    path = os.path.join(_PIPELINE_DIR, "output", "wiki_exits_cache.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            wiki_exits = json.load(f)
        for map_id_str, entry in wiki_exits.items():
            map_id = int(map_id_str)
            for exit_ in entry.get("exits", []):
                nid = resolve_map_id_by_name(exit_["target_title"], prefer_ids=set(MAP_ID_TO_DAT_FILE_ID))
                if nid is not None:
                    neighbors[map_id].add(nid)
    _wiki_neighbors_cache = neighbors
    return neighbors


def _is_wiki_adjacent(a: dict, b: dict) -> bool:
    neighbors = _get_wiki_neighbors()
    ida, idb = a["sub_map_id"], b["sub_map_id"]
    return idb in neighbors.get(ida, set()) or ida in neighbors.get(idb, set())


def _passes_geom(bpg, archive: GwDatArchive, a: dict, b: dict) -> bool:
    for radius in GEOM_RADII:
        conf, matched, total = _narrow_geom_match(bpg, archive, a, b, radius=radius)
        if (conf >= GEOM_MIN_CONF and total >= GEOM_MIN_EDGES) or (
            conf >= GEOM_ALT_MIN_CONF and matched >= GEOM_ALT_MIN_MATCHED
        ):
            return True
    return False


def _is_cross_era(a: dict, b: dict) -> bool:
    pre_a = "pre-searing" in a["sub_map_name"].lower()
    pre_b = "pre-searing" in b["sub_map_name"].lower()
    return pre_a != pre_b


def verify_pairs(pairs: list[list[dict]], archive: GwDatArchive, label: str):
    """Verification gate for coincidence-prone pair tiers (template-default
    transforms shared corpus-wide, and reachability-only disambiguations):
    a real pair must (1) not span the pre-/post-Searing era boundary,
    (2) have BOTH gates functional (near their own map's spawn-reachable
    navmesh), and (3) pass the narrow-radius geometric navmesh match -- an
    unrelated cross-campaign collision like Ice Tooth Cave<->Boreas Seabed
    outpost (real bug: same default transform, coincidentally same
    rotation) fails (3) because the local geometry has nothing to do with
    each other, while every live-confirmed real pair passes. Returns
    (verified, rejected_with_reason)."""
    import lib_build_portal_graph as bpg
    verified, rejected = [], []
    for a, b in pairs:
        if _is_cross_era(a, b):
            rejected.append((a, b, "cross_era"))
            continue
        da = prop_reach_distance(archive, a["map_id"], a["x"], a["y"])
        db = prop_reach_distance(archive, b["map_id"], b["x"], b["y"])
        if da is None or db is None or da > REACHABLE_DIST_THRESHOLD or db > REACHABLE_DIST_THRESHOLD:
            rejected.append((a, b, f"unreachable(a={da if da is None else round(da)},b={db if db is None else round(db)})"))
            continue
        if not _passes_geom(bpg, archive, a, b):
            # Geometry can false-negative a REAL pair when one map's file
            # simply doesn't carry a navmesh copy around the gate (confirmed
            # on the live-verified Droknar's Forge 284 <-> Talus Chute 220).
            # Wiki adjacency is an independent rescue signal: a functional-
            # both-ends, transform-matched, wiki-documented pair is accepted
            # even without local mesh overlap.
            if _is_wiki_adjacent(a, b):
                verified.append([a, b])
                continue
            rejected.append((a, b, "geometry_mismatch"))
            continue
        verified.append([a, b])
    print(f"  {label}: {len(verified)} verified, {len(rejected)} rejected")
    return verified, rejected


def _write_pairs_csv(path: str, pairs: list[list[dict]]) -> None:
    """map_id/map_name columns carry the ATTRIBUTED sub-map (differs from
    the raw file-level label only for shared-navmesh files); pair_kind
    records whether the two props live in different files or in one shared
    file's two sub-regions."""
    fieldnames = [
        "map_id_a", "map_name_a", "prop_id_a", "x_a", "y_a", "z_a",
        "map_id_b", "map_name_b", "prop_id_b", "x_b", "y_b", "z_b",
        "model_fid", "pair_kind",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for a, b in pairs:
            w.writerow({
                "map_id_a": a["sub_map_id"], "map_name_a": a["sub_map_name"], "prop_id_a": a["prop_id"],
                "x_a": a["x"], "y_a": a["y"], "z_a": a["z"],
                "map_id_b": b["sub_map_id"], "map_name_b": b["sub_map_name"], "prop_id_b": b["prop_id"],
                "x_b": b["x"], "y_b": b["y"], "z_b": b["z"],
                "model_fid": a["model_fid"],
                "pair_kind": "intra_file" if a["map_id"] == b["map_id"] else "cross_file",
            })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rescan", action="store_true", help="Force a fresh Gw.dat scan instead of using the cached CSV")
    args = parser.parse_args()

    rows = collect_all_portal_props(force_rescan=args.rescan)

    os.makedirs("output", exist_ok=True)
    with open(_OUT_ALL, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_ALL_FIELDNAMES)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows -> {_OUT_ALL}")

    clean_pairs, leftovers = find_pairs(rows)

    template_keys = find_template_default_keys(rows)
    high_confidence_pairs = [
        (a, b) for a, b in clean_pairs if (a["scale"], a["mystery"]) not in template_keys
    ]
    template_default_pairs = [
        (a, b) for a, b in clean_pairs if (a["scale"], a["mystery"]) in template_keys
    ]

    archive = GwDatArchive(_DAT_PATH)
    all_rejected: list[tuple[dict, dict, str, str]] = []

    # High-confidence pairs carry unique transforms (coincidence implausible)
    # -- only the cross-era filter applies (a pre/post-Searing pair IS the
    # same world gate in both eras, so even geometry can't reject it).
    hc_kept = []
    for a, b in high_confidence_pairs:
        if _is_cross_era(a, b):
            all_rejected.append((a, b, "cross_era", "high_confidence"))
        else:
            hc_kept.append([a, b])
    _write_pairs_csv(_OUT_PAIRS, hc_kept)
    print(f"Wrote {len(hc_kept)} high-confidence 1:1 pairs (unique scale+mystery) -> {_OUT_PAIRS}")

    print("Stage 2b: verifying template-default pairs (reachability + narrow-radius geometry)...")
    template_verified, template_rejected = verify_pairs(template_default_pairs, archive, "template-default")
    all_rejected.extend((a, b, reason, "template_default") for a, b, reason in template_rejected)
    _write_pairs_csv(_OUT_PAIRS_TEMPLATE, template_verified)
    print(f"Wrote {len(template_verified)} verified template-default pairs -> {_OUT_PAIRS_TEMPLATE}")

    print("Stage 3: reachability disambiguation of 3+-member groups (loads pathing per involved map, may take a minute)...")
    disambiguated_pairs, reach_annotations, resolved_group_ids = disambiguate_multi_groups(leftovers, archive)
    disambiguated_verified, disambiguated_rejected = verify_pairs(disambiguated_pairs, archive, "disambiguated")
    all_rejected.extend((a, b, reason, "disambiguated") for a, b, reason in disambiguated_rejected)
    _write_pairs_csv(_OUT_PAIRS_DISAMBIGUATED, disambiguated_verified)
    print(f"Wrote {len(disambiguated_verified)} reachability-disambiguated pairs (from 3+-member groups) -> {_OUT_PAIRS_DISAMBIGUATED}")

    print("Stage 4: narrow-radius geometric pairing inside remaining multi-groups...")
    geometric_pairs = geometric_pair_multi_groups(leftovers, reach_annotations, resolved_group_ids, archive)
    geo_kept = []
    for a, b in geometric_pairs:
        if _is_cross_era(a, b):
            all_rejected.append((a, b, "cross_era", "geometric"))
        else:
            geo_kept.append([a, b])
    _write_pairs_csv(_OUT_PAIRS_GEOMETRIC, geo_kept)
    print(f"Wrote {len(geo_kept)} geometrically-paired candidates (from unresolved multi-groups) -> {_OUT_PAIRS_GEOMETRIC}")

    with open(_OUT_PAIRS_REJECTED, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["tier", "reason", "map_name_a", "prop_id_a", "map_name_b", "prop_id_b", "model_fid"])
        w.writeheader()
        for a, b, reason, tier in all_rejected:
            w.writerow({
                "tier": tier, "reason": reason,
                "map_name_a": a["sub_map_name"], "prop_id_a": a["prop_id"],
                "map_name_b": b["sub_map_name"], "prop_id_b": b["prop_id"],
                "model_fid": a["model_fid"],
            })
    print(f"Wrote {len(all_rejected)} rejected pairs (with reasons) -> {_OUT_PAIRS_REJECTED}")

    with open(_OUT_LEFTOVERS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["group_size", "map_id", "map_name", "sub_map_id", "sub_map_name", "prop_id", "model_fid", "x", "y", "z", "template_default", "reach_dist"])
        w.writeheader()
        for members in leftovers:
            for m in members:
                d = reach_annotations.get(id(m))
                w.writerow({
                    "group_size": len(members),
                    "map_id": m["map_id"], "map_name": m["map_name"],
                    "sub_map_id": m["sub_map_id"], "sub_map_name": m["sub_map_name"],
                    "prop_id": m["prop_id"],
                    "model_fid": m["model_fid"], "x": m["x"], "y": m["y"], "z": m["z"],
                    "template_default": (m["scale"], m["mystery"]) in template_keys,
                    "reach_dist": f"{d:.0f}" if d is not None else "",
                })
    leftover_prop_count = sum(len(m) for m in leftovers)
    print(f"Wrote {leftover_prop_count} leftover props ({len(leftovers)} groups) -> {_OUT_LEFTOVERS}")

    print()
    print("=== Summary ===")
    print(f"Total candidate props collected: {len(rows)}")
    print(f"Distinct (scale, mystery) combos flagged as reused template defaults: {len(template_keys)}")
    print(f"Clean 1:1 cross-map pairs: {len(clean_pairs)} ({len(clean_pairs) * 2} props)")
    print(f"  high-confidence kept (unique scale+mystery, minus cross-era): {len(hc_kept)}")
    print(f"  template-default verified (of {len(template_default_pairs)} raw): {len(template_verified)}")
    print(f"Reachability-disambiguated pairs verified (of {len(disambiguated_pairs)} raw): {len(disambiguated_verified)}")
    print(f"Geometrically-paired candidates kept (of {len(geometric_pairs)} raw): {len(geo_kept)}")
    print(f"Rejected pairs across all tiers: {len(all_rejected)}")
    print(f"Leftover groups (not a clean 1:1 cross-map pair): {len(leftovers)} ({leftover_prop_count} props)")
    singleton_count = sum(1 for m in leftovers if len(m) == 1)
    print(f"  of which true singletons (matched nothing at all): {singleton_count}")
    multi_count = sum(1 for m in leftovers if len(m) > 2)
    print(f"  of which 3+-member ambiguous groups: {multi_count} ({len(disambiguated_pairs)} now resolved via reachability)")
    samemap_count = sum(1 for m in leftovers if len(m) == 2 and len({x['map_id'] for x in m}) == 1)
    print(f"  of which same-map 2-member (not cross-map, likely duplicate/symmetric placements): {samemap_count}")


if __name__ == "__main__":
    main()
