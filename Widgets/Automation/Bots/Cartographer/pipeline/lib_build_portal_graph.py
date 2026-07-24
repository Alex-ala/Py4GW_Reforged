"""
Build a map-to-map portal graph purely from spawn points and pathfinding-mesh
geometry -- deliberately ignoring decorative portal-marker props entirely,
since those don't exist for "gateless" plain-boundary crossings (see project
memory: gw_zones_stuff_investigation, gw_portal_matching).

Method (per user spec):
  For each connection A<->B where we have a correlating spawn point on each
  side (a spawn on A tagged for B, and a spawn on B tagged for A):

    - If A and B share the same underlying map FILE (file_id collision --
      i.e. they're actually the same navmesh, e.g. an outpost carved out of
      its own explorable area), the two spawns are already in one coordinate
      grid: place the portal identifier at their midpoint directly.

    - Otherwise, the two maps use independent coordinate grids. Find a rare
      (low-frequency) trapezoid boundary edge near each side's spawn point,
      match them by length, and derive the rigid transform (rotation +
      translation) that maps one grid onto the other. Validated on
      Henge of Denravi <-> Tangle Root (2026-07-09): matched a ~590-unit
      edge present on both sides at ~0.1% frequency, giving an EXACT
      (-6144,-24576) = -1024*(6,24) integer-grid translation (no rotation),
      and predicting the independently live-traced crossing point on the far
      side to within 45 units.

    Confidence is scored by re-projecting ALL nearby boundary edges through
    the derived transform and counting how many find a length-and-position
    match on the other side -- a single matched edge could be coincidence,
    many independently agreeing edges are not.

Run: python lib_build_portal_graph.py (superseded standalone CLI -- kept as library, see 08_build_connection_graph.py)
Outputs:
  output/portal_graph.csv            -- resolved connections (both sides known)
  output/portal_graph_unresolved.csv -- connections with only one side (or
                                         neither) resolved via spawn tag;
                                         kept separate rather than guessed.
"""
from __future__ import annotations

import csv
import math
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib_gwdat_unpack import GwDatArchive
from lib_ffna_pure import parse_ffna_pathing
from lib_map_file_ids import MAP_ID_TO_DAT_FILE_ID
from lib_map_names import MAP_VARIANTS_TO_BASE
from lib_map_names import MAP_ID_TO_NAME

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# Relocated out of gwdat_tools (2026-07-19): no longer sits next to a "bla/"
# sibling folder, and the 4GB Gw.dat archive itself must never be copied
# into a git repo -- point at the real install instead, overridable via env
# var for other machines/prefixes.
_DAT_PATH = os.environ.get(
    "GWDAT_PIPELINE_DAT_PATH",
    "/home/alex/git/gw_wine/drive_c/Program Files (x86)/GUILD WARS/Gw.dat",
)
_TAG_MATCHES_CSV = os.path.join(_THIS_DIR, "output", "tag_dictionary_matches.csv")
_NICKNAME_SPAWNS_CSV = os.path.join(_THIS_DIR, "output", "nickname_decoded_spawns.csv")
_OUT_RESOLVED = os.path.join(_THIS_DIR, "output", "portal_graph.csv")
_OUT_UNRESOLVED = os.path.join(_THIS_DIR, "output", "portal_graph_unresolved.csv")

