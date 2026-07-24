"""
Fetch and parse per-map "Exits" data from wiki.guildwars.com raw wikitext.

Prefers the prose "==Exits==" section (has compass-direction hints); falls
back to the infobox `exits = A<br>B<br>C` field (names only, no direction)
when no prose section exists. Results are cached to disk (wiki_exits_cache.json)
so repeated runs don't re-hit the wiki for maps already fetched, and also
written out flat to CSV (one row per map-neighbor exit) for direct
inspection or as an input elsewhere.

Run: python 07_fetch_wiki_adjacency.py [map_id ...]   # specific maps
     python 07_fetch_wiki_adjacency.py --all           # every named map (~655, ~5-10 min)
     python 07_fetch_wiki_adjacency.py                 # same as --all
Output: output/wiki_exits_cache.json (JSON cache, keyed by map_id, read by
        08_build_connection_graph.py), output/wiki_adjacency.csv (flat,
        human-readable: map_id, map_name, neighbor_map_id, neighbor_title,
        direction)
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_PATH = os.path.join(_THIS_DIR, "output", "wiki_exits_cache.json")
_CSV_PATH = os.path.join(_THIS_DIR, "output", "wiki_adjacency.csv")

_USER_AGENT = "Py4GW-gwdat-tools-research-script/1.0 (offline map graph export)"
_REQUEST_DELAY_S = 0.3

_DIRECTION_WORDS = (
    "northeast", "northwest", "southeast", "southwest",
    "north", "south", "east", "west",
)

_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")


@dataclass
class ExitEntry:
    target_title: str
    direction: Optional[str]  # 'northeast', etc, or None if unknown
    note: str  # raw trailing text after the link(s), for manual review


@dataclass
class WikiExitsResult:
    page_title: Optional[str]   # the title that actually resolved, or None if nothing found
    source: str                 # 'prose', 'infobox', or 'none'
    exits: list  # list of ExitEntry (as dicts when loaded from cache)


def _load_cache() -> dict:
    if os.path.exists(_CACHE_PATH):
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    tmp = _CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, ensure_ascii=False)
    os.replace(tmp, _CACHE_PATH)


def _fetch_raw(title: str) -> Optional[str]:
    url = "https://wiki.guildwars.com/index.php?" + urllib.parse.urlencode(
        {"title": title, "action": "raw"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except urllib.error.URLError:
        return None


def _resolve_redirect(text: str, depth: int = 0) -> tuple[Optional[str], Optional[str]]:
    """If `text` is a #REDIRECT page, fetch the target and return (target_title, target_text)."""
    if depth > 3:
        return None, None
    m = re.match(r"\s*#REDIRECT\s*\[\[([^\]|#]+)", text, re.IGNORECASE)
    if not m:
        return None, text
    target = m.group(1).strip()
    time.sleep(_REQUEST_DELAY_S)
    target_text = _fetch_raw(target)
    if target_text is None:
        return target, None
    redirected_again, final_text = _resolve_redirect(target_text, depth + 1)
    if redirected_again:
        return redirected_again, final_text
    return target, target_text


def _parse_direction(trailing: str) -> Optional[str]:
    low = trailing.lower()
    for word in _DIRECTION_WORDS:
        if word in low:
            return word
    return None


