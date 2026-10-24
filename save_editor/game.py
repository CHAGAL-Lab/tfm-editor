"""Advanced features: read and change game files (growth table = growth curve, Scripts.dll). Standard library only.

It changes files in the game install folder, not the save, so it affects every save. Changing growth table values and patching Scripts.dll
keep the file size, so they are done here; adding star grade rows to the growth table changes the file size, so the Unity serialized file is rewritten (unity_asset.py).

Game files are much bigger than saves (sharedassets0.assets is about 570MB). So the whole file is not backed up; only the growth table being changed
(a few hundred bytes) is backed up as JSON. Putting the growth table object back at the same size makes the file identical to the original byte for byte (checked with unity_asset tests),
so even after the row count has changed this backup alone can restore it. Every write goes to a temporary file and is verified before replacing.
Only the oldest backup (the original) and the newest 5 are kept; the rest are pruned automatically.
"""
import json
import mmap
import os
import re
import shutil
import struct
import time
from pathlib import Path

from . import core, scripts_patch, steam, unity_asset
from .core import EditError
from .i18n import t

STEAM_APP_ID = "1372810"
DATA_DIR_NAME = "Teamfight Manager_Data"
ASSET_NAME = "sharedassets0.assets"
GAME_BACKUP_DIR = core.ROOT / "backups" / "game"
SETTINGS_FILE = (core.ROOT if core.FROZEN else core.ROOT / "data") / "settings.json"

AGES = list(range(17, 27))     # 10 columns of the growth table: ages 17-26. Age 27 and up use the last column as is.
COLUMNS = len(AGES)
INT32 = (-2 ** 31, 2 ** 31 - 1)

# A row number in the growth table is the star count (grade). The original game has stars 0-3 (4 rows). When rows are added, 4 and 5 stars get the defaults below,
# and 6 stars and up continue the gap between the previous two rows (extend_rows).
# After adding, they can be changed in the growth curve editor. Up to 5 stars was checked in game; 6-9 was not.
MAX_GRADE_STARS = core.MAX_STARS
NEW_GRADE_DEFAULTS = {
    4: ([800, 900, 600, 500, 400, 300, 200, 100, 50, -1], [90, 80, 68, 56, 48, 40, 32, 24, 24, 24]),
    5: ([1000, 1100, 800, 700, 600, 450, 300, 200, 100, -1], [100, 90, 76, 64, 56, 48, 40, 32, 32, 32]),
}

def _step(prev, last):
    """The row that goes further than the last row by (last - prev). Negative cells (stat decline) are left as they are."""
    return [v if v < 0 else max(0, v + (v - p)) for p, v in zip(prev, last)]


def decline_mask(base):
    """A list of the same shape marking whether each cell is a stat decline (negative)."""
    return [[v < 0 for v in row] for row in base]


def set_decline(base, mask, on):
    """A new table with the cells marked in mask set to decline (-1, on) or no decline (0, off). Leaving no decline can make
        a veteran's next salary keep rising (when BaseGet is 0 or more the salary coefficient is 1.2; not checked directly)."""
    return [[(-1 if on else 0) if m else v for v, m in zip(row, mrow)] for row, mrow in zip(base, mask)]


def normalize_curve(base):
    """Return a new table tidied into one shape where a higher star is the same or better at every age. Decline (negative) cells are -1.
        - Keep each row's growth total (sum of the positive cells) but never let it drop as stars go up.
        - The age at which decline starts is the same or later as stars go up (no row declines earlier than a lower star).
        - Use one shape for all ages, the average of the rows from 3 stars up, rounded to 10. Cells of a higher star row that are smaller than the lower star row are raised."""
    n = len(base[0])
    onset, tot = [], []
    for i, row in enumerate(base):
        o = next((a for a, v in enumerate(row) if v < 0), n)
        t = sum(v for v in row if v > 0)
        onset.append(max(o, onset[-1]) if onset else o)
        tot.append(max(t, tot[-1]) if tot else t)
    refs = base[3:] or base
    shape = [0.0] * n
    for row in refs:
        t = sum(v for v in row if v > 0) or 1
        for a, v in enumerate(row):
            shape[a] += max(v, 0) / t / len(refs)
    out = []
    for i in range(len(base)):
        ages = range(onset[i])
        w = [shape[a] for a in ages]
        sw = sum(w) or 1.0
        row = [max(10, int(round(tot[i] * (shape[a] / sw) / 10.0)) * 10) if a < onset[i] else -1 for a in range(n)]
        if out:
            row = [max(v, pv) if v >= 0 and pv >= 0 else v for v, pv in zip(row, out[-1])]
        out.append(row)
    return out


