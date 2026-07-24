"""Pure-python FFNA chunk parsers, copied from
Py4GWCoreLib/native_src/methods/FfnaMapMethods.py (offline portal/spawn
detection section) so they can run outside the injected game process.
"""
import struct
import math
from dataclasses import dataclass

FFNA_MAGIC = 0x616E6666
CHUNK_TYPE_SPAWN = 0x20000007
CHUNK_TYPE_PROPINFO = 0x20000004
CHUNK_TYPE_PROPFILENAMES = 0x21000004
CHUNK_TYPE_PROPFILENAMES_ALT = 0x21000003

# Kept in sync with this pipeline's own scan/07_find_portal_pairs_by_transform.py
# CANDIDATE_MODEL_FILE_IDS -- that script already trusted 0x1C533/0x5E77A on
# real connections (Snake Dance<->Grenth's Footprint, Deldrimor Bowl<->Anvil
# Rock, Sage Lands<->Mamnoon Lagoon, and now map 880<->The Breach, confirmed
# live 2026-07-23 with matching model_file_id on both ends) but this dict
# never picked them up, so 06_build_all_portals.py silently missed them.
_PORTAL_MODEL_FILE_IDS = {
    0x4E6B2: "EotN Asura Gate",
    0x3C5AC: "EotN/Nightfall",
    0x0A825: "Prophecies/Factions",
    0xE723: "Prophecies/Factions (alt)",
    0x858B: "Prophecies/Factions (alt 2)",
    0x1C533: "Gate variant",
    0x5E77A: "Gate variant",
}


@dataclass(slots=True)
class SpawnPoint:
    x: float
    y: float
    angle: float
    tag: str

    @property
    def zone_map_id(self):
        if self.tag and self.tag.isdigit():
            return int(self.tag)
        return None

    @property
    def is_default(self):
        return self.tag == '0000'


def is_ffna_pathing(data: bytes) -> bool:
    if len(data) < 5:
        return False
    magic = struct.unpack_from('<I', data, 0)[0]
    return magic == FFNA_MAGIC and data[4] == 3


def _parse_chunks(data: bytes):
    pos = 5
    chunks = []
    while pos + 8 <= len(data):
        chunk_type, chunk_length = struct.unpack_from('<ii', data, pos)
        pos += 8
        if pos + chunk_length > len(data):
            break
        chunks.append((chunk_type, chunk_length, data[pos:pos + chunk_length]))
        pos += chunk_length
    return chunks


def _angle_byte_to_float(b: int) -> float:
    signed = b if b < 128 else b - 256
    return signed * (2.0 * math.pi / 254.0)


def _parse_tagged_entries(chunk: bytes, off: int):
    if off + 2 > len(chunk):
        return [], off
    count = struct.unpack_from('<H', chunk, off)[0]
    off += 2
    entries = []
    for _ in range(count):
        if off + 13 > len(chunk):
            break
        x, y = struct.unpack_from('<ii', chunk, off)
        angle_byte = chunk[off + 8]
        tag_u32 = struct.unpack_from('<I', chunk, off + 9)[0]
        tag_be = struct.pack('>I', tag_u32)
        tag_str = ''.join(chr(c) if 32 <= c < 127 else '' for c in tag_be)
        entries.append(SpawnPoint(x=float(x), y=float(y), angle=_angle_byte_to_float(angle_byte), tag=tag_str))
        off += 13
    return entries, off


def _parse_float_entries(chunk: bytes, off: int):
    if off + 2 > len(chunk):
        return [], off
    count = struct.unpack_from('<H', chunk, off)[0]
    off += 2
    entries = []
    for _ in range(count):
        if off + 8 > len(chunk):
            break
        x, y = struct.unpack_from('<ff', chunk, off)
        entries.append(SpawnPoint(x=x, y=y, angle=0.0, tag=''))
        off += 8
    return entries, off


_SPAWN_HEADER_SIZE = 0x1D


def parse_ffna_spawns(data: bytes):
    if not is_ffna_pathing(data):
        return None
    chunks = _parse_chunks(data)
    spawn_chunk = None
    for ct, _cl, cd in chunks:
        if ct == CHUNK_TYPE_SPAWN:
            spawn_chunk = cd
            break
    if spawn_chunk is None or len(spawn_chunk) < _SPAWN_HEADER_SIZE + 2:
        return None
    off = _SPAWN_HEADER_SIZE
    spawns1, off = _parse_tagged_entries(spawn_chunk, off)
    spawns2, off = _parse_tagged_entries(spawn_chunk, off)
    spawns3, off = _parse_float_entries(spawn_chunk, off)
    return spawns1, spawns2, spawns3


def _file_hash_to_file_id_offline(id0: int, id1: int) -> int:
    return ((id0 - 0xFF00FF) + id1 * 0xFF00) & 0xFFFFFFFF


def parse_ffna_prop_filenames(data: bytes):
    if not is_ffna_pathing(data):
        return []
    chunks = _parse_chunks(data)
    chunk_data = None
    for ct, _cl, cd in chunks:
        if ct in (CHUNK_TYPE_PROPFILENAMES, CHUNK_TYPE_PROPFILENAMES_ALT):
            chunk_data = cd
            break
    if chunk_data is None or len(chunk_data) < 5:
        return []
    entry_data = chunk_data[5:]
    num_entries = len(entry_data) // 6
    file_ids = []
    for i in range(num_entries):
        off = i * 6
        id0, id1 = struct.unpack_from('<HH', entry_data, off)
        file_ids.append(_file_hash_to_file_id_offline(id0, id1))
    return file_ids


