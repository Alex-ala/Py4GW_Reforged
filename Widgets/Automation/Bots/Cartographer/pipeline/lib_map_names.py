"""
Map id <-> display name lookups, loaded directly from
Py4GWCoreLib/enums_src/Map_enums.py without going through the
Py4GWCoreLib package __init__ (which imports native game-only modules
like PyScanner/Py4GW that don't exist outside the injected process).
"""
import importlib.util
import os

def _find_repo_root() -> str:
    """Walk up from this file until the directory containing Py4GWCoreLib.
    Relocated out of gwdat_tools (2026-07-19): the old fixed "one level up"
    relative path assumed gwdat_tools sat directly beside Py4GWCoreLib;
    this pipeline now lives several directories deeper inside Reforged."""
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isdir(os.path.join(d, "Py4GWCoreLib")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            raise FileNotFoundError("Py4GWCoreLib not found above " + __file__)
        d = parent


_MAP_ENUMS_PATH = os.path.join(_find_repo_root(), "Py4GWCoreLib", "enums_src", "Map_enums.py")


def _load_map_enums():
    spec = importlib.util.spec_from_file_location("map_enums_standalone", _MAP_ENUMS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_map_enums = _load_map_enums()

OUTPOSTS: dict[int, str] = _map_enums.outposts
EXPLORABLES: dict[int, str] = _map_enums.explorables

# id -> display name (as used in-game / in this codebase), outposts + explorables combined.
MAP_ID_TO_NAME: dict[int, str] = {**OUTPOSTS, **EXPLORABLES}

# Seasonal/event map variant -> base map id (e.g. 837 Talmark Wilderness -
# War in Kryta -> 17 Talmark Wilderness). Hand-curated in Map_enums.py for
# an unrelated purpose (identifying "am I in Kamadan" across festival
# reskins), but doubles as strong evidence for our own file_id resolution:
# every other entry in this table that has a known file_id on BOTH sides
# turns out to share the IDENTICAL file_id (checked all 13 checkable pairs,
# 2026-07-20) -- these reskins reuse the same underlying map file verbatim,
# they're not just "similar", so get_file_id() below treats an unscanned
# variant's file_id as its base map's.
MAP_VARIANTS_TO_BASE: dict[int, int] = _map_enums.map_variants_to_base


def _norm(name: str) -> str:
    """Loose-match key: lowercase, drop common disambiguation suffixes/parens."""
    n = name.lower().strip()
    for suffix in (" outpost", " (outpost)", " (explorable area)"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    n = n.replace("'", "").replace("(", "").replace(")", "")
    return " ".join(n.split())


# normalized name -> list of map_ids sharing that normalized name (there can be
# several, e.g. seasonal Kamadan variants, or an outpost and its attached
# explorable area sharing a base name).
_NORM_TO_IDS: dict[str, list[int]] = {}
for _id, _name in MAP_ID_TO_NAME.items():
    _NORM_TO_IDS.setdefault(_norm(_name), []).append(_id)


def resolve_map_id_by_name(name: str, prefer_ids: set[int] | None = None) -> int | None:
    """Resolve a (possibly wiki-link-formatted) map name to a map_id.

    `prefer_ids`, if given, breaks ties toward a candidate already known to
    exist in our own map set (e.g. avoid seasonal/event variants nobody asked
    about) -- currently unused for filtering, just prioritizes ordering.
    """
    key = _norm(name)
    candidates = _NORM_TO_IDS.get(key)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if prefer_ids:
        for cid in candidates:
            if cid in prefer_ids:
                return cid
    return candidates[0]


def wiki_title_for_map_id(map_id: int) -> str | None:
    """Best-effort guess at the wiki.guildwars.com page title for a map_id.

    Our internal outpost names are usually "X outpost" (no parens); the wiki
    prefers "X (outpost)" only when needed for disambiguation, and plain "X"
    otherwise. We try the plain-name form first since it's correct far more
    often, and let the caller fall back to other forms on a 404.
    """
    name = MAP_ID_TO_NAME.get(map_id)
    if name is None:
        return None
    if name.endswith(" outpost"):
        return name[: -len(" outpost")]
    return name


def wiki_title_variants(map_id: int) -> list[str]:
    """All wiki title forms worth trying, in priority order."""
    name = MAP_ID_TO_NAME.get(map_id)
    if name is None:
        return []
    variants = []
    if name.endswith(" outpost"):
        base = name[: -len(" outpost")]
        variants.append(base)
        variants.append(f"{base} (outpost)")
    else:
        variants.append(name)
    if name not in variants:
        variants.append(name)
    return variants