def extend_rows(base, tpg, stars):
    """Return a new pair of lists with base/tpg (row lists) extended to stars stars (stars+1 rows).
        4 and 5 stars get the defaults; 6 stars and up continue the gap between the previous two rows (evenly)."""
    base, tpg = [list(r) for r in base], [list(r) for r in tpg]
    while len(base) <= stars:
        s = len(base)
        if s in NEW_GRADE_DEFAULTS:
            base.append(list(NEW_GRADE_DEFAULTS[s][0]))
            tpg.append(list(NEW_GRADE_DEFAULTS[s][1]))
        else:
            base.append(_step(base[-2], base[-1]))
            tpg.append(_step(tpg[-2], tpg[-1]))
    return base, tpg


FALLBACK_STARS = 3             # Star count assumed to be safe when the game folder is not found (what the original game supports)



def dll_state_text(state):
    """The text for a Scripts.dll star patch state: 'original', 'patched', 'partial' or 'unknown'."""
    return t('game.dll_' + state)


# ---- Game folder ----
def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_setting(key, value):
    """Remember one setting (the game folder chosen directly, the language) in settings.json."""
    settings = load_settings()
    settings[key] = value
    SETTINGS_FILE.parent.mkdir(exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding='utf-8')


def save_game_dir(path):
    """Remember the game folder chosen directly in the GUI/CLI."""
    save_setting('game_dir', str(path))


def asset_path(game_dir):
    return Path(game_dir) / DATA_DIR_NAME / ASSET_NAME


def dll_path(game_dir):
    return Path(game_dir) / DATA_DIR_NAME / "Managed" / "Scripts.dll"


def is_game_dir(p):
    return asset_path(p).is_file() and dll_path(p).is_file()


def find_game_dir():
    """Find the game folder: the TFM_GAME_DIR environment variable, the folder chosen directly, then Steam libraries, in that order. None if not found."""
    cands = []
    if os.environ.get("TFM_GAME_DIR"):
        cands.append(Path(os.environ["TFM_GAME_DIR"]))
    if load_settings().get('game_dir'):
        cands.append(Path(load_settings()['game_dir']))
    for lib in steam.steam_libraries():
        try:
            m = re.search(r'"installdir"\s+"([^"]+)"',
                          (lib / "steamapps" / ("appmanifest_%s.acf" % STEAM_APP_ID)).read_text(encoding='utf-8'))
        except OSError:
            continue
        if m:
            cands.append(lib / "steamapps" / "common" / m.group(1))
    return next((c for c in cands if is_game_dir(c)), None)


def require_game_dir(game_dir=None):
    game_dir = Path(game_dir) if game_dir else find_game_dir()
    if not game_dir or not is_game_dir(game_dir):
        raise EditError(t('game.no_game_dir'))
    return game_dir


# ---- Growth table (GrowthConfig) ----
# Object layout: [68-byte header (including the name)][BaseGet][TrainingPointGet]. Each table is [int row count][(int length=10, int x 10) x row count].
# BaseGet is the stat growth/decline by age (growth curve) and TrainingPointGet is the training experience coefficient. Both tables have the same row count.
GROWTH_NAME = b"GrowthConfig"
GROWTH_HEADER = 68


def parse_growth_at(buf, start):
    """Read GrowthConfig from buf at start and return (BaseGet row list, TrainingPointGet row list, end position). ValueError otherwise."""
    if start < 0 or struct.unpack_from('<i', buf, start + 28)[0] != len(GROWTH_NAME) \
            or bytes(buf[start + 32:start + 32 + len(GROWTH_NAME)]) != GROWTH_NAME:
        raise ValueError(t('game.bad_growth_head'))
    off, tables = start + GROWTH_HEADER, []
    for _ in range(2):
        n = struct.unpack_from('<i', buf, off)[0]
        if not 0 < n < 20:
            raise ValueError(t('game.bad_grade_rows'))
        off += 4
        rows = []
        for _r in range(n):
            if struct.unpack_from('<i', buf, off)[0] != COLUMNS:
                raise ValueError(t('game.bad_table_width', n=COLUMNS))
            off += 4
            rows.append(list(struct.unpack_from('<%di' % COLUMNS, buf, off)))
            off += 4 * COLUMNS
        tables.append(rows)
    if len(tables[0]) != len(tables[1]):
        raise ValueError(t('game.table_rows_differ'))
    return tables[0], tables[1], off