EDGE_SEARCH_RADIUS = 6000.0       # how far from the spawn point to look for boundary edges
LENGTH_MATCH_TOL = 2.0            # units, for matching edge lengths between maps
TRANSLATION_CONSISTENCY_TOL = 5.0 # units, endpoint-pair translation agreement
CONFIRM_POS_TOL = 40.0            # units, for confidence re-projection position match
GRID_UNIT = 1024.0
SAME_NAVMESH_AGREEMENT_TOL = 2500.0  # spawn_a/spawn_b must agree within this
                                       # distance to be trusted as "the same
                                       # physical portal, seen from two
                                       # sides" before averaging (matches the
                                       # spawn-to-marker match radius used
                                       # elsewhere in this pipeline family).
                                       # BUG FOUND 2026-07-19: same_navmesh
                                       # pairs were averaged unconditionally
                                       # with NO agreement check at all --
                                       # when each side's own tag-matching
                                       # pass lands on a DIFFERENT real
                                       # portal within a shared multi-map
                                       # file (e.g. Cursed Lands' numeric tag
                                       # '0059' near (20268,4589) vs Nebo
                                       # Terrace's nickname tag 'curs' near
                                       # (-3326,-11534) -- ~24700u apart,
                                       # confirmed via live map dumps), the
                                       # midpoint is a phantom coordinate
                                       # matching neither real portal. Since
                                       # same_navmesh pairs get confidence=1.0
                                       # by construction ("file_id equality is
                                       # definitional"), nothing else in the
                                       # pipeline catches this -- unlike
                                       # cross_navmesh_transform pairs, which
                                       # get independently geometrically
                                       # confirmed. See _resolve_same_navmesh_position().
MIN_CONFIDENCE = 0.15             # below this, treat as unresolved rather than guess.
                                   # Lowered from 0.5 (2026-07-09) after discovering a
                                   # manually-verified, 0-residual-error TRUE transform
                                   # (Snake Dance <-> Grenth's Footprint) only scores 0.476 --
                                   # at EDGE_SEARCH_RADIUS=6000 the ratio has a natural
                                   # ceiling well under 1.0 even for a provably-correct
                                   # transform, since much of the search box's geometry has
                                   # no counterpart on the other side at all. Known false
                                   # positives (e.g. Garden of Seborhin<->Tihark Orchard)
                                   # still score <=0.09 with tiny absolute match counts, so
                                   # there's a clear gap to sit this threshold in.

# Connections known only via live GPS tracing (portal_trigger_trace.csv),
# not resolvable through any static spawn tag -- fed in as ground truth
# "correlating points" per side, same role a tag-matched spawn would play.
LIVE_TRACED_CONNECTIONS = {
    (49, 48): {  # Henge of Denravi <-> Tangle Root
        49: (6292.37, -10683.5),
        48: (12469.93, 13922.43),
    },
    (56, 59): {  # Cursed Lands <-> Nebo Terrace (2026-07-19): part of the
        # Cursed Lands/Nebo Terrace/Bergen Hot Springs same_navmesh 3-way
        # cluster (all 3 share one Gw.dat file). The pipeline's own
        # same_navmesh resolver honestly can't disambiguate this pair from
        # static data alone -- Cursed Lands' side numeric-tag-matches to a
        # DIFFERENT real portal marker (20348,3531) than Nebo Terrace's side
        # nickname-tag-matches to (-4181,-11659); both are genuine portal
        # props on the shared file, just for different pairwise connections
        # in the cluster, so proximity/marker-snapping alone can't tell
        # which belongs to THIS pair (see SAME_NAVMESH_AGREEMENT_TOL).
        # User stood at the real in-game portal and confirmed its position
        # directly: ~(-4594,-11391), 492u from the (-4181,-11659) marker --
        # well within normal marker/live-position measurement slop.
        56: (-4181.0, -11659.0),
        59: (-4181.0, -11659.0),
    },
}

_trap_cache: dict[int, list] = {}
_edge_len_hist_cache: dict[int, Counter] = {}
_trap_shape_hist_cache: dict[int, Counter] = {}

# Fallback file-id lookup for map_ids not in MAP_ID_TO_DAT_FILE_ID (e.g. the
# 260 maps missing from that table, some recovered via orphan-file matching
# -- see match_orphan_maps.py / merge_expanded_spawns.py). Populate this
# from a caller before resolving pairs that involve such a map_id.
EXTRA_FILE_IDS: dict[int, int] = {}


