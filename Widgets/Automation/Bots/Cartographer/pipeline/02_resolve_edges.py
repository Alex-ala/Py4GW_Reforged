"""
V2 pipeline, stage 2: for every wiki-graph edge (01_wiki_graph.py), find
where the portal actually sits, in priority order (per user design
2026-07-19):

  1. Spawn-tag "dead giveaway": a numeric tag equal to the other map's own
     map_id, or a nickname tag that is a clear prefix of the other map's
     name -- trusted directly, no geometric confirmation needed, BUT only
     when it's unambiguous. Critically, within a same-navmesh CLUSTER of
     3+ map_ids sharing one file (e.g. Cursed Lands/Nebo Terrace/Bergen
     Hot Springs), a numeric/nickname tag is NOT trusted as a dead
     giveaway at all: since every cluster member shares the identical
     spawn list, a tag like '0059' cannot distinguish "the gate to Nebo
     specifically" from "the gate to Bergen" -- this is exactly the bug
     that produced a phantom (7720,-3191) midpoint for Cursed<->Nebo in
     v1 (see project memory). Simple 2-member same-file pairs (e.g. an
     outpost carved out of its own explorable area) have no such
     ambiguity and ARE trusted.

  2. Different navmesh files: match pathing-mesh boundary edges near a
     seed point on each side (they overlap under one rigid XY offset --
     reuses lib_build_portal_graph.find_transform, already validated).
     Seed points tried in order: dead-giveaway tag position(s) first (even
     if not "clean" enough to trust alone under rule 1), then every other
     spawn tag, cheapest first.

  3. Same file, cluster size 3+: identify which of the file's real
     portal-marker props are independently matched (via the existing
     decorative-gate-transform-byte signal, cached in
     candidate_portal_pairs_by_transform.csv / _template_default.csv) to
     a DIFFERENT file entirely -- exclude those (they're some OTHER edge
     from this shared landmass, not this one). Among what's left, a pair
     of markers matching EACH OTHER's transform signature is the real
     intra-cluster gate. If nothing survives exclusion, or nothing pairs
     up, the connection is almost certainly gateless (no physical portal
     prop at all, just an invisible boundary within one continuous
     landmass) -- honestly unresolved rather than guessed, per
     feedback_prefer_honest_unmatched_over_guess.

Run: python 02_resolve_edges.py
Outputs: output/resolved_v2.csv, output/unresolved_v2.csv
"""
import concurrent.futures as _cf
import csv
import json
import math
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # pipeline/ (this file's own dir, 2026-07-20 v2->standard promotion)

import lib_build_portal_graph as bpg
from lib_map_file_ids import MAP_ID_TO_DAT_FILE_ID
from lib_map_names import MAP_ID_TO_NAME

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR = _THIS_DIR  # this file now lives directly in pipeline/, not pipeline/v2/
_OUT_DIR = os.path.join(_THIS_DIR, "output")

_WIKI_GRAPH_JSON = os.path.join(_OUT_DIR, "wiki_graph.json")
_ALL_SPAWNS_JSON = os.path.join(_PIPELINE_DIR, "output", "all_spawns_expanded.json")
_ALL_PORTALS_CSV = os.path.join(_PIPELINE_DIR, "output", "all_portals.csv")
_ORPHAN_MATCHES_CSV = os.path.join(_PIPELINE_DIR, "output", "orphan_map_matches.csv")
_TRANSFORM_MATCH_CSV = os.path.join(_PIPELINE_DIR, "output", "candidate_portal_pairs_by_transform.csv")
_TRANSFORM_TEMPLATE_CSV = os.path.join(_PIPELINE_DIR, "output", "candidate_portal_pairs_template_default.csv")
_OUT_RESOLVED = os.path.join(_OUT_DIR, "resolved_v2.csv")
_OUT_UNRESOLVED = os.path.join(_OUT_DIR, "unresolved_v2.csv")

STOP_CONFIDENCE = 0.3
MAX_SEEDS_PER_SIDE = 12  # cost guard on the cross-navmesh seed search
# Hard wall-clock cap per pair, on top of MAX_NEARBY_EDGES in
# lib_build_portal_graph.py -- mirrors 08_build_connection_graph.py's
# PAIR_TIME_BUDGET_S, ported here 2026-07-20 after this exact failure mode
# recurred: 297/299 pairs finished in under 2 hours, then Seitung
# Harbor<->Kaineng Docks and Bejunkan Pier<->Consulate Docks outpost (dense
# Kaineng-area meshes) stalled with no log progress for 58+ minutes and 8+
# hours total elapsed. Checked BETWEEN combo attempts (can't interrupt one
# already in progress, same accepted limitation v1 documents;
# MAX_NEARBY_EDGES bounds a single call's worst case instead).
#
# Raised 180 -> 600 the same day after the first value proved TOO TIGHT,
# not just a hang-preventer: Nebo Terrace<->North Kryta Province (a real,
# doubly-cross-validated connection -- confirmed independently via the
# prop-transform-byte identification on BOTH sides, matching this
# resolver's own 0.83-confidence answer exactly) measured at 121s
# SINGLE-THREADED with zero contention -- already 67% of the old 180s
# budget alone. Under this run's real 15-way parallel contention it tipped
# over 180s and got cut off before reaching that correct candidate,
# silently reporting a near-zero "low_confidence" result instead of the
# real 0.83 match. 600s gives ~5x headroom over the measured single-
# threaded cost (room for contention on this and slower pairs) while
# staying far below the original unbounded-hang failure mode above.
PAIR_TIME_BUDGET_S = 600.0