def parse_ffna_prop_positions(data: bytes):
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
    num_props = struct.unpack_from('<H', chunk_data, off)[0]
    off += 2
    props = []
    for _ in range(num_props):
        if off + 48 > len(chunk_data):
            break
        filename_index = struct.unpack_from('<H', chunk_data, off)[0]
        x, y, z = struct.unpack_from('<fff', chunk_data, off + 2)
        num_trailing = chunk_data[off + 47]
        off += 48 + num_trailing * 8
        props.append((filename_index, x, y, z))
    return props


CHUNK_TYPE_TRAPEZOID = 0x20000008


@dataclass(slots=True)
class GWPathingTrapezoid:
    adjacent1: int
    adjacent2: int
    adjacent3: int
    adjacent4: int
    transition1: int
    transition2: int
    yt: float
    yb: float
    xtl: float
    xtr: float
    xbl: float
    xbr: float

    @property
    def cx(self):
        return (self.xtl + self.xtr + self.xbl + self.xbr) / 4

    @property
    def cy(self):
        return (self.yt + self.yb) / 2


@dataclass(slots=True)
class FFNAPortalRecord:
    count: int
    trap_offset: int
    portal_index: int


@dataclass(slots=True)
class FFNAPlaneData:
    trapezoids: list = None
    portal_records: list = None
    portal_trap_ids: list = None

    def __post_init__(self):
        if self.trapezoids is None:
            self.trapezoids = []
        if self.portal_records is None:
            self.portal_records = []
        if self.portal_trap_ids is None:
            self.portal_trap_ids = []


def parse_ffna_pathing(data: bytes):
    """Parse FFNA pathing chunk: trapezoids + portal data per plane.
    Copied from FfnaMapMethods.parse_ffna_pathing (pure, no game deps)."""
    if not is_ffna_pathing(data):
        return None
    chunks = _parse_chunks(data)
    trap_chunk = None
    for ct, cl, cd in chunks:
        if ct == CHUNK_TYPE_TRAPEZOID:
            trap_chunk = (cl, cd)
            break
    if trap_chunk is None:
        return None
    chunk_length, chunk_data = trap_chunk
    if chunk_length < 17:
        return None

    cur_pos = 13
    if cur_pos + 4 > chunk_length:
        return None
    skip_len = struct.unpack_from('<i', chunk_data, cur_pos)[0]
    cur_pos += skip_len + 5 + 4
    if cur_pos + 4 > chunk_length or cur_pos < 0:
        return None
    _section_count = struct.unpack_from('<i', chunk_data, cur_pos)[0]
    cur_pos += 4

    planes = []
    current_plane = None

    while cur_pos < chunk_length:
        section_header = chunk_data[cur_pos]
        cur_pos += 1
        if cur_pos + 4 > chunk_length:
            break
        if section_header != 0x0B:
            section_length = struct.unpack_from('<i', chunk_data, cur_pos)[0]
        else:
            section_length = struct.unpack_from('<i', chunk_data, cur_pos)[0] // 2
        cur_pos += 4
        sec_end = cur_pos + section_length

        if section_header == 0x00:
            current_plane = FFNAPlaneData()
            planes.append(current_plane)
        elif section_header == 0x02 and current_plane is not None:
            tmp_pos = cur_pos
            while tmp_pos + 44 <= sec_end:
                if tmp_pos + 44 > chunk_length:
                    break
                adj1, adj2, adj3, adj4 = struct.unpack_from('<iiii', chunk_data, tmp_pos)
                tmp_pos += 16
                trans1, trans2 = struct.unpack_from('<HH', chunk_data, tmp_pos)
                tmp_pos += 4
                yt, yb, xtl, xtr, xbl, xbr = struct.unpack_from('<ffffff', chunk_data, tmp_pos)
                tmp_pos += 24
                current_plane.trapezoids.append(GWPathingTrapezoid(
                    adjacent1=adj1, adjacent2=adj2, adjacent3=adj3, adjacent4=adj4,
                    transition1=trans1, transition2=trans2,
                    yt=yt, yb=yb, xtl=xtl, xtr=xtr, xbl=xbl, xbr=xbr,
                ))
        elif section_header == 0x09 and current_plane is not None:
            tmp_pos = cur_pos
            while tmp_pos + 9 <= sec_end:
                cnt, off, _f2, pidx = struct.unpack_from('<HHHH', chunk_data, tmp_pos)
                current_plane.portal_records.append(FFNAPortalRecord(count=cnt, trap_offset=off, portal_index=pidx))
                tmp_pos += 9
        elif section_header == 0x0A and current_plane is not None:
            tmp_pos = cur_pos
            while tmp_pos + 4 <= sec_end:
                tid = struct.unpack_from('<I', chunk_data, tmp_pos)[0]
                current_plane.portal_trap_ids.append(tid)
                tmp_pos += 4
        elif section_header in (0x0C, 0x0D, 0x0E, 0xFF):
            break

        cur_pos = sec_end

    return planes if planes else None


def parse_travel_portals(data: bytes):
    filenames = parse_ffna_prop_filenames(data)
    if not filenames:
        return []
    positions = parse_ffna_prop_positions(data)
    if not positions:
        return []
    portals = []
    for filename_index, x, y, z in positions:
        if filename_index >= len(filenames):
            continue
        fid = filenames[filename_index]
        if fid in _PORTAL_MODEL_FILE_IDS:
            portals.append((x, y, z, fid, _PORTAL_MODEL_FILE_IDS[fid]))
    return portals