def get_file_id(map_id):
    """Resolves a map_id to its archive file hash, checking the normal
    table first, EXTRA_FILE_IDS as a fallback, then -- for a seasonal/event
    variant id that was never itself scanned (e.g. 837 Talmark Wilderness -
    War in Kryta) -- its base map's file_id via MAP_VARIANTS_TO_BASE (see
    lib_map_names.py: these reskins verifiably reuse the base map's file
    verbatim). Returns None (never treated as equal to another None -- see
    same_navmesh check below) if genuinely unknown."""
    fid = MAP_ID_TO_DAT_FILE_ID.get(map_id, EXTRA_FILE_IDS.get(map_id))
    if fid is not None:
        return fid
    base_id = MAP_VARIANTS_TO_BASE.get(map_id)
    if base_id is not None:
        return MAP_ID_TO_DAT_FILE_ID.get(base_id, EXTRA_FILE_IDS.get(base_id))
    return None


def _archive():
    return GwDatArchive(_DAT_PATH)


def load_trapezoids(archive, map_id):
    if map_id in _trap_cache:
        return _trap_cache[map_id]
    fid = get_file_id(map_id)
    data = archive.read_by_hash(fid)
    planes = parse_ffna_pathing(data) or []
    traps = []
    for plane in planes:
        traps.extend(plane.trapezoids)
    _trap_cache[map_id] = traps
    return traps


def all_edges(traps):
    """Top/bottom (horizontal) boundary-candidate edges for every trapezoid."""
    edges = []
    for t in traps:
        edges.append(((t.xtl, t.yt), (t.xtr, t.yt)))
        edges.append(((t.xbl, t.yb), (t.xbr, t.yb)))
    return edges


def edge_len(e):
    (x1, y1), (x2, y2) = e
    return math.hypot(x2 - x1, y2 - y1)


def length_histogram(archive, map_id):
    if map_id in _edge_len_hist_cache:
        return _edge_len_hist_cache[map_id]
    traps = load_trapezoids(archive, map_id)
    hist = Counter(round(edge_len(e)) for e in all_edges(traps))
    _edge_len_hist_cache[map_id] = hist
    return hist


MAX_NEARBY_EDGES = 1500  # hard cap on find_transform's O(edges_a*edges_b) candidate
                          # search -- a dense coastline/outpost mesh within
                          # EDGE_SEARCH_RADIUS can otherwise return many thousands
                          # of edges, and that product is built in full BEFORE any
                          # sorting/early-exit can help. Discovered 2026-07-10: the
                          # geometric brute-force pass stalled dead on its last few
                          # pairs for 2+ hours with workers still burning CPU --
                          # one single find_transform call, not many slow calls.
                          # Truncating (deterministic, closest-first would be nicer
                          # but not worth the cost here) bounds the worst case to
                          # 1500*1500=2.25M comparisons regardless of local density.


def nearby_edges(archive, map_id, center, radius=EDGE_SEARCH_RADIUS):
    traps = load_trapezoids(archive, map_id)
    out = []
    for e in all_edges(traps):
        (x1, y1), (x2, y2) = e
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        if math.hypot(mx - center[0], my - center[1]) <= radius:
            out.append(e)
    if len(out) > MAX_NEARBY_EDGES:
        out = out[:MAX_NEARBY_EDGES]
    return out


def try_translation(e_a, e_b):
    """Given two edges assumed corresponding (same length), test both endpoint
    orderings for a single consistent translation (rotation-free) mapping
    b -> a. Returns (dx, dy) or None."""
    a1, a2 = e_a
    for b1, b2 in ((e_b[0], e_b[1]), (e_b[1], e_b[0])):
        t1 = (a1[0] - b1[0], a1[1] - b1[1])
        t2 = (a2[0] - b2[0], a2[1] - b2[1])
        if math.hypot(t1[0] - t2[0], t1[1] - t2[1]) <= TRANSLATION_CONSISTENCY_TOL:
            return ((t1[0] + t2[0]) / 2, (t1[1] + t2[1]) / 2)
    return None


def snap_to_grid(dx, dy, unit=GRID_UNIT, tol=3.0):
    sx, sy = round(dx / unit) * unit, round(dy / unit) * unit
    if abs(sx - dx) <= tol and abs(sy - dy) <= tol:
        return sx, sy
    return dx, dy


