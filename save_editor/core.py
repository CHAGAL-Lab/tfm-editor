"""Core logic for reading and writing save files: parsing, athlete list, writing star count, age and gold, backup/restore. Standard library only.

Shared by the CLI (cli.py) and the GUI (gui.py). It prints nothing to the screen.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

from . import steam
from .i18n import t

APP_ID = "1372810"                                   # Steam's Teamfight Manager
FROZEN = getattr(sys, "frozen", False)               # whether this is a PyInstaller bundle
# Backups and settings live in the source folder or next to the executable (a frozen executable unpacks into a temp folder on every run, so anything written there would vanish).
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", ROOT))  # read-only bundled data (names.json, pyproject.toml)
BACKUP_DIR = ROOT / "backups"
NAMES_FILE = RESOURCE_DIR / "data" / "names.json"
SAVE_SUBPATH = ("AppData", "LocalLow", "samoyed", "Teamfight Manager")
# Star limit. Only up to 5 was checked in game (streaming screen); 6-9 is a value chosen because the growth table structure allows it.
MAX_STARS = 9
# Age range. The lower bound 17 is there because the game's growth table starts at age 17 and anything younger reads outside the table.
# The upper bound is an arbitrary value, not one verified as safe (age 27 and up use the last column of the growth table).
MIN_AGE, MAX_AGE = 17, 40


def stars_note():
    """Notice shown when 4 or more stars are chosen. The squad screen draws up to 3 and the streaming screen up to 5 (only the screens checked)."""
    return t('core.stars_note')


# The gold limit is also arbitrary, not verified as safe (just chosen so it does not overflow the screen).
MAX_GOLD = 9_999_999


# ---- Save parsing (.NET BinaryFormatter, MS-NRBF) ----
PRIM = {1: '?', 2: 'B', 6: 'd', 7: 'h', 8: 'i', 9: 'q', 10: 'b', 11: 'f', 12: 'q', 13: 'q', 14: 'H', 15: 'I', 16: 'Q'}


class Reader:
    def __init__(self, d):
        self.d, self.p = d, 0

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def i32(self):
        v = struct.unpack_from('<i', self.d, self.p)[0]
        self.p += 4
        return v

    def lps(self):
        n = shift = 0
        while True:
            b = self.u8()
            n |= (b & 0x7f) << shift
            shift += 7
            if not b & 0x80:
                break
        v = self.d[self.p:self.p + n].decode('utf-8')
        self.p += n
        return v

    def prim(self, t):
        if t == 3:
            b0 = self.d[self.p]
            n = 1 if b0 < 0x80 else 2 if b0 < 0xE0 else 3 if b0 < 0xF0 else 4
            v = self.d[self.p:self.p + n].decode('utf-8')
            self.p += n
            return v
        if t in (5, 18):
            return self.lps()
        fmt = '<' + PRIM[t]
        v = struct.unpack_from(fmt, self.d, self.p)[0]
        self.p += struct.calcsize(fmt)
        return v


class Parser:
    """MS-NRBF parser. Records the primitive member values of class instances and their positions in the file (offs)."""

    def __init__(self, data, reader=None):
        self.r = reader or Reader(data)
        self.classes = {}
        self.objs = {}
        self.spans = {}           # object id -> (start, end): byte range of the record that defines the object in the stream
        self.libs = {}            # class id -> library id (the value written in the record that defines the class)
        self.meta_use = {}        # class id -> [(object id, position of the class id)]: records that use a class id without defining the class
        self.refs = {}            # object id -> [(start, end), ...]: reference records pointing at that object

    def parse(self):
        r = self.r
        assert r.u8() == 0, t('nrbf.bad_header')
        r.i32(), r.i32(), r.i32(), r.i32()
        while self.record()[0] != 'END':
            pass
        return self

    def type_extra(self, bt):
        r = self.r
        if bt in (0, 7):
            return r.u8()
        if bt == 3:
            return r.lps()
        if bt == 4:
            return (r.lps(), r.i32())
        return None

    def class_info(self):
        r = self.r
        oid, name, n = r.i32(), r.lps(), r.i32()
        return oid, name, [r.lps() for _ in range(n)]

    def record(self):
        start = self.r.p
        rec = self._record()
        if rec[0] == 'obj':
            self.spans[rec[1]] = (start, self.r.p)
        elif rec[0] == 'ref':
            self.refs.setdefault(rec[1], []).append((start, self.r.p))
        return rec

    def _record(self):
        r = self.r
        while True:
            rec_type = r.u8()
            if rec_type == 12:           # BinaryLibrary
                r.i32()
                r.lps()
                continue
            break
        if rec_type == 11:
            return ('END',)
        if rec_type == 10:
            return ('null', 1)
        if rec_type == 13:
            return ('null', r.u8())
        if rec_type == 14:
            return ('null', r.i32())
        if rec_type == 9:
            return ('ref', r.i32())
        if rec_type == 6:
            oid = r.i32()
            self.objs[oid] = {'class': 'String', 'str': r.lps()}
            return ('obj', oid)
        if rec_type == 8:
            pt = r.u8()
            return ('prim', pt, r.prim(pt))
        if rec_type in (4, 5):
            oid, name, names = self.class_info()
            bts = [r.u8() for _ in names]
            extras = [self.type_extra(bt) for bt in bts]
            if rec_type == 5:
                self.libs[oid] = r.i32()
            meta = (name, names, bts, extras)
            self.classes[oid] = meta
            return self.members(oid, meta)
        if rec_type == 1:
            oid = r.i32()
            mid_at = r.p
            mid = r.i32()
            self.meta_use.setdefault(mid, []).append((oid, mid_at))
            return self.members(oid, self.classes[mid])
        if rec_type == 7:
            oid, at, rank = r.i32(), r.u8(), r.i32()
            len_at = r.p if rank == 1 else None
            lens = [r.i32() for _ in range(rank)]
            if at >= 3:
                [r.i32() for _ in range(rank)]
            bt = r.u8()
            extra = self.type_extra(bt)
            total = 1
            for n in lens:
                total *= n
            self.objs[oid] = dict(zip(('items', 'spans'), self.elements(bt, extra, total)), **{'class': 'Array', 'len_at': len_at})
            return ('obj', oid)
        if rec_type == 15:
            oid, n, pt = r.i32(), r.i32(), r.u8()
            if pt == 2:
                r.p += n
            else:
                for _ in range(n):
                    r.prim(pt)
            self.objs[oid] = {'class': 'PrimArray'}
            return ('obj', oid)
        if rec_type in (16, 17):
            oid = r.i32()
            len_at = r.p
            n = r.i32()
            self.objs[oid] = dict(zip(('items', 'spans'), self.elements(2 if rec_type == 16 else 1, None, n)),
                                  **{'class': 'Array', 'len_at': len_at})
            return ('obj', oid)
        raise ValueError(t('nrbf.bad_record', type=rec_type, pos=r.p - 1))

    def elements(self, bt, extra, count):
        """Read the array elements and return them: the value for value types, otherwise the record (a run of nulls counts as one)."""
        out, spans, i = [], [], 0
        while i < count:
            spans.append(self.r.p)
            if bt == 0:
                out.append(self.r.prim(extra))
                i += 1
            else:
                rec = self.record()
                out.append(rec)
                i += rec[1] if rec[0] == 'null' else 1
        spans.append(self.r.p)
        return out, spans

    def members(self, oid, meta):
        name, names, bts, extras = meta
        vals, offs, pending = {}, {}, 0
        for k, nm in enumerate(names):
            if bts[k] == 0:
                offs[nm] = self.r.p
                vals[nm] = ('prim', extras[k], self.r.prim(extras[k]))
                continue
            if pending:
                pending -= 1
                vals[nm] = ('null', 1)
                continue
            rec = self.record()
            vals[nm] = rec
            if rec[0] == 'null':
                pending = rec[1] - 1
        self.objs[oid] = {'class': name, 'vals': vals, 'offs': offs}
        return ('obj', oid)


def ref_id(v):
    return v[1] if v[0] in ('obj', 'ref') else None


def analyze(data):
    """A file can hold several BinaryFormatter streams in a row (preview + main data). Pick the stream that contains athletes."""
    r = Reader(data)
    found = []
    while r.p < len(data):
        ps = Parser(data, r).parse()
        teams = {i: o for i, o in ps.objs.items() if o['class'] == 'TeamInfo'}
        aths = {i: o for i, o in ps.objs.items() if o['class'] == 'Athlete'}
        if aths:
            found.append((ps, teams, aths))
    if len(found) != 1:
        raise ValueError(t('core.athlete_streams', n=len(found)))
    return found[0]


def gold_fields(data):
    """Return where gold is stored as [(position in file, value), ...]. The first entry is the real value (TodayData.Gold),
        the rest are the previews shown in the save slot list (SavePreview.Gold)."""
    r = Reader(data)
    today, preview = [], []
    while r.p < len(data):
        for o in Parser(data, r).parse().objs.values():
            if o.get('class') == 'TodayData':
                today.append(o)
            elif o.get('class') == 'SavePreview':
                preview.append(o)
    if len(today) != 1:
        raise ValueError(t('core.gold_objects', n=len(today)))
    return [(o['offs']['Gold'], o['vals']['Gold'][2]) for o in today + preview]


def save_dir_candidates():
    """Candidate save folders. On Windows it is LocalLow; on Linux the same structure sits inside Proton's virtual Windows folder."""
    if sys.platform == "win32":
        return [Path(os.environ.get("USERPROFILE") or Path.home()).joinpath(*SAVE_SUBPATH)]
    rel = Path("pfx/drive_c/users/steamuser").joinpath(*SAVE_SUBPATH)
    cands = [Path.home() / "SteamCompatData" / APP_ID / rel]     # when compatdata is kept elsewhere under the home folder (STEAM_COMPAT_DATA_PATH)
    cands += [lib / "steamapps" / "compatdata" / APP_ID / rel for lib in steam.steam_libraries()]
    return cands