def _norm_title(title: str) -> str:
    t = title.lower().strip()
    for suffix in (" (outpost)", " (explorable area)", " outpost"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return t.replace("'", "").strip()


# Matches sentences like "South: Near the exit to Riverside Province" or
# "Northeast, near the exit to [[Majesty's Rest]]" -- these live outside any
# dedicated "==Exits==" section (most often under "==Shrines=="), and are
# the only direction signal available for maps that only have the plain
# infobox exits list (most of them). The target after "to/into" is often a
# *plain* mention rather than a [[wikilink]] -- MediaWiki convention only
# links a term's first occurrence on a page, and it's frequently already
# linked earlier (e.g. in the infobox) -- so this captures the raw trailing
# text and _apply_direction_hints() matches it against known exit names by
# substring rather than requiring brackets.
_EXIT_HINT_RE = re.compile(
    r"\b(northeast|northwest|southeast|southwest|north|south|east|west)\b"
    r"[^.\n]{0,60}?exit\w*[^.\n]{0,20}?(?:to|into)\s*([^.\n]{0,60})",
    re.IGNORECASE,
)


def _scan_direction_hints(wikitext: str) -> list[tuple[str, str]]:
    """List of (direction, trailing_text) scanned from anywhere in the page."""
    hints = []
    for m in _EXIT_HINT_RE.finditer(wikitext):
        direction = m.group(1).lower()
        trailing = re.sub(r"[\[\]]", "", m.group(2))
        hints.append((direction, trailing))
    return hints


def _apply_direction_hints(entries: list[ExitEntry], wikitext: str) -> list[ExitEntry]:
    hints = _scan_direction_hints(wikitext)
    if not hints:
        return entries
    out = []
    for e in entries:
        if e.direction:
            out.append(e)
            continue
        target_norm = _norm_title(e.target_title)
        found = None
        for direction, trailing in hints:
            if target_norm and target_norm in _norm_title(trailing):
                found = direction
                break
        if found:
            out.append(ExitEntry(target_title=e.target_title, direction=found, note=e.note + " [direction from page text]"))
        else:
            out.append(e)
    return out


def _parse_prose_exits(wikitext: str) -> Optional[list[ExitEntry]]:
    m = re.search(r"^==\s*Exits\s*==\s*$", wikitext, re.IGNORECASE | re.MULTILINE)
    if not m:
        return None
    start = m.end()
    m2 = re.search(r"^==[^=]", wikitext[start:], re.MULTILINE)
    section = wikitext[start: start + m2.start()] if m2 else wikitext[start:]

    entries: list[ExitEntry] = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("*"):
            continue
        links = list(_LINK_RE.finditer(line))
        if not links:
            continue
        last_link_end = links[-1].end()
        trailing = line[last_link_end:]
        direction = _parse_direction(trailing)
        # one bullet can reference more than one page (rare); record each.
        for lm in links:
            title = lm.group(1).strip()
            entries.append(ExitEntry(target_title=title, direction=direction, note=trailing.strip(" *:.")))
    return entries or None


def _parse_infobox_exits(wikitext: str) -> Optional[list[ExitEntry]]:
    m = re.search(r"\|\s*exits\s*=\s*(.*?)\n\s*\|", wikitext, re.IGNORECASE | re.DOTALL)
    if not m:
        m = re.search(r"\|\s*exits\s*=\s*(.*?)\n", wikitext, re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1)
    entries: list[ExitEntry] = []
    for lm in _LINK_RE.finditer(raw):
        entries.append(ExitEntry(target_title=lm.group(1).strip(), direction=None, note=""))
    return entries or None


def get_exits_for_map(map_id: int, title_variants: list[str], cache: dict) -> WikiExitsResult:
    cache_key = str(map_id)
    if cache_key in cache:
        c = cache[cache_key]
        return WikiExitsResult(
            page_title=c["page_title"],
            source=c["source"],
            exits=[ExitEntry(**e) for e in c["exits"]],
        )

    result = WikiExitsResult(page_title=None, source="none", exits=[])
    for title in title_variants:
        time.sleep(_REQUEST_DELAY_S)
        text = _fetch_raw(title)
        if text is None:
            continue
        resolved_title, resolved_text = _resolve_redirect(text)
        if resolved_title:
            title = resolved_title
        if resolved_text is None:
            continue

        prose = _parse_prose_exits(resolved_text)
        if prose is not None:
            result = WikiExitsResult(page_title=title, source="prose", exits=_apply_direction_hints(prose, resolved_text))
            break
        infobox = _parse_infobox_exits(resolved_text)
        if infobox is not None:
            result = WikiExitsResult(page_title=title, source="infobox", exits=_apply_direction_hints(infobox, resolved_text))
            break
        # Page exists but has neither (often a disambiguation page, e.g.
        # base "X" when only "X (outpost)"/"X (explorable area)" carry real
        # content) -- remember we found *a* page, but keep trying other
        # title variants for one with actual exits data.
        if result.page_title is None:
            result = WikiExitsResult(page_title=title, source="none", exits=[])

    cache[cache_key] = {
        "page_title": result.page_title,
        "source": result.source,
        "exits": [asdict(e) for e in result.exits],
    }
    return result


def main() -> None:
    from lib_map_file_ids import MAP_ID_TO_DAT_FILE_ID
    from lib_map_names import MAP_ID_TO_NAME, wiki_title_variants, resolve_map_id_by_name

    args = sys.argv[1:]
    if not args or args == ["--all"]:
        map_ids = sorted(MAP_ID_TO_NAME.keys())
    else:
        map_ids = sorted(int(a) for a in args)

    cache = _load_cache()
    print(f"{len(map_ids)} map(s) to fetch, {len(cache)} already cached")

    prefer_ids = set(MAP_ID_TO_DAT_FILE_ID)
    t_start = time.time()
    fetched = 0
    for i, map_id in enumerate(map_ids):
        if i % 20 == 0:
            print(f"  {i}/{len(map_ids)} ({time.time()-t_start:.0f}s elapsed)", flush=True)
            _save_cache(cache)  # periodic save so a crash/interrupt doesn't lose progress

        variants = wiki_title_variants(map_id)
        if not variants:
            continue
        was_cached = str(map_id) in cache
        get_exits_for_map(map_id, variants, cache)
        if not was_cached:
            fetched += 1

    _save_cache(cache)
    print(f"DONE in {time.time()-t_start:.0f}s. {fetched} newly fetched, {len(cache)} total cached.")
    print(f"-> {_CACHE_PATH}")

    # Flatten the full cache (not just this run's map_ids) out to CSV --
    # the CSV should always reflect everything we know, not just this run.
    rows = []
    for map_id_str, entry in cache.items():
        map_id = int(map_id_str)
        map_name = MAP_ID_TO_NAME.get(map_id, "")
        for exit_ in entry.get("exits", []):
            neighbor_id = resolve_map_id_by_name(exit_["target_title"], prefer_ids=prefer_ids)
            rows.append({
                "map_id": map_id,
                "map_name": map_name,
                "neighbor_map_id": neighbor_id if neighbor_id is not None else "",
                "neighbor_title": exit_["target_title"],
                "direction": exit_.get("direction") or "",
            })
    rows.sort(key=lambda r: (r["map_id"], r["neighbor_title"]))

    os.makedirs(os.path.dirname(_CSV_PATH), exist_ok=True)
    with open(_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["map_id", "map_name", "neighbor_map_id", "neighbor_title", "direction"])
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} adjacency rows -> {_CSV_PATH}")


if __name__ == "__main__":
    main()