def score_translation(edges_a, edges_b, dx, dy):
    """Count how many of A's edges, shifted by (dx,dy), land near a
    same-length edge in B (position+length match) -- confidence signal."""
    matched = 0
    b_by_len = defaultdict(list)
    for e in edges_b:
        b_by_len[round(edge_len(e))].append(e)
    for e in edges_a:
        (x1, y1), (x2, y2) = e
        sx1, sy1 = x1 - dx, y1 - dy
        sx2, sy2 = x2 - dx, y2 - dy
        L = round(edge_len(e))
        for cand in b_by_len.get(L, []):
            (cx1, cy1), (cx2, cy2) = cand
            d_direct = math.hypot(sx1 - cx1, sy1 - cy1) + math.hypot(sx2 - cx2, sy2 - cy2)
            d_swapped = math.hypot(sx1 - cx2, sy1 - cy2) + math.hypot(sx2 - cx1, sy2 - cy1)
            if min(d_direct, d_swapped) <= CONFIRM_POS_TOL * 2:
                matched += 1
                break
    return matched, len(edges_a)


def trapezoid_shape(t):
    """Translation-invariant shape signature: (top_width, bottom_width,
    height), rounded. This format never rotates trapezoids (same
    "rotation-free" scope as the old edge-length method -- see
    try_translation), so this triple alone is enough to recognize the
    "same" trapezoid on two maps regardless of where it sits."""
    return (round(t.xtr - t.xtl), round(t.xbr - t.xbl), round(t.yt - t.yb))


def trapezoid_shape_histogram(archive, map_id):
    if map_id in _trap_shape_hist_cache:
        return _trap_shape_hist_cache[map_id]
    traps = load_trapezoids(archive, map_id)
    hist = Counter(trapezoid_shape(t) for t in traps)
    _trap_shape_hist_cache[map_id] = hist
    return hist


MAX_NEARBY_TRAPEZOIDS = 1500  # same worst-case-bound rationale as MAX_NEARBY_EDGES


def nearby_trapezoids(archive, map_id, center, radius=EDGE_SEARCH_RADIUS):
    traps = load_trapezoids(archive, map_id)
    out = [t for t in traps if math.hypot(t.cx - center[0], t.cy - center[1]) <= radius]
    if len(out) > MAX_NEARBY_TRAPEZOIDS:
        out = out[:MAX_NEARBY_TRAPEZOIDS]
    return out


def score_translation_traps(traps_a, traps_b, dx, dy):
    """Count how many of A's trapezoids, shifted by (dx,dy), land near a
    same-shape trapezoid in B (center position + shape match) -- confidence
    signal, same role as the old score_translation but per-trapezoid
    instead of per-edge (one comparison covers what used to take two,
    since a trapezoid's top+bottom edges are checked together as a unit)."""
    matched = 0
    b_by_shape = defaultdict(list)
    for t in traps_b:
        b_by_shape[trapezoid_shape(t)].append(t)
    for t in traps_a:
        sx, sy = t.cx - dx, t.cy - dy
        for cand in b_by_shape.get(trapezoid_shape(t), []):
            if math.hypot(sx - cand.cx, sy - cand.cy) <= CONFIRM_POS_TOL:
                matched += 1
                break
    return matched, len(traps_a)