def default_save_dir():
    """The TFM_SAVE_DIR environment variable, else the first candidate that exists, else the first candidate (a path to show in the error message)."""
    if os.environ.get("TFM_SAVE_DIR"):
        return Path(os.environ["TFM_SAVE_DIR"])
    cands = save_dir_candidates()
    return next((c for c in cands if c.is_dir()), cands[0])


SAVE_DIR = default_save_dir()


def version():
    """The version is written in one place only, pyproject.toml, so read it from there (the executable bundles that file). None if it cannot be read."""
    try:
        text = (RESOURCE_DIR / "pyproject.toml").read_text(encoding="utf-8")
        return re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)
    except (OSError, AttributeError):
        return None


GAME_EXE = "Teamfight Manager.exe"


def game_running():
    """Whether the game is running. On Windows it uses tasklist; on Linux /proc (the command lines of Proton processes)."""
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + GAME_EXE, "/NH", "/FO", "CSV"],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return GAME_EXE.lower() in out.lower()
    me = {os.getpid(), os.getppid()}
    for p in Path('/proc').iterdir():
        if p.name.isdigit() and int(p.name) not in me:
            try:
                if GAME_EXE.encode() in (p / 'cmdline').read_bytes():
                    return True
            except OSError:
                pass
    return False


SPECIAL_TEAMS = {'team.name.null': 'core.team_none', 'team.name.tbd': 'core.team_tbd'}