# Explicit human-confirmed ground truth for edges the algorithm can only
# honestly call ambiguous/gateless (same role as v1's LIVE_TRACED_CONNECTIONS
# for cross-navmesh pairs -- this is the SAME-FILE-cluster equivalent).
# Position is identical for both sides (same file = same coordinate space).
LIVE_CONFIRMED: dict[frozenset, tuple[float, float]] = {
    frozenset((56, 59)): (-4181.0, -11659.0),  # Cursed Lands <-> Nebo Terrace:
    # user stood at the real in-game portal and confirmed ~(-4594,-11391),
    # 492u from this marker -- see project memory. Part of the Cursed
    # Lands/Nebo Terrace/Bergen Hot Springs 3-way same-navmesh cluster;
    # marker-exclusion alone leaves this ambiguous against Cursed<->Bergen
    # and Bergen<->Nebo (all 3 edges would otherwise compete for the same
    # single leftover, unclaimed marker).
}


def get_file_id(map_id):
    return bpg.get_file_id(map_id)


def _load_extra_file_ids():
    """map_id -> orphan file hash, for the unambiguously-recovered subset of
    maps missing from MAP_ID_TO_DAT_FILE_ID -- same helper as
    08_build_connection_graph.py's _load_extra_file_ids(), duplicated here
    (numbered-filename modules aren't import-friendly) so v2 benefits from
    the same orphan-recovery fallback instead of silently reporting
    unknown_file_id for those maps."""
    extra = {}
    if not os.path.exists(_ORPHAN_MATCHES_CSV):
        return extra
    by_orphan = defaultdict(list)
    with open(_ORPHAN_MATCHES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["best_orphan_hash"] and int(row["match_score"]) >= 1:
                by_orphan[row["best_orphan_hash"]].append(row)
    for h, claimants in by_orphan.items():
        if len(claimants) == 1:
            extra[int(claimants[0]["map_id"])] = int(h)
    return extra


def load_spawns():
    with open(_ALL_SPAWNS_JSON, encoding="utf-8") as f:
        raw = json.load(f)
    return {int(k): [(tag, x, y) for tag, x, y in v] for k, v in raw.items()}


def build_file_clusters():
    """file_id -> set of map_ids sharing it (only entries with >= 2)."""
    by_file = defaultdict(set)
    for mid in MAP_ID_TO_DAT_FILE_ID:
        by_file[get_file_id(mid)].add(mid)
    return {fid: members for fid, members in by_file.items() if fid is not None and len(members) >= 2}


def load_external_marker_matches():
    """{(round(x), round(y)): destination_map_name} for every marker on any
    map that the existing transform-byte-matching tool already identified
    as leading to a DIFFERENT file -- used to exclude non-intra-cluster
    markers in the same-file/3+-cluster case. Keyed on rounded position
    only (positions are shared verbatim across a cluster's member map_ids,
    same underlying file)."""
    ident = {}
    for path in (_TRANSFORM_MATCH_CSV, _TRANSFORM_TEMPLATE_CSV):
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ident[(round(float(row["x_a"])), round(float(row["y_a"])))] = row["map_name_b"]
                ident[(round(float(row["x_b"])), round(float(row["y_b"])))] = row["map_name_a"]
    return ident


_PORTAL_PROPS_BY_MAP: dict[int, list[tuple]] | None = None  # cached whole-CSV parse


def _load_all_portals() -> dict[int, list[tuple]]:
    """map_id -> [(portal_x, portal_y, nearest_spawn_pos_or_None), ...],
    parsed once per process and cached (this file is read repeatedly --
    once per candidate seed across hundreds of pairs -- so re-parsing it
    every call, like the old load_cluster_markers() did, adds up)."""
    global _PORTAL_PROPS_BY_MAP
    if _PORTAL_PROPS_BY_MAP is not None:
        return _PORTAL_PROPS_BY_MAP
    by_map = defaultdict(list)
    if os.path.exists(_ALL_PORTALS_CSV):
        with open(_ALL_PORTALS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                mid = int(row["map_id"])
                px, py = float(row["portal_x"]), float(row["portal_y"])
                sx_raw, sy_raw = row.get("spawn_x"), row.get("spawn_y")
                spawn_pos = (float(sx_raw), float(sy_raw)) if sx_raw and sy_raw else None
                by_map[mid].append((px, py, spawn_pos))
    _PORTAL_PROPS_BY_MAP = dict(by_map)
    return _PORTAL_PROPS_BY_MAP


def load_cluster_markers(map_id):
    """ALL portal-marker prop positions on map_id (same-file cluster
    exclusion needs every prop regardless of spawn linkage)."""
    return [(px, py) for px, py, _sp in _load_all_portals().get(map_id, [])]


# Anchor tuning (user observation 2026-07-20): a real portal-marker PROP is
# ground truth for a portal's existence and location -- paired gate props
# line up far more tightly across two connected maps than an arbitrary
# spawn point does (spawn tags just say WHICH destination a NEARBY portal
# is about, per build_all_portals.py's own docstring). Anchoring the
# transform search on the prop instead of the spawn, when one is close
# enough to trust, lets the search radius shrink accordingly.
PROP_ANCHOR_RADIUS = 4000.0
# How close a spawn must be to a prop's recorded nearest-spawn link to
# count as "this spawn is about that prop" -- matches build_all_portals.py's
# own _SPAWN_MATCH_RADIUS exactly, so we're reusing the SAME association
# that CSV already encodes, not inventing a new threshold.
PROP_ASSOCIATION_TOL = 2000.0


def nearest_portal_prop(map_id, spawn_pos):
    """The portal-marker prop this spawn point is most likely 'about', or
    None if no prop's recorded nearest-spawn link is within
    PROP_ASSOCIATION_TOL of it (e.g. a genuinely gateless boundary
    crossing, or a spawn unrelated to any nearby portal)."""
    sx, sy = spawn_pos
    best, best_d = None, PROP_ASSOCIATION_TOL
    for px, py, sp in _load_all_portals().get(map_id, []):
        if sp is None:
            continue
        d = math.hypot(sp[0] - sx, sp[1] - sy)
        if d <= best_d:
            best, best_d = (px, py), d
    return best


def numeric_tag_candidates(spawns_by_map, map_id, target_id):
    """Spawns on map_id numerically tagged for target_id (excluding
    self-reference). No corroboration required -- wiki already
    established the edge exists, we only need a position."""
    out = []
    for tag, x, y in spawns_by_map.get(map_id, []):
        if tag and tag.isdigit() and int(tag) == target_id and target_id != map_id:
            out.append((x, y))
    return out


def name_prefix_tag_candidates(spawns_by_map, map_id, target_name):
    """Spawns on map_id with a non-numeric tag that's a clear text prefix
    of target_name (normalized, lowercased) -- e.g. 'curs' <-> 'Cursed
    Lands'. Same no-corroboration-needed reasoning as numeric tags."""
    norm_target = "".join(ch for ch in target_name.lower() if ch.isalnum())
    out = []
    for tag, x, y in spawns_by_map.get(map_id, []):
        if not tag or tag.isdigit():
            continue
        if len(tag) >= 3 and norm_target.startswith(tag.lower()):
            out.append((x, y))
    return out


_ALL_NORMALIZED_MAP_NAMES = None


def _tag_matches_some_known_map(tag):
    """True if `tag` is a numeric id present in the map catalog, or a
    non-numeric prefix of ANY known map's normalized name (not just the
    current pair's two members) -- i.e. this tag is explainable as a
    reference to a *specific, different* destination somewhere in the
    whole 413-map graph. Used to find tags that are explainable NO OTHER
    way, see _unclaimed_nearby_tag_count below."""
    global _ALL_NORMALIZED_MAP_NAMES
    if not tag:
        return True  # empty/no tag: not "unidentified", just absent
    if tag.isdigit():
        return int(tag) in MAP_ID_TO_NAME
    if _ALL_NORMALIZED_MAP_NAMES is None:
        _ALL_NORMALIZED_MAP_NAMES = [
            "".join(ch for ch in name.lower() if ch.isalnum())
            for name in MAP_ID_TO_NAME.values()
        ]
    if len(tag) < 3:
        return False
    tag_lower = tag.lower()
    return any(norm.startswith(tag_lower) for norm in _ALL_NORMALIZED_MAP_NAMES)


def _unclaimed_nearby_tag_count(map_id, prop_pos, spawns_by_map, already_used):
    """How many spawns whose NEAREST real portal prop (nearest_portal_prop,
    same anchoring logic used to build the candidate groups themselves) is
    exactly `prop_pos` carry a tag that matches NO known map anywhere in
    the catalog -- i.e. cannot be evidence for some OTHER destination --
    excluding positions already counted in `already_used`. A tag like this
    being closer to one real portal prop than to any other, with no
    alternative explanation available, is weak but real corroboration that
    THIS prop is the gate the pair's own (already-matched) candidate is
    also pointing at."""
    count = 0
    for tag, x, y in spawns_by_map.get(map_id, []):
        if (x, y) in already_used:
            continue
        if _tag_matches_some_known_map(tag):
            continue
        if nearest_portal_prop(map_id, (x, y)) == prop_pos:
            count += 1
    return count


def resolve_cross_navmesh(archive, a, b, spawns_by_map):
    """Different-file case: dead-giveaway seeds first, then every other
    spawn, cheapest/most-specific first; first confident transform wins.

    Anchors (2026-07-20, user observation): a spawn tag only identifies
    WHICH destination a nearby portal is about -- it is not itself the
    portal. Each candidate spawn is checked against nearest_portal_prop();
    when a real portal-marker prop correlates, that prop's own position
    replaces the spawn as the anchor (used for BOTH the geometric search
    and the final reported portal position), with a tighter search radius
    since paired gate props line up far more precisely across two maps
    than an arbitrary spawn point does. Falls back to the raw spawn
    position + the full radius when no prop correlates (e.g. a genuinely
    gateless boundary crossing has no prop to anchor on at all)."""
    def seeds(map_id, other_id, other_name):
        dead = (numeric_tag_candidates(spawns_by_map, map_id, other_id)
                + name_prefix_tag_candidates(spawns_by_map, map_id, other_name))
        others = [(x, y) for tag, x, y in spawns_by_map.get(map_id, [])
                  if tag and not (tag.isdigit() and int(tag) == map_id)]
        ordered = dead + [p for p in others if p not in dead]
        anchored = []
        seen_props = set()
        for x, y in ordered:
            prop = nearest_portal_prop(map_id, (x, y))
            if prop is not None:
                anchored.append((prop, True))
                seen_props.add(prop)
            else:
                anchored.append(((x, y), False))
        # Also add every real portal-marker prop on this map directly, even
        # ones with no nearby spawn tag at all -- a real prop is valid
        # evidence on its own, and the exhaustive both-anchored search
        # below GEOMETRICALLY confirms it via find_transform's trapezoid-
        # shape matching (the actual navmesh neighborhood, not just point-
        # transform algebra), so an untagged prop only ever wins if its
        # surroundings really line up -- it doesn't get assumed correct
        # just because it happens to sit where a transform predicts (user
        # instruction 2026-07-21: "portals do not always snap on... do not
        # just snap the portals together but use their surroundings to
        # find a trapezoid snap"). Found live: Lion's Arch<->North Kryta
        # Province both have a real 0xA825 prop for this exact connection,
        # but Lion's Arch's (253,12387) has no nearby spawn tag, so the
        # tag-based loop above never surfaces it as a candidate at all.
        for prop_pos in load_cluster_markers(map_id):
            if prop_pos not in seen_props:
                anchored.append((prop_pos, True))
                seen_props.add(prop_pos)
        # Prop-anchored seeds first (stable sort -- preserves the existing
        # dead-giveaway-then-others order WITHIN each tier). Below, the
        # first seed PAIR to clear STOP_CONFIDENCE wins outright and short-
        # circuits the search, so seed ORDER decides which candidate's
        # position gets reported, not just which ones are eligible. Found
        # live 2026-07-21 (Diessa Lowlands<->Nolani Academy outpost): the
        # raw 'next' spawn (no nearby prop) cleared STOP_CONFIDENCE=0.3
        # first and got averaged into a position 2860u off; the very next
        # candidate in file order, 'nort', is anchored to a real 0xA825
        # marker and -- when actually tried -- finds the identical dx,dy at
        # 0.99 confidence with 0 discrepancy against the other side's own
        # prop-anchored position. Truncating to MAX_SEEDS_PER_SIDE only
        # AFTER this sort (not before) matters too: a late-listed but
        # prop-anchored spawn must not be dropped in favor of earlier,
        # weaker, un-anchored ones.
        anchored.sort(key=lambda item: not item[1])
        return anchored[:MAX_SEEDS_PER_SIDE]

    name_a = MAP_ID_TO_NAME.get(a, f"map_{a}")
    name_b = MAP_ID_TO_NAME.get(b, f"map_{b}")
    seeds_a = seeds(a, b, name_b)
    seeds_b = seeds(b, a, name_a)
    if not seeds_a or not seeds_b:
        return None, "no_spawn_seeds"

    best = None
    best_pair = None
    best_props = None
    _start = time.monotonic()

    # Phase 1: exhaustively try every BOTH-SIDES-prop-anchored combo (small,
    # cheap set -- typically a handful per side once deduped) with NO early
    # exit. Two different real portal-marker props can each independently
    # confirm the SAME correct transform yet score very differently, since
    # a prop's own local search radius can happen to capture more or less
    # of the matching trapezoid neighborhood -- taking the FIRST combo to
    # merely clear STOP_CONFIDENCE instead of the best among all of them
    # can lock in a needlessly truncated, low-confidence match (and report
    # the WRONG one of the two props as the portal position). Found live
    # 2026-07-21: Nebo Terrace<->Beetletun's 'op1' prop scored only 0.304
    # confidence and got reported, while 'farm' -- confirming the exact
    # same dx,dy -- scored a clean 1.000 (365/365) and was never tried
    # because 'op1' happened to come first and already cleared the old
    # single-pass early-exit bar.
    # Ranked by ABSOLUTE match count, not confidence ratio (user
    # instruction 2026-07-21, "larger absolute match counts are good"):
    # confidence = matches/len(traps_a) is NOT symmetric -- it depends on
    # how many trapezoids happen to sit in radius around whichever side
    # was passed as `a`, not on match quality. Found live: North Kryta
    # Province<->Nebo Terrace's real, well-supported pairing (139/169
    # matched, confidence 0.822) was being beaten by a coincidental small-
    # sample pairing (36/36 = confidence 1.000, but only 36 trapezoids
    # existed in that radius at all) that also happened to steal a marker
    # (prop 64) that rightfully belonged to a different edge (Bergen Hot
    # Springs<->Nebo Terrace) on the same shared file. 139 real matches is
    # objectively stronger evidence than 36, regardless of ratio.
    anchored_a = [(p, True) for p, is_anchored in seeds_a if is_anchored]
    anchored_b = [(p, True) for p, is_anchored in seeds_b if is_anchored]
    for pa, prop_a in anchored_a:
        for pb, prop_b in anchored_b:
            if time.monotonic() - _start > PAIR_TIME_BUDGET_S:
                break
            t = bpg.find_transform(archive, a, pa, b, pb, radius=PROP_ANCHOR_RADIUS)
            if t is None:
                continue
            if best is None or t["matches"] > best["matches"]:
                best, best_pair, best_props = t, (pa, pb), (prop_a, prop_b)
        if time.monotonic() - _start > PAIR_TIME_BUDGET_S:
            break

    # Phase 2: only if phase 1 didn't already find a confident match, fall
    # back to the original early-exit search across ALL seeds (including
    # raw, un-anchored spawn points) -- that space can be much larger, so
    # early-exit-on-first-good-enough stays the right tradeoff there.
    if best is None or best["confidence"] < STOP_CONFIDENCE:
        for pa, prop_a in seeds_a:
            for pb, prop_b in seeds_b:
                if time.monotonic() - _start > PAIR_TIME_BUDGET_S:
                    break
                radius = PROP_ANCHOR_RADIUS if (prop_a and prop_b) else bpg.EDGE_SEARCH_RADIUS
                t = bpg.find_transform(archive, a, pa, b, pb, radius=radius)
                if t is None:
                    continue
                if best is None or t["confidence"] > best["confidence"]:
                    best, best_pair, best_props = t, (pa, pb), (prop_a, prop_b)
                if t["confidence"] >= STOP_CONFIDENCE:
                    break
            if (best is not None and best["confidence"] >= STOP_CONFIDENCE) \
                    or time.monotonic() - _start > PAIR_TIME_BUDGET_S:
                break

    if best is None:
        return None, "no_transform_found"
    if best["confidence"] < bpg.MIN_CONFIDENCE:
        return None, f"low_confidence({best['confidence']:.2f})"

    dx, dy = best["dx"], best["dy"]
    pa, pb = best_pair
    prop_a, prop_b = best_props
    # Position (user rule, 2026-07-20): a portal is ALWAYS a real prop --
    # when a side is prop-anchored, pa/pb IS already that prop's own
    # coordinates, so report it directly rather than blending it with the
    # other side's transform-projected estimate (the transform can be
    # excellent but is still an estimate; the real prop position is
    # ground truth and shouldn't be diluted by averaging it with one).
    # Only fall back to the transform-based midpoint on a side that has no
    # real prop to anchor on at all (gateless, or evidence not found).
    if prop_a:
        portal_a = pa
    else:
        b_in_a = (pb[0] + dx, pb[1] + dy)
        portal_a = ((pa[0] + b_in_a[0]) / 2, (pa[1] + b_in_a[1]) / 2)
    if prop_b:
        portal_b = pb
    else:
        a_in_b = (pa[0] - dx, pa[1] - dy)
        portal_b = ((pb[0] + a_in_b[0]) / 2, (pb[1] + a_in_b[1]) / 2)
    return {"portal_a": portal_a, "portal_b": portal_b,
            "method": "cross_navmesh_transform", "confidence": best["confidence"]}, None


TWIN_MARKER_TOL = 3000.0  # markers within this distance of EACH OTHER are one
# physical gate represented by two decorative instances (one per side),
# not two distinct gates. Found live 2026-07-21: Bergen Hot Springs<->Nebo
# Terrace's real gate is two separate 0x858B props only 65u apart (one per
# side, user-confirmed via live prop dumps); every pair of genuinely
# DIFFERENT real gates found on any shared file in this whole graph so far
# sits at least ~13000u apart. This is comfortably below that gap and
# generalizes to any cluster with the same twin-marker pattern -- not
# specific to this one pair.


def _group_nearby_markers(markers, tol=TWIN_MARKER_TOL):
    """Greedy proximity clustering (transitive): a marker joins the first
    existing group it's within `tol` of any member of; otherwise it starts
    a new group. Returns a list of groups, each a list of (x, y) markers
    -- used to recognize a single-gate "twin" marker pair as ONE candidate
    instead of two independently-competing ones."""
    groups: list[list[tuple[float, float]]] = []
    for m in markers:
        for g in groups:
            if any(math.hypot(m[0] - o[0], m[1] - o[1]) <= tol for o in g):
                g.append(m)
                break
        else:
            groups.append([m])
    return groups


def resolve_same_file_cluster(a, b, cluster, external_idents, spawns_by_map, claimed_markers,
                              excluded_positions=(), remaining_cluster_edges=None):
    """Same file, 3+ member cluster: exclude externally-identified
    markers; whatever survives is diagnostic-only UNLESS it's provably
    unambiguous (see the elimination rule below).

    excluded_positions (2026-07-20): also drops any marker sitting on top
    of a position already resolved this run as a cross-navmesh portal on
    either map_id -- a second, independent exclusion source alongside
    external_idents (the pre-existing transform-byte CSV), narrowing the
    diagnostic candidate count further even though this function still
    never auto-resolves from it.

    Tried and rejected in this session's own testing: "if exactly one
    marker survives exclusion, trust it for this edge" -- caught assigning
    the SAME leftover marker to both Cursed<->Nebo and Cursed<->Bergen
    (impossible, one gate can't serve two distinct pairwise connections).
    Adding claimed-marker tracking stopped the duplicate, but WHICH edge
    wins the claim is still purely processing-order-dependent -- tested
    directly: it picked Cursed<->Bergen over the actually-live-confirmed
    Cursed<->Nebo, i.e. the wrong one.

    Elimination rule (2026-07-21, user design, fully dynamic -- no per-pair
    data): a same-file connection can be represented by either ONE real
    portal-marker prop (shared by both directions) or TWO placed close
    together (one per side, matching the ordinary cross-navmesh
    convention) rather than always a single marker -- see TWIN_MARKER_TOL.
    `remaining_cluster_edges` is computed by the caller from how many of
    THIS cluster's own wiki edges are still unresolved by the time this
    call happens (not hardcoded -- recomputed fresh every run from
    whatever the wiki graph + prior resolution passes actually produced).
    When surviving candidates group into exactly ONE marker-group AND this
    is provably the cluster's only outstanding edge, there is no
    competing edge left that could claim the same group -- auto-resolve,
    using the group's single marker or (for a 2-marker twin group) their
    midpoint, since there's no dynamic way to tell which of a twin pair is
    the A->B vs B->A side. Still NEVER resolves when more than one
    edge/group is outstanding -- that's exactly the untrustworthy
    processing-order-dependent case above, per
    feedback_prefer_honest_unmatched_over_guess."""
    markers = load_cluster_markers(a)  # identical across every cluster member
    candidates = [m for m in markers if (round(m[0]), round(m[1])) not in external_idents
                  and not any(math.hypot(m[0] - ex[0], m[1] - ex[1]) <= SAME_FILE_AGREEMENT_TOL
                              for ex in excluded_positions)]
    unclaimed = [m for m in candidates if (round(m[0]), round(m[1])) not in claimed_markers]
    if not candidates:
        return None, "gateless_all_markers_external"
    if not unclaimed:
        return None, f"gateless_all_{len(candidates)}_surviving_markers_claimed_by_LIVE_CONFIRMED_edges"

    groups = _group_nearby_markers(unclaimed)
    if remaining_cluster_edges == 1 and len(groups) == 1:
        group = groups[0]
        if len(group) == 1:
            pos = group[0]
            method = "same_navmesh_cluster_last_edge"
        else:
            pos = (sum(p[0] for p in group) / len(group), sum(p[1] for p in group) / len(group))
            method = "same_navmesh_cluster_last_edge_twin"
        for m in group:
            claimed_markers.add((round(m[0]), round(m[1])))
        return {"portal_a": pos, "portal_b": pos, "method": method, "confidence": 0.9}, None

    return None, (f"gateless_or_ambiguous({len(unclaimed)}_unclaimed_candidate(s)_in_{len(groups)}_group(s)_"
                  f"survive_exclusion, no reliable way to assign to a SPECIFIC edge among this cluster's -- "
                  f"needs live confirmation, see LIVE_CONFIRMED)")


SAME_FILE_AGREEMENT_TOL = 2500.0  # matches v1's SAME_NAVMESH_AGREEMENT_TOL


def resolve_same_file_simple_pair(a, b, spawns_by_map, excluded_positions=()):
    """Same file, exactly 2 members: no CROSS-MEMBER ambiguity (a tag
    numerically/textually matching the other map's own id/name can't be
    about a third map) -- but that does NOT mean every such tag is
    automatically about THIS pair's own portal. The shared file's spawn
    pool is the whole file's spawns, and a file can host more than one
    portal (this pair's, plus this map's own separate exits to other,
    unrelated maps) -- a spawn near one of THOSE other portals can still
    coincidentally carry a tag that numerically matches this pair's other
    member.

    Found live 2026-07-20 (checked all 64 simple-pair edges in the real
    graph): 49 of 64 have candidate tags that disagree by >2500u, several
    by 30-49k units -- e.g. Diessa Lowlands(13)/Grendich Courthouse(36)'s
    shared file has a tight 3-point cluster of '0036' tags at the real
    gate, PLUS a lone '0013' tag ~38000u away that turned out to be right
    on top of Diessa's SEPARATE, already-resolved cross-navmesh portal to
    The Breach -- yet the old code blindly averaged all 4 together into a
    single "confidence 1.0" phantom midpoint. Same averaging-dilution bug
    class already found and fixed twice elsewhere in this project (v1's
    SAME_NAVMESH_AGREEMENT_TOL, the Scoundrel's Rise multi-candidate fix),
    just not here yet.

    Fix (user design, 2026-07-20): resolve every DIFFERENT-file (cross-
    navmesh) portal first, across the whole graph, before attempting any
    same-file pair -- see main()'s pass ordering. `excluded_positions` is
    every position already resolved as a cross-navmesh portal on either
    map_id of this pair; any candidate tag landing near one of those is
    PROVEN to belong to that other, already-identified connection, not
    this one, and gets dropped before clustering. This is what correctly
    throws out Grendich's stray '0013' tag: it's within tolerance of
    Diessa's confirmed Breach position, so it's excluded outright rather
    than needing a prop-confirmation guess (which was tried first and
    shown to be unsafe -- a false-positive "nearby real prop" match is
    exactly how the Breach gate would have been mislabeled as Grendich's).

    Position (user rule, 2026-07-20): a portal is ALWAYS a real portal-
    marker prop -- a spawn tag only ever IDENTIFIES which prop a
    destination's gate is, it never contributes to the reported
    coordinate. Found live investigating Temple of the Ages<->Black
    Curtain: even after exclusion correctly threw out a stray candidate,
    the surviving 3 candidates got averaged directly into a "confidence
    1.0" position sitting ~1958u from the real, unclaimed `op1` prop --
    spawn points mark where the CHARACTER arrives, not where the gate
    model sits, so averaging several of them can never land exactly on
    the real gate. Fix: every surviving candidate is snapped to its
    nearest real prop (nearest_portal_prop) and grouped by PROP IDENTITY,
    not spatial proximity; the reported position is always that prop's
    own (x, y), never a spawn-derived average. If every surviving
    candidate agrees on one prop, or one prop has a clear majority (more
    independent tags agreeing beats fewer, same principle already used
    for nickname-tag majority voting elsewhere), trust it. If distinct
    props are tied, or NO candidate is close enough to any real prop at
    all (a genuinely gateless crossing, or one needing live confirmation),
    refuse rather than fabricate a position from spawn data alone."""
    name_a, name_b = MAP_ID_TO_NAME.get(a, f"map_{a}"), MAP_ID_TO_NAME.get(b, f"map_{b}")
    cands = (numeric_tag_candidates(spawns_by_map, a, b)
             + name_prefix_tag_candidates(spawns_by_map, a, name_b)
             + numeric_tag_candidates(spawns_by_map, b, a)
             + name_prefix_tag_candidates(spawns_by_map, b, name_a))
    if not cands:
        return None, "same_navmesh_no_tag_evidence"

    # Exclude a candidate only if it snaps to the SAME real portal prop as
    # an already-resolved external position -- prop IDENTITY, not a flat
    # distance radius. Found live 2026-07-20: Lornar's Pass/Beacon's Perch
    # has TWO distinct real gates only ~1500-1800u apart; the (imprecise,
    # 0.742-confidence, non-prop-anchored) Deldrimor Bowl exclusion
    # position happened to sit almost equidistant between them, so a flat
    # SAME_FILE_AGREEMENT_TOL=2500u radius wrongly blanket-excluded ALL 4
    # tag candidates -- when only ONE of them (the one snapping to the
    # SAME prop the Deldrimor Bowl connection snaps to) is actually that
    # same gate; the other 3 snap to a different, genuinely separate prop.
    excluded_props = {nearest_portal_prop(a, ex) for ex in excluded_positions}
    excluded_props.discard(None)
    if excluded_props:
        cands = [c for c in cands if nearest_portal_prop(a, c) not in excluded_props]
    if not cands:
        return None, "same_navmesh_all_candidates_externally_identified"

    prop_groups: dict[tuple[float, float], list] = defaultdict(list)
    unanchored = []
    for c in cands:
        prop = nearest_portal_prop(a, c)
        if prop is not None:
            prop_groups[prop].append(c)
        else:
            unanchored.append(c)

    if not prop_groups:
        return None, f"same_navmesh_no_prop_found({len(unanchored)}_candidates_unanchored)"

    ranked = sorted(prop_groups.items(), key=lambda kv: -len(kv[1]))
    if len(ranked) == 1:
        prop_pos, _ = ranked[0]
        return {"portal_a": prop_pos, "portal_b": prop_pos,
                "method": "same_navmesh_simple_pair", "confidence": 1.0}, None

    if len(ranked[0][1]) > len(ranked[1][1]):
        prop_pos, _ = ranked[0]
        return {"portal_a": prop_pos, "portal_b": prop_pos,
                "method": "same_navmesh_simple_pair_majority", "confidence": 1.0}, None

    # Still tied on direct tag evidence. Before giving up, check for a
    # WEAKER but real signal: spawns near one of the tied props whose tag
    # matches no known map anywhere in the whole catalog (so it cannot be
    # evidence for some OTHER, third destination) -- see
    # _unclaimed_nearby_tag_count. Found live 2026-07-21 (Arborstone
    # outpost<->explorable): a classic 0xA825 prop anchored by one '0244'
    # tag tied 1-1 against a newly-catalogued 0xE723 prop anchored by one
    # '0218' tag, but the 0xE723 prop's own closest spawn was an
    # unidentified 'tree' tag closer than its own anchoring candidate --
    # with no alternative explanation available for 'tree' anywhere in the
    # 413-map catalog, it's real (if indirect) corroboration for that prop
    # specifically. This is a general rule, not a per-pair override (see
    # feedback_no_static_pipeline_overrides) -- it re-scores every tied
    # pair in the graph the same way, not just this one.
    top_count = len(ranked[0][1])
    tied_props = [(prop_pos, members) for prop_pos, members in ranked if len(members) == top_count]
    if len(tied_props) >= 2:
        already_used = set(cands)
        scored = sorted(
            (
                (prop_pos, top_count + _unclaimed_nearby_tag_count(a, prop_pos, spawns_by_map, already_used))
                for prop_pos, _ in tied_props
            ),
            key=lambda kv: -kv[1],
        )
        if scored[0][1] > scored[1][1]:
            prop_pos, _ = scored[0]
            return {"portal_a": prop_pos, "portal_b": prop_pos,
                    "method": "same_navmesh_simple_pair_tiebreak_unidentified_tag",
                    "confidence": 0.8}, None

    return None, (f"same_navmesh_disagreement({len(prop_groups)}_distinct_props_tied, "
                  f"sizes={sorted((len(v) for v in prop_groups.values()), reverse=True)})")


_worker_archive = None
_worker_spawns_by_map = None


def _worker_init(extra_file_ids, spawns_by_map):
    # Same rationale as 08_build_connection_graph.py's _worker_init: a
    # module-global set by the parent AFTER import is not reliably visible
    # in worker processes (fork-only, not spawn-safe) -- pass explicitly.
    global _worker_archive, _worker_spawns_by_map
    bpg.EXTRA_FILE_IDS = extra_file_ids
    _worker_archive = bpg._archive()
    _worker_spawns_by_map = spawns_by_map


def _worker_resolve_cross_navmesh(args):
    a, b = args
    result, reason = resolve_cross_navmesh(_worker_archive, a, b, _worker_spawns_by_map)
    return a, b, result, reason


def main():
    with open(_WIKI_GRAPH_JSON, encoding="utf-8") as f:
        edges = json.load(f)["edges"]

    extra_file_ids = _load_extra_file_ids()
    bpg.EXTRA_FILE_IDS = extra_file_ids
    print(f"{len(extra_file_ids)} recovered maps given a fallback file-id for trapezoid loading", flush=True)

    spawns_by_map = load_spawns()
    clusters = build_file_clusters()
    external_idents = load_external_marker_matches()
    claimed_markers: set[tuple[int, int]] = set()

    results: dict[tuple[int, int], tuple[dict | None, str | None]] = {}
    cross_navmesh_tasks = []
    same_file_tasks = []  # (a, b, fid) -- resolved AFTER cross-navmesh, see below

    # Pass 1 (classification only): split edges by file relationship.
    # Cross-navmesh tasks get queued for the worker pool; same-file tasks
    # are deliberately NOT resolved yet -- see pass 3.
    for edge in edges:
        a, b = edge["map_a"], edge["map_b"]
        fid_a, fid_b = get_file_id(a), get_file_id(b)

        if fid_a is not None and fid_a == fid_b:
            same_file_tasks.append((a, b, fid_a))
        elif fid_a is not None and fid_b is not None:
            cross_navmesh_tasks.append((a, b))
        else:
            results[(a, b)] = (None, "file_id_unknown_for_a_or_b")

    # Pass 2 (parallel, expensive -- find_transform geometric search): one
    # archive load per worker process via _worker_init, mirrors
    # 08_build_connection_graph.py's resolve_bidir_pairs pool pattern.
    # Runs BEFORE same-file resolution on purpose (user design, 2026-07-20):
    # every different-file portal gets identified first, so same-file
    # resolution can cross those positions off its own candidate list --
    # see resolved_positions_by_map below.
    max_workers = max(1, (os.cpu_count() or 4) - 1)
    print(f"{len(cross_navmesh_tasks)} cross-navmesh pairs to resolve, using {max_workers} worker processes",
          flush=True)
    with _cf.ProcessPoolExecutor(
        max_workers=max_workers, initializer=_worker_init, initargs=(dict(extra_file_ids), spawns_by_map),
    ) as pool:
        futures = {pool.submit(_worker_resolve_cross_navmesh, task): task for task in cross_navmesh_tasks}
        done = 0
        for fut in _cf.as_completed(futures):
            a, b, result, reason = fut.result()
            results[(a, b)] = (result, reason)
            done += 1
            name_a, name_b = MAP_ID_TO_NAME.get(a, f"map_{a}"), MAP_ID_TO_NAME.get(b, f"map_{b}")
            if result is None:
                print(f"[cross-navmesh {done}/{len(cross_navmesh_tasks)}] UNRESOLVED {name_a} <-> {name_b}: "
                      f"{reason}", flush=True)
            else:
                print(f"[cross-navmesh {done}/{len(cross_navmesh_tasks)}] {result['method']}: "
                      f"{name_a} <-> {name_b} conf={result['confidence']:.2f}", flush=True)

    # Exclusion index (user design, 2026-07-20): every position already
    # resolved as a DIFFERENT-file portal, per map_id. This is what makes
    # the "B+C share a file, but B also exits to A and C also exits to D"
    # scenario safe -- since A<->B and C<->D are cross-navmesh and just got
    # resolved above, B's own A-ward position and C's own D-ward position
    # are now provably accounted for, so same-file resolution below can
    # drop any of its own candidates that land on one of these instead of
    # guessing (or averaging) across a stray, unrelated tag. This is
    # exactly what catches Diessa Lowlands/Grendich Courthouse's stray
    # '0013' tag, which sits on top of Diessa's own already-resolved
    # cross-navmesh position for The Breach.
    resolved_positions_by_map: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for (ra, rb), (result, _reason) in results.items():
        if result is not None and result["method"] == "cross_navmesh_transform":
            resolved_positions_by_map[ra].append(result["portal_a"])
            resolved_positions_by_map[rb].append(result["portal_b"])
    print(f"{sum(len(v) for v in resolved_positions_by_map.values())} cross-navmesh positions "
          f"available to exclude from same-file resolution", flush=True)

    # Pass 3 (sequential, cheap -- no archive/trapezoid access): same-file
    # resolution, now armed with the exclusion index above.
    # Sub-pass 3a: everything answerable without needing to know how many
    # OTHER edges in a shared 3+ cluster are still outstanding --
    # LIVE_CONFIRMED entries and true 2-member simple pairs.
    for a, b, fid_a in same_file_tasks:
        live_pos = LIVE_CONFIRMED.get(frozenset((a, b)))
        if live_pos is not None:
            results[(a, b)] = ({"portal_a": live_pos, "portal_b": live_pos,
                                "method": "same_navmesh_live_confirmed", "confidence": 1.0}, None)
            # Mark the real marker (if any) at this position as claimed so
            # sub-pass 3b's cluster elimination rule doesn't still count
            # it as a live, unclaimed competitor for a sibling edge on the
            # same shared file (e.g. Cursed Lands<->Nebo Terrace's marker
            # must not still look "available" when resolving Bergen Hot
            # Springs<->Nebo Terrace on the same 3-member cluster).
            claimed_markers.add((round(live_pos[0]), round(live_pos[1])))
            continue
        cluster = clusters.get(fid_a, {a, b})
        if len(cluster) <= 2:
            excluded = resolved_positions_by_map.get(a, []) + resolved_positions_by_map.get(b, [])
            results[(a, b)] = resolve_same_file_simple_pair(a, b, spawns_by_map, excluded)

    # Sub-pass 3b: same-file 3+ clusters. `remaining_cluster_edges` is
    # computed fresh here from how many of THIS cluster's own wiki edges
    # are still unresolved after 3a -- not a hardcoded fact about any
    # specific cluster -- so resolve_same_file_cluster's elimination rule
    # only fires when there's exactly one outstanding edge cluster-wide.
    cluster_tasks_3plus = [(a, b, fid_a) for a, b, fid_a in same_file_tasks if (a, b) not in results]
    cluster_remaining: dict[int, int] = defaultdict(int)
    for a, b, fid_a in cluster_tasks_3plus:
        cluster_remaining[fid_a] += 1
    for a, b, fid_a in cluster_tasks_3plus:
        cluster = clusters.get(fid_a, {a, b})
        excluded = resolved_positions_by_map.get(a, []) + resolved_positions_by_map.get(b, [])
        results[(a, b)] = resolve_same_file_cluster(
            a, b, cluster, external_idents, spawns_by_map, claimed_markers, excluded,
            remaining_cluster_edges=cluster_remaining[fid_a])

    # Pass 4: emit rows in the original wiki-graph edge order (deterministic
    # output regardless of which worker finished a given task first).
    resolved_rows = []
    unresolved_rows = []
    for edge in edges:
        a, b = edge["map_a"], edge["map_b"]
        name_a, name_b = MAP_ID_TO_NAME.get(a, f"map_{a}"), MAP_ID_TO_NAME.get(b, f"map_{b}")
        result, reason = results[(a, b)]

        if result is None:
            unresolved_rows.append((a, name_a, b, name_b, reason))
            continue

        pax, pay = result["portal_a"]
        pbx, pby = result["portal_b"]
        for m1, n1, m2, n2, px, py in ((a, name_a, b, name_b, pax, pay),
                                        (b, name_b, a, name_a, pbx, pby)):
            resolved_rows.append({
                "map_id": m1, "map_name": n1, "neighbor_map_id": m2, "neighbor_name": n2,
                "portal_x": round(px, 1), "portal_y": round(py, 1),
                "method": result["method"], "confidence": round(result["confidence"], 3),
                "source": "wiki_v2",
            })

    with open(_OUT_RESOLVED, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "map_id", "map_name", "neighbor_map_id", "neighbor_name",
            "portal_x", "portal_y", "method", "confidence", "source",
        ])
        w.writeheader()
        w.writerows(resolved_rows)

    with open(_OUT_UNRESOLVED, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["map_id", "map_name", "other_map_id", "other_map_name", "reason"])
        w.writerows(unresolved_rows)

    print(f"\nresolved: {len(resolved_rows) // 2} pairs -> {_OUT_RESOLVED}", flush=True)
    print(f"unresolved: {len(unresolved_rows)} -> {_OUT_UNRESOLVED}", flush=True)


if __name__ == "__main__":
    main()