def find_transform(archive, map_a, point_a, map_b, point_b, radius=EDGE_SEARCH_RADIUS):
    """Returns dict(dx, dy, confidence, matches, total) mapping map_b coords
    into map_a coords (map_a = map_b + (dx,dy)), or None if no candidate
    trapezoid-shape match was found at all.

    Matches whole trapezoids (top_width, bottom_width, height) instead of
    individual top/bottom edge lengths (2026-07-20 user observation/redesign):
    a trapezoid's full shape is a far more selective fingerprint than a
    single edge's length alone (lots of walls/boundaries share a common
    length; far fewer trapezoids share an identical full (w_top, w_bottom,
    height) triple), so there are usually several genuine 100%-shape
    matches to seed from, AND each matching pair gives an unambiguous
    translation directly from the two centers -- no "try both endpoint
    orderings" step needed the way two directionless edges require. Fewer,
    far-less-frequently-colliding candidates to generate and confirm makes
    this faster as well as more selective than the old edge-based search.

    radius: search radius around each point (default EDGE_SEARCH_RADIUS,
    tuned for spawn-point anchors, which can sit well back from the actual
    portal). A caller anchoring on a real portal-marker PROP position
    instead of a spawn point can safely pass a smaller radius -- paired
    portal props line up far more tightly across the two maps than an
    arbitrary spawn point does (2026-07-20 user observation)."""
    hist_a = trapezoid_shape_histogram(archive, map_a)
    hist_b = trapezoid_shape_histogram(archive, map_b)
    traps_a = nearby_trapezoids(archive, map_a, point_a, radius)
    traps_b = nearby_trapezoids(archive, map_b, point_b, radius)

    # candidate pairs: matching shape, prefer rarer shapes first
    by_shape_b = defaultdict(list)
    for tb in traps_b:
        by_shape_b[trapezoid_shape(tb)].append(tb)
    candidates = []
    for ta in traps_a:
        shape = trapezoid_shape(ta)
        matches_b = by_shape_b.get(shape)
        if not matches_b:
            continue
        rarity_a = hist_a.get(shape, 1)
        rarity_b = hist_b.get(shape, 1)
        for tb in matches_b:
            candidates.append((rarity_a + rarity_b, ta, tb))
    candidates.sort(key=lambda c: c[0])

    best = None
    for _, ta, tb in candidates:
        dx, dy = snap_to_grid(ta.cx - tb.cx, ta.cy - tb.cy)
        matched, total = score_translation_traps(traps_a, traps_b, dx, dy)
        confidence = matched / total if total else 0.0
        cand_result = {"dx": dx, "dy": dy, "confidence": confidence,
                       "matches": matched, "total": total}
        if best is None or confidence > best["confidence"]:
            best = cand_result
        if confidence >= 0.5:
            break  # good enough, stop searching
    return best


def load_tag_matches():
    """Returns dict[(map_id, target_map_id)] -> (averaged (spawn_x, spawn_y), sources set).

    Merges the numeric/name-prefix tag dictionary with the cross-map
    nickname-tag decodings (see decode_nickname_tags.py) -- both produce
    the same "a spawn on A is tagged for B" evidence, just via different
    decoding rules.
    """
    groups = defaultdict(list)
    sources = defaultdict(set)
    for path, label in ((_TAG_MATCHES_CSV, "tag_dictionary"), (_NICKNAME_SPAWNS_CSV, "nickname_decode")):
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (int(row["map_id"]), int(row["target_map_id"]))
                groups[key].append((float(row["spawn_x"]), float(row["spawn_y"])))
                sources[key].add(label)
    out = {}
    for key, pts in groups.items():
        avg = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        out[key] = (avg, sources[key])
    return out


_worker_archive = None


_ALL_PORTALS_CSV = os.path.join(_THIS_DIR, "output", "all_portals.csv")
_portal_markers_by_map: dict[int, list[tuple[float, float]]] | None = None