class Save:
    def __init__(self, path):
        self.path = path
        self.data = path.read_bytes()
        self.ps, self.teams, self.aths = analyze(self.data)
        self.gold_slots = gold_fields(self.data)
        self.gold = self.gold_slots[0][1]
        try:
            names = json.loads(NAMES_FILE.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            names = {}
        self.player_names = names.get('players', [])
        self.team_names = names.get('teams', {})

    def text(self, v):
        rid = ref_id(v) if v else None
        o = self.ps.objs.get(rid) if rid is not None else None
        return o.get('str') if o else None

    def team_label(self, tid):
        if tid is None or tid not in self.teams:
            return t('core.no_team')
        key = self.text(self.teams[tid]['vals'].get('NameKey')) or '?'
        if key in SPECIAL_TEAMS:                 # names.json holds these two in Korean only, so they come from the language catalog
            return t(SPECIAL_TEAMS[key])
        return self.team_names.get(key, key)

    def athlete_label(self, aid):
        s = self.text(self.aths[aid]['vals'].get('Name')) or '?'
        if s.startswith('`') and s[1:].isdigit() and int(s[1:]) < len(self.player_names):
            return self.player_names[int(s[1:])]
        return s

    def traits(self, aid):
        """The athlete's list of trait keys. An empty slot is 'null'."""
        v = self.aths[aid]['vals'].get('Properties')
        lst = self.ps.objs.get(ref_id(v)) if v else None
        items = self.ps.objs.get(ref_id(lst['vals']['_items'])) if lst and 'vals' in lst else None
        out = []
        for it in (items or {}).get('items', []):
            if it[0] == 'null':
                out.extend(['null'] * it[1])
                continue
            o = self.ps.objs.get(ref_id(it))
            nm = next(iter(o['vals'].values())) if o and o.get('vals') else None
            out.append(self.text(nm) or '?')
        return out[:lst['vals']['_size'][2]]    # _items has spare slots, so only use up to _size

    def rows(self):
        out = []
        for aid, a in self.aths.items():
            v = a['vals']
            tid = ref_id(v['Team']) if v.get('Team') else None
            mine = tid in self.teams and self.teams[tid]['vals'].get('UseKey', (0, 0, False))[2] is True
            out.append({'id': aid, 'name': self.athlete_label(aid), 'team': self.team_label(tid), 'mine': mine,
                        'age': v['Age'][2], 'stars': v['Potential'][2], 'salary': v['Salary'][2]})
        out.sort(key=lambda r: (not r['mine'], r['team'], -r['stars'], r['name'].casefold()))
        return out


def list_saves():
    """List of slot_*.tfm files, oldest save time first."""
    return sorted(SAVE_DIR.glob('slot_*.tfm'), key=lambda p: p.stat().st_mtime)


class EditError(Exception):
    """The reason the save could not be changed (a message shown to the user as is)."""


def ensure_game_closed():
    if game_running():
        raise EditError(t('core.game_running'))


def make_backup(path):
    BACKUP_DIR.mkdir(exist_ok=True)
    ts, n = time.strftime('%Y%m%d-%H%M%S'), 0
    backup = BACKUP_DIR / ("%s.%s.bak" % (path.name, ts))
    while backup.exists():                       # if another backup is made within the same second, add a number so the earlier one is not overwritten
        n += 1
        backup = BACKUP_DIR / ("%s.%s-%d.bak" % (path.name, ts, n))
    shutil.copyfile(path, backup)
    return backup


# ---- Backup pruning: keep only the oldest one (the original) and the newest KEEP_NEWEST ----
KEEP_NEWEST = 5
_pruned = []


def _stamp_key(p):
    """Build a sort key from the time in the backup file name (and the number for the same second)."""
    m = re.search(r"(\d{8}-\d{6})(?:-(\d+))?", p.name)
    return (m.group(1), int(m.group(2) or 0), p.name) if m else ("", 0, p.name)


def prune(files, keep_newest=KEEP_NEWEST, keep_oldest=1):
    """Keep only the oldest keep_oldest and the newest keep_newest of the backup list and delete the rest. Return the deleted files."""
    files = sorted(files, key=_stamp_key)
    keep = set(files[:keep_oldest]) | (set(files[-keep_newest:]) if keep_newest else set())
    removed = []
    for p in files:
        if p not in keep:
            try:
                p.unlink()
            except OSError:
                continue
            removed.append(p)
    return removed


def take_pruned():
    """Return and clear the names of the backups pruned by recent work (so the screen can report them)."""
    out = list(_pruned)
    _pruned.clear()
    return out


def prune_backups(path):
    """For this save, keep only the oldest 1 and the newest 5 backups (.bak) and the newest 5 pre-restore copies (.keep)."""
    removed = prune(list_backups(path)) + prune(BACKUP_DIR.glob(path.name + '.*.bak.keep'), keep_oldest=0)
    _pruned.extend(p.name for p in removed)
    return removed


def check_values(stars=None, age=None):
    """Validate the values to change and return {save field name: value}. Items that are None are not changed."""
    new = {}
    if stars is not None:
        if not 0 <= stars <= MAX_STARS:
            raise EditError(t('core.stars_range', max=MAX_STARS))
        new['Potential'] = stars
    if age is not None:
        if not MIN_AGE <= age <= MAX_AGE:
            raise EditError(t('core.age_range', min=MIN_AGE, max=MAX_AGE))
        new['Age'] = age
    if not new:
        raise EditError(t('core.nothing_to_change'))
    return new


def count_changes(sv, targets, stars=None, age=None):
    """Number of athletes whose values actually change. If 0, no backup or write is needed."""
    new = check_values(stars, age)
    return sum(1 for r in targets if any(sv.aths[r['id']]['vals'][f][2] != v for f, v in new.items()))


def write_fields(sv, targets, stars=None, age=None):
    """Write the targets' stars (Potential) and age (Age) into the save. Items that are None are left as they are.
        Return the number of athletes changed. If verification fails the save is not changed."""
    new = check_values(stars, age)
    out = bytearray(sv.data)
    offs, ids = [], set()
    for r in targets:
        for field, value in new.items():
            off = sv.aths[r['id']]['offs'][field]
            if struct.unpack_from('<i', out, off)[0] != value:
                struct.pack_into('<i', out, off, value)
                offs.append(off)
                ids.add(r['id'])
    if not offs:
        return 0                                 # every value is already the same, so the file is not rewritten

    # Verify: re-parse; targets must have the new values, everything else unchanged, and changed bytes only inside the written positions.
    _ps, _teams, aths2 = analyze(bytes(out))
    want = {r['id'] for r in targets}
    assert set(aths2) == set(sv.aths)
    for i, a in sv.aths.items():
        for field in ('Potential', 'Age'):
            exp = new[field] if i in want and field in new else a['vals'][field][2]
            assert aths2[i]['vals'][field][2] == exp, t('core.verify_athlete', id=i, field=field)
    diff = {k for k in range(len(sv.data)) if sv.data[k] != out[k]}
    assert diff <= {o + b for o in offs for b in range(4)}, t('core.verify_unexpected_bytes')
    tmp = sv.path.with_name(sv.path.name + '.new')
    tmp.write_bytes(bytes(out))
    os.replace(tmp, sv.path)
    prune_backups(sv.path)
    return len(ids)


def check_gold(gold):
    if not 0 <= gold <= MAX_GOLD:
        raise EditError(t('core.gold_range', max=MAX_GOLD))


def gold_changes(sv, gold):
    """Whether gold actually changes (True if either the real value or a slot preview differs)."""
    check_gold(gold)
    return any(v != gold for _, v in sv.gold_slots)


def write_gold(sv, gold):
    """Write gold into the save. Change both the real value and the slot list previews.
        Return False if the value was already the same. If verification fails the save is not changed."""
    check_gold(gold)
    out = bytearray(sv.data)
    offs = [off for off, _ in sv.gold_slots]
    for off in offs:
        struct.pack_into('<i', out, off, gold)

    # Verify: re-parse; every gold value must be the target and changed bytes only inside those positions.
    after = gold_fields(bytes(out))
    assert [off for off, _ in after] == offs and all(v == gold for _, v in after), t('core.verify_gold')
    diff = {k for k in range(len(sv.data)) if sv.data[k] != out[k]}
    assert diff <= {o + b for o in offs for b in range(4)}, t('core.verify_unexpected_bytes')
    analyze(bytes(out))                      # athlete data must still read back as before
    if not diff:
        return False
    tmp = sv.path.with_name(sv.path.name + '.new')
    tmp.write_bytes(bytes(out))
    os.replace(tmp, sv.path)
    prune_backups(sv.path)
    return True


def list_backups(path):
    return sorted(BACKUP_DIR.glob(path.name + '.*.bak'))


def restore_backup(path, src):
    """Restore src (a backup of this save) as the save. Keep the pre-restore state separately and return that file."""
    if src not in list_backups(path):
        raise EditError(t('core.no_such_backup', name=src.name))
    ensure_game_closed()
    safety = BACKUP_DIR / ("%s.%s.before-restore.bak.keep" % (path.name, time.strftime('%Y%m%d-%H%M%S')))
    shutil.copyfile(path, safety)
    tmp = path.with_name(path.name + '.new')
    shutil.copyfile(src, tmp)
    os.replace(tmp, path)
    prune_backups(path)
    return safety