def pack_tables(base, tpg):
    if len(base) != len(tpg) or not all(len(r) == COLUMNS for r in base + tpg):
        raise ValueError(t('game.bad_table_shape'))
    out = b""
    for rows in (base, tpg):
        out += struct.pack('<i', len(rows))
        for r in rows:
            out += struct.pack('<i%di' % COLUMNS, COLUMNS, *r)
    return out


class Growth:
    def __init__(self, offset, base, tpg):
        self.offset = offset                       # position in the file where the GrowthConfig object starts
        self.base, self.tpg = base, tpg

    @property
    def tables_start(self):
        return self.offset + GROWTH_HEADER

    @property
    def tables_end(self):
        return self.tables_start + len(pack_tables(self.base, self.tpg))

    @property
    def max_stars(self):
        return len(self.base) - 1


def find_growth(asset):
    """Find GrowthConfig in the game asset file (there must be exactly one in the file)."""
    found = []
    with open(asset, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        i = m.find(GROWTH_NAME)
        while i != -1:
            try:
                base, tpg, _end = parse_growth_at(m, i - 32)
                found.append(Growth(i - 32, base, tpg))
            except (ValueError, struct.error):
                pass
            i = m.find(GROWTH_NAME, i + 1)
    if len(found) != 1:
        raise EditError(t('game.growth_count', n=len(found)))
    return found[0]


def check_rows(rows, count, label):
    if len(rows) != count:
        raise EditError(t('game.row_count', label=label, count=count, got=len(rows)))
    for i, r in enumerate(rows):
        if len(r) != COLUMNS or not all(isinstance(v, int) and INT32[0] <= v <= INT32[1] for v in r):
            raise EditError(t('game.row_values', label=label, row=i, n=COLUMNS))


# ---- Status ----
def game_info(game_dir=None):
    """The status shown by the advanced tab and the `game` command. found=False if the game folder is not found."""
    info = {'found': False, 'game_dir': None, 'max_stars': FALLBACK_STARS, 'grades': None,
            'dll_state': None, 'trait_limit': None, 'running': core.game_running(), 'error': None}
    try:
        game_dir = require_game_dir(game_dir)
        info['game_dir'] = game_dir
        g = find_growth(asset_path(game_dir))
        info.update(found=True, grades=len(g.base), max_stars=g.max_stars, growth=g)
        dll = dll_path(game_dir).read_bytes()
        info['dll_state'] = scripts_patch.state(dll)
        info['trait_limit'] = scripts_patch.trait_state(dll)
    except (EditError, OSError) as e:
        info['error'] = str(e)
    return info


def supported_stars(game_dir=None):
    """The maximum star count the game supports. If the game folder cannot be read, 3, what the original game supports."""
    return game_info(game_dir)['max_stars']


# ---- Backups ----
def stamped_path(name):
    """A new file path inside GAME_BACKUP_DIR. {ts} is replaced with the time, and a number is added if the name already exists."""
    GAME_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y%m%d-%H%M%S')
    p = GAME_BACKUP_DIR / name.format(ts=ts)
    n = 1
    while p.exists():
        p = GAME_BACKUP_DIR / name.format(ts="%s-%d" % (ts, n))
        n += 1
    return p


def backup_growth(asset, g):
    """Back up only the two growth tables as JSON (the whole file is not backed up)."""
    p = stamped_path("growth.{ts}.json")
    p.write_text(json.dumps({'asset': str(asset), 'offset': g.offset, 'size': Path(asset).stat().st_size,
                             'base': g.base, 'tpg': g.tpg}, indent=1) + "\n", encoding='utf-8')
    return p


def list_game_backups():
    if not GAME_BACKUP_DIR.is_dir():
        return []
    return sorted((p for p in GAME_BACKUP_DIR.iterdir() if re.fullmatch(r"growth\.[\d-]+\.json|Scripts\.dll\.[\d-]+\.bak", p.name)),
                  key=core._stamp_key)


def prune_game_backups():
    """Keep only the oldest 1 and the newest 5 growth table / Scripts.dll backups and the newest 5 pre-restore copies (.keep)."""
    if not GAME_BACKUP_DIR.is_dir():
        return []
    removed = (core.prune(GAME_BACKUP_DIR.glob("growth.*.json")) + core.prune(GAME_BACKUP_DIR.glob("Scripts.dll.*.bak")) +
               core.prune(GAME_BACKUP_DIR.glob("*.before-restore.keep"), keep_oldest=0))
    core._pruned.extend(p.name for p in removed)
    return removed


def take_pruned():
    """Return and clear the names of the backups pruned by recent work (so the screen can report them)."""
    return core.take_pruned()


# ---- Replacing files ----
def same_outside(a, b, start, end, chunk=1 << 24):
    """Whether two files are the same except for the [start, end) range."""
    if Path(a).stat().st_size != Path(b).stat().st_size:
        return False
    with open(a, 'rb') as fa, open(b, 'rb') as fb:
        pos = 0
        while True:
            x, y = fa.read(chunk), fb.read(chunk)
            if not x and not y:
                return True
            lo, hi = max(start - pos, 0), min(end - pos, len(x))
            if lo < hi:
                x, y = x[:lo] + x[hi:], y[:lo] + y[hi:]
            if x != y:
                return False
            pos += chunk


def replace_file(src, dst):
    """Copy src next to dst, check they are the same, then replace. If it fails midway, dst is untouched."""
    tmp = Path(dst).with_name(Path(dst).name + ".new")
    try:
        shutil.copyfile(src, tmp)
        if not _same_file(src, tmp):
            raise EditError(t('game.copy_differs'))
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def _same_file(a, b, chunk=1 << 24):
    return same_outside(a, b, 0, 0, chunk)


def write_growth(game_dir, base, tpg=None):
    """Change the growth table to base (and tpg). The row count must stay the same. Return the path of the backup made."""
    core.ensure_game_closed()
    game_dir = require_game_dir(game_dir)
    asset = asset_path(game_dir)
    g = find_growth(asset)
    tpg = g.tpg if tpg is None else tpg
    check_rows(base, len(g.base), "BaseGet")
    check_rows(tpg, len(g.base), "TrainingPointGet")
    if base == g.base and tpg == g.tpg:
        return None
    backup = backup_growth(asset, g)
    tmp = asset.with_name(asset.name + ".new")
    try:
        shutil.copyfile(asset, tmp)
        with open(tmp, 'r+b') as f:
            f.seek(g.tables_start)
            f.write(pack_tables(base, tpg))
        # Verify: only the growth table has the target values and every other byte is identical.
        g2 = find_growth(tmp)
        if (g2.offset, g2.base, g2.tpg) != (g.offset, base, tpg) or g2.tables_end != g.tables_end:
            raise EditError(t('game.verify_growth'))
        if not same_outside(asset, tmp, g.tables_start, g.tables_end):
            raise EditError(t('game.verify_outside'))
        os.replace(tmp, asset)
    finally:
        if tmp.exists():
            tmp.unlink()
    prune_game_backups()
    return backup


# ---- Scripts.dll ----
def patch_dll(game_dir):
    """Apply the Scripts.dll star safety patch (prevents the squad screen's star display from going out of range, and the salary coefficient).
        Return None if it was already applied, otherwise the path of the backup made."""
    core.ensure_game_closed()
    dll = dll_path(require_game_dir(game_dir))
    data = dll.read_bytes()
    state = scripts_patch.state(data)
    if state == "patched":
        return None
    if state == "unknown":
        raise EditError(t('game.dll_unexpected'))
    new = scripts_patch.apply(data)
    if scripts_patch.state(new) != "patched" or len(new) != len(data):
        raise EditError(t('game.dll_verify'))
    backup = stamped_path("Scripts.dll.{ts}.bak")
    shutil.copyfile(dll, backup)
    tmp = dll.with_name(dll.name + ".new")
    try:
        tmp.write_bytes(new)
        os.replace(tmp, dll)
    finally:
        if tmp.exists():
            tmp.unlink()
    prune_game_backups()
    return backup


def set_trait_limit(game_dir, on):
    """Make the squad and recruit screens draw at most 3 trait slots (on) or restore the original (off). Return None if already in that state, otherwise the path of the backup made.
        The file size stays the same and only the bytes of those methods change. It writes only when the bytes are exactly the known ones."""
    core.ensure_game_closed()
    dll = dll_path(require_game_dir(game_dir))
    data = dll.read_bytes()
    state = scripts_patch.trait_state(data)
    if state == "unknown":
        raise EditError(t('game.dll_unexpected'))
    if state == ("patched" if on else "original"):
        return None                                # if only partly applied (partial), finish applying the rest
    new = scripts_patch.set_trait_limit(data, on)
    if scripts_patch.trait_state(new) != ("patched" if on else "original") or len(new) != len(data):
        raise EditError(t('game.dll_verify'))
    backup = stamped_path("Scripts.dll.{ts}.bak")
    shutil.copyfile(dll, backup)
    tmp = dll.with_name(dll.name + ".new")
    try:
        tmp.write_bytes(new)
        os.replace(tmp, dll)
    finally:
        if tmp.exists():
            tmp.unlink()
    prune_game_backups()
    return backup


# ---- Changing the growth table row count (adding star grades, reverting) ----
def rebuild_growth(src, dst, g, base, tpg, progress=print):
    """Make in dst an asset in which the growth table (g) of the src asset is replaced with base/tpg. The row count may change so the object size may change.
        Only that object is replaced with unity_asset and the objects after it are shifted. src is not changed."""
    sf = unity_asset.SerializedFile(src)
    entry = sf.entry_at(g.offset)
    raw = sf.read_object(entry)
    cur_base, cur_tpg, end = parse_growth_at(raw, 0)
    if end != len(raw) or (cur_base, cur_tpg) != (g.base, g.tpg):
        raise EditError(t('game.growth_unexpected'))
    new = raw[:GROWTH_HEADER] + pack_tables(base, tpg)
    if parse_growth_at(new, 0)[:2] != (base, tpg):
        raise EditError(t('game.growth_build'))
    sf.replace_object(dst, entry.path_id, new, progress)
    g2 = find_growth(dst)
    if (g2.base, g2.tpg) != (base, tpg):
        raise EditError(t('game.verify_new_growth'))


def resize_growth(game_dir, base, tpg, progress=print):
    """Change the growth table to base/tpg. The row count may change. Back up the growth table before changing as JSON and return that path
        (None if the values were already the same). The game file is built in a temporary file and verified before replacing, so if it fails midway nothing changes."""
    core.ensure_game_closed()
    game_dir = require_game_dir(game_dir)
    asset = asset_path(game_dir)
    g = find_growth(asset)
    if len(base) != len(tpg) or not 1 <= len(base) < 20:
        raise EditError(t('game.growth_rows'))
    check_rows(base, len(base), "BaseGet")
    check_rows(tpg, len(tpg), "TrainingPointGet")
    if base == g.base and tpg == g.tpg:
        return None
    backup = backup_growth(asset, g)
    work = asset.with_name(asset.name + ".new")
    try:
        rebuild_growth(asset, work, g, base, tpg, progress)
        progress(t('game.replacing'))
        os.replace(work, asset)
    finally:
        if work.exists():
            work.unlink()
    prune_game_backups()
    return backup


def expand_stars(game_dir, stars, progress=print):
    """Raise the star count the game supports to stars (at most MAX_GRADE_STARS): patch Scripts.dll + add grade rows to the growth table.
        Lowering is not supported. Return the list of backup paths made (Scripts.dll backup, growth table backup)."""
    if not 1 <= stars <= MAX_GRADE_STARS:
        raise EditError(t('game.stars_target_range', max=MAX_GRADE_STARS))
    core.ensure_game_closed()
    game_dir = require_game_dir(game_dir)
    g = find_growth(asset_path(game_dir))
    if stars <= g.max_stars:
        raise EditError(t('game.stars_already', n=g.max_stars))
    base, tpg = extend_rows(g.base, g.tpg, stars)
    backups = []
    # Patch Scripts.dll first. The game still works with only this applied, and if the growth table step fails there is nothing to roll back.
    progress(t('game.patching_dll'))
    b = patch_dll(game_dir)
    if b:
        backups.append(b)
    progress(t('game.writing_growth'))
    backups.append(resize_growth(game_dir, base, tpg, progress))
    return backups


# ---- Restore ----
def restore_game_backup(game_dir, backup, progress=print):
    """Restore from a backup (an item of list_game_backups). A growth table backup is restored even if the row count changed (the growth table
        before restoring is also kept as a new backup). A Scripts.dll backup keeps the file before restoring as .keep and returns that path."""
    core.ensure_game_closed()
    game_dir = require_game_dir(game_dir)
    backup = Path(backup)
    if backup not in list_game_backups():
        raise EditError(t('game.not_backup', name=backup.name))
    if backup.suffix == ".json":
        data = json.loads(backup.read_text(encoding='utf-8'))
        g = find_growth(asset_path(game_dir))
        if g.offset != data['offset']:
            raise EditError(t('game.growth_moved'))
        if len(g.base) == len(data['base']):
            write_growth(game_dir, data['base'], data['tpg'])
        else:
            resize_growth(game_dir, data['base'], data['tpg'], progress)
        return None
    target = dll_path(game_dir)
    keep = stamped_path(target.name + ".{ts}.before-restore.keep")
    progress(t('game.keeping_before', name=keep.name))
    shutil.copyfile(target, keep)
    progress(t('game.restoring'))
    replace_file(backup, target)
    prune_game_backups()
    return keep