def _load_portal_markers() -> dict[int, list[tuple[float, float]]]:
    """map_id -> [(portal_x, portal_y), ...] ground-truth portal-prop
    positions (output/all_portals.csv, built by build_all_portals.py from
    the raw Gw.dat marker props -- NOT the spawn-tag evidence this module
    otherwise relies on). Cached after first load."""
    global _portal_markers_by_map
    if _portal_markers_by_map is not None:
        return _portal_markers_by_map
    markers: dict[int, list[tuple[float, float]]] = defaultdict(list)
    if os.path.exists(_ALL_PORTALS_CSV):
        with open(_ALL_PORTALS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    markers[int(row["map_id"])].append(
                        (float(row["portal_x"]), float(row["portal_y"])))
                except (KeyError, ValueError):
                    continue
    _portal_markers_by_map = dict(markers)
    return _portal_markers_by_map


def _nearest_portal_marker(map_id: int, pos: tuple[float, float],
                          radius: float = SAME_NAVMESH_AGREEMENT_TOL):
    """Closest real portal-marker prop to pos on map_id within radius, or
    None. Ground truth independent of the spawn-tag matching that produced
    pos in the first place -- used to disambiguate disagreeing spawns."""
    best, best_d = None, radius
    for mx, my in _load_portal_markers().get(map_id, []):
        d = math.hypot(mx - pos[0], my - pos[1])
        if d <= best_d:
            best, best_d = (mx, my), d
    return best


def _resolve_same_navmesh_position(a, b, spawn_a, spawn_b):
    """Returns (mx, my, note) or None (genuinely ambiguous -- caller should
    mark unresolved rather than guess). See SAME_NAVMESH_AGREEMENT_TOL's
    comment for the bug this guards against: spawn_a/spawn_b only get
    averaged when they actually agree; when they don't, ground-truth
    portal-marker positions (independent of the spawn-tag evidence that
    produced spawn_a/spawn_b) are used to pick the real one instead."""
    dist_ab = math.hypot(spawn_a[0] - spawn_b[0], spawn_a[1] - spawn_b[1])
    if dist_ab <= SAME_NAVMESH_AGREEMENT_TOL:
        return (spawn_a[0] + spawn_b[0]) / 2, (spawn_a[1] + spawn_b[1]) / 2, "midpoint"

    marker_a = _nearest_portal_marker(a, spawn_a)
    marker_b = _nearest_portal_marker(b, spawn_b)
    if marker_a is not None and marker_b is not None:
        if math.hypot(marker_a[0] - marker_b[0], marker_a[1] - marker_b[1]) <= SAME_NAVMESH_AGREEMENT_TOL:
            return ((marker_a[0] + marker_b[0]) / 2, (marker_a[1] + marker_b[1]) / 2,
                    "marker_confirmed_midpoint")
        return None  # both sides confirm a REAL marker, but different ones -- genuinely ambiguous
    if marker_a is not None:
        return marker_a[0], marker_a[1], "marker_confirmed_a"
    if marker_b is not None:
        return marker_b[0], marker_b[1], "marker_confirmed_b"
    return None  # disagree, and neither candidate is near any real portal prop


def _worker_init(extra_file_ids):
    # Passed explicitly rather than relying on the parent's already-mutated
    # EXTRA_FILE_IDS global being inherited -- true under fork, NOT true
    # under spawn (a fresh interpreter only gets what's explicitly passed),
    # and silently returning None file-ids for the orphan-recovered maps
    # broke 3 pairs (Ascalon Arena outpost, Spearhead Peak, Bokka
    # Amphitheatre) the first time this was parallelized.
    global _worker_archive, EXTRA_FILE_IDS
    EXTRA_FILE_IDS = extra_file_ids
    _worker_archive = _archive()


def _worker_resolve_cross_navmesh(args):
    a, b, spawn_a, spawn_b = args
    try:
        transform = find_transform(_worker_archive, a, spawn_a, b, spawn_b)
    except Exception as e:
        return (a, b, None, repr(e))
    return (a, b, transform, None)


def resolve_bidir_pairs(bidir_pairs, out_resolved_path, out_unresolved_extra_path=None, max_workers=None):
    """Given a list of (map_a, map_b, spawn_a_xy, spawn_b_xy, source_label)
    tuples -- both sides already independently identified as pointing at
    each other -- resolve each into a concrete portal position (same-navmesh
    midpoint, or cross-navmesh rigid transform) and write the resolved CSV.
    Returns (resolved_rows, unresolved_pairs) so callers can fold
    unresolved_pairs into their own unresolved-report format.

    Same-navmesh pairs are trivial and resolved inline. Cross-navmesh pairs
    each require an expensive independent find_transform() call (the
    O(edges_a * edges_b) candidate search), so those are farmed out to a
    process pool -- one archive/MFT load per worker (via _worker_init),
    reused across every pair that worker handles.
    """
    print(f"{len(bidir_pairs)} bidirectionally-resolved connections to process")

    resolved_rows = []
    unresolved_pairs = []
    cross_navmesh_pairs = []  # (a, b, spawn_a, spawn_b, source, name_a, name_b)

    for a, b, spawn_a, spawn_b, source in bidir_pairs:
        name_a = MAP_ID_TO_NAME.get(a, f"map_{a}")
        name_b = MAP_ID_TO_NAME.get(b, f"map_{b}")
        fid_a, fid_b = get_file_id(a), get_file_id(b)
        same_navmesh = fid_a is not None and fid_a == fid_b

        if same_navmesh:
            resolution = _resolve_same_navmesh_position(a, b, spawn_a, spawn_b)
            if resolution is None:
                dist_ab = math.hypot(spawn_a[0] - spawn_b[0], spawn_a[1] - spawn_b[1])
                unresolved_pairs.append((
                    a, name_a, b, name_b,
                    f"same_navmesh_spawn_disagreement(dist={dist_ab:.0f}u)",
                    spawn_a, spawn_b,
                ))
                print(f"[same-navmesh, AMBIGUOUS] {name_a} <-> {name_b}: spawn_a=({spawn_a[0]:.0f},"
                      f"{spawn_a[1]:.0f}) spawn_b=({spawn_b[0]:.0f},{spawn_b[1]:.0f}) "
                      f"disagree by {dist_ab:.0f}u -- moved to unresolved rather than averaging "
                      f"a phantom midpoint")
                continue
            mx, my, note = resolution
            resolved_rows.append({
                "map_id": a, "map_name": name_a, "neighbor_map_id": b, "neighbor_name": name_b,
                "portal_x": round(mx, 1), "portal_y": round(my, 1),
                "method": "same_navmesh", "confidence": 1.0, "source": source,
            })
            resolved_rows.append({
                "map_id": b, "map_name": name_b, "neighbor_map_id": a, "neighbor_name": name_a,
                "portal_x": round(mx, 1), "portal_y": round(my, 1),
                "method": "same_navmesh", "confidence": 1.0, "source": source,
            })
            print(f"[same-navmesh:{note}] {name_a} <-> {name_b}: portal=({mx:.0f},{my:.0f})")
            continue

        cross_navmesh_pairs.append((a, b, spawn_a, spawn_b, source, name_a, name_b))

    if max_workers is None:
        max_workers = max(1, (os.cpu_count() or 4) - 1)
    print(f"{len(cross_navmesh_pairs)} cross-navmesh pairs to resolve, using {max_workers} worker processes")

    import concurrent.futures
    transforms_by_pair = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=max_workers, initializer=_worker_init, initargs=(dict(EXTRA_FILE_IDS),)
    ) as pool:
        futures = {
            pool.submit(_worker_resolve_cross_navmesh, (a, b, spawn_a, spawn_b)): (a, b)
            for a, b, spawn_a, spawn_b, source, name_a, name_b in cross_navmesh_pairs
        }
        for fut in concurrent.futures.as_completed(futures):
            a, b, transform, err = fut.result()
            transforms_by_pair[(a, b)] = (transform, err)

    for a, b, spawn_a, spawn_b, source, name_a, name_b in cross_navmesh_pairs:
        transform, err = transforms_by_pair[(a, b)]
        if err is not None:
            print(f"  ERROR on {name_a}<->{name_b}: {err}")

        if transform is None:
            unresolved_pairs.append((a, name_a, b, name_b, "no_edge_length_match", spawn_a, spawn_b))
            print(f"[no-match] {name_a} <-> {name_b}: no candidate boundary edge match found")
            continue

        dx, dy = transform["dx"], transform["dy"]
        conf = transform["confidence"]

        if conf < MIN_CONFIDENCE:
            unresolved_pairs.append((
                a, name_a, b, name_b,
                f"low_confidence_transform(conf={conf:.2f},matches={transform['matches']}/{transform['total']})",
                spawn_a, spawn_b,
            ))
            print(f"[low-confidence, SKIPPED] {name_a} <-> {name_b}: dx,dy=({dx:.1f},{dy:.1f}) "
                  f"confidence={conf:.2f} ({transform['matches']}/{transform['total']}) -- moved to unresolved")
            continue
        # b_in_a = spawn_b + (dx,dy)  [since map_a_coord = map_b_coord + (dx,dy)]
        b_in_a = (spawn_b[0] + dx, spawn_b[1] + dy)
        portal_a = ((spawn_a[0] + b_in_a[0]) / 2, (spawn_a[1] + b_in_a[1]) / 2)
        a_in_b = (spawn_a[0] - dx, spawn_a[1] - dy)
        portal_b = ((spawn_b[0] + a_in_b[0]) / 2, (spawn_b[1] + a_in_b[1]) / 2)

        resolved_rows.append({
            "map_id": a, "map_name": name_a, "neighbor_map_id": b, "neighbor_name": name_b,
            "portal_x": round(portal_a[0], 1), "portal_y": round(portal_a[1], 1),
            "method": "cross_navmesh_transform", "confidence": round(conf, 3), "source": source,
        })
        resolved_rows.append({
            "map_id": b, "map_name": name_b, "neighbor_map_id": a, "neighbor_name": name_a,
            "portal_x": round(portal_b[0], 1), "portal_y": round(portal_b[1], 1),
            "method": "cross_navmesh_transform", "confidence": round(conf, 3), "source": source,
        })
        print(f"[cross-navmesh] {name_a} <-> {name_b}: dx,dy=({dx:.1f},{dy:.1f}) "
              f"confidence={conf:.2f} ({transform['matches']}/{transform['total']}) "
              f"portal_a=({portal_a[0]:.0f},{portal_a[1]:.0f}) portal_b=({portal_b[0]:.0f},{portal_b[1]:.0f})")

    os.makedirs(os.path.dirname(out_resolved_path), exist_ok=True)
    with open(out_resolved_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "map_id", "map_name", "neighbor_map_id", "neighbor_name",
            "portal_x", "portal_y", "method", "confidence", "source",
        ])
        w.writeheader()
        w.writerows(resolved_rows)

    print(f"\nresolved connections: {len(resolved_rows)//2} (both directions written) -> {out_resolved_path}")
    return resolved_rows, unresolved_pairs


def write_unresolved_csv(path, onesided, unresolved_pairs):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["map_id", "map_name", "other_map_id", "other_map_name", "reason",
                    "known_spawn_x", "known_spawn_y"])
        for row in onesided + [(a, na, b, nb, reason, sa, sb) for a, na, b, nb, reason, sa, sb in unresolved_pairs]:
            m, name_m, t, name_t, reason, spawn, _ = row
            sx, sy = (spawn if spawn else ("", ""))
            w.writerow([m, name_m, t, name_t, reason, sx, sy])
    print(f"unresolved rows: {len(onesided) + len(unresolved_pairs)} -> {path}")


def main():
    tag_matches_raw = load_tag_matches()
    tag_matches = {k: v[0] for k, v in tag_matches_raw.items()}

    # bidirectional pairs: both directions resolved via spawn tag (either
    # the numeric/prefix dictionary or the nickname decoder)
    pair_keys = set(tuple(sorted(k)) for k in tag_matches)
    bidir_pairs = []
    for a, b in pair_keys:
        if (a, b) in tag_matches and (b, a) in tag_matches:
            src = sorted(tag_matches_raw[(a, b)][1] | tag_matches_raw[(b, a)][1])
            bidir_pairs.append((a, b, tag_matches[(a, b)], tag_matches[(b, a)], "+".join(src)))

    for (a, b), pts in LIVE_TRACED_CONNECTIONS.items():
        bidir_pairs.append((a, b, pts[a], pts[b], "live_trace"))

    resolved_rows, unresolved_pairs = resolve_bidir_pairs(bidir_pairs, _OUT_RESOLVED)

    bidir_keys = set(tuple(sorted((a, b))) for a, b, _, _, _ in bidir_pairs)
    onesided = []
    for (m, t), spawn in tag_matches.items():
        if tuple(sorted((m, t))) in bidir_keys:
            continue
        onesided.append((m, MAP_ID_TO_NAME.get(m, f"map_{m}"), t, MAP_ID_TO_NAME.get(t, f"map_{t}"),
                          "one_sided_tag_only", spawn, None))

    write_unresolved_csv(_OUT_UNRESOLVED, onesided, unresolved_pairs)


if __name__ == "__main__":
    main()
