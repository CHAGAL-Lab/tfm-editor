"""Read athlete traits (AthleteProperty). Standard library only.

The trait list is not in the save but in PropertyConfig in the game file (sharedassets0.assets). So that traits show correctly even in a game
where they were modified, it is read from the game file instead of being hard-coded. It only reads and never changes the file.
PropertyConfig object: [int count][(name, category, float probability) x count]. A string is [int length][bytes] and is aligned to 4 bytes.
Display names come from the translation JSON in the same file (`"property": {"key": {"name", "desc"}}`). The game ships one translation per
language, so the names and descriptions follow the language the program is set to.
"""
import json
import mmap
import re
import struct

from . import game, i18n
from .i18n import t

CONFIG_MARK = b"\x0e\x00\x00\x00PropertyConfig\x00\x00"
EMPTY = "null"                  # name of an empty trait slot in the save
_NAME = re.compile(rb"^[a-z][a-z0-9_]*$")
_PUNCTUATION = "\u2018\u2019\u201c\u201d\u2013\u2014\u2026"      # typographic quotes and dashes that may appear in English text


def _lps(buf, p):
    n = struct.unpack_from("<i", buf, p)[0]
    if not 0 <= n < 64:
        raise ValueError(t('traits.bad_string'))
    return buf[p + 4:p + 4 + n], p + 4 + (n + 3) // 4 * 4


def parse_config(buf, start):
    """Read the list after the PropertyConfig head (start) as [(key, category, probability)]."""
    p = start + len(CONFIG_MARK)
    count = struct.unpack_from("<i", buf, p)[0]
    if not 0 < count < 500:
        raise ValueError(t('traits.bad_count'))
    p += 4
    out = []
    for _ in range(count):
        name, p = _lps(buf, p)
        cat, p = _lps(buf, p)
        prob = struct.unpack_from("<f", buf, p)[0]
        p += 4
        if not _NAME.match(name):
            raise ValueError(t('traits.bad_name'))
        out.append((name.decode(), cat.decode("ascii", "replace"), prob))
    return out


def _display_names(buf, lang):
    """From the "property" entry of the translation JSON, {key: (name, description)}, in the language lang ('ko' or 'en').
        The Korean translation is the one with Hangul names and the English one is the only one written in plain ASCII (the other
        languages have accented letters or other scripts). If that language is not found, the first translation is used."""
    best = {}
    for m in re.finditer(rb'"property"\s*:\s*\{', buf):
        depth, i = 0, m.end() - 1
        end = min(len(buf), i + 200000)
        while i < end:
            c = buf[i]
            depth += (c == 0x7B) - (c == 0x7D)
            i += 1
            if depth == 0:
                break
        try:
            obj = json.loads(buf[m.end() - 1:i].decode("utf-8"))
        except ValueError:
            continue
        names = {k: (v["name"], str(v.get("desc", ""))) for k, v in obj.items()
                 if isinstance(v, dict) and isinstance(v.get("name"), str)}
        if not names:
            continue
        if lang == 'ko' and any("가" <= ch <= "힣" for n, _ in names.values() for ch in n):
            return names
        if lang == 'en' and all(ch.isascii() or ch in _PUNCTUATION for n, d in names.values() for ch in n + d):
            return names
        best = best or names
    return best


def load_traits(game_dir=None, lang=None):
    """A list of [{'key', 'category', 'prob', 'name', 'desc'}] with the names in lang (default: the program's language).
        Empty if the game folder or the list is not found."""
    game_dir = game_dir or game.find_game_dir()
    if not game_dir:
        return []
    try:
        with open(game.asset_path(game_dir), "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
            # Other objects with the same name exist (with fewer entries), so use the longest list that reads without problems.
            items = []
            pos = buf.find(CONFIG_MARK)
            while pos != -1:
                try:
                    items = max(items, parse_config(buf, pos), key=len)
                except (ValueError, struct.error):
                    pass
                pos = buf.find(CONFIG_MARK, pos + 1)
            if not items:
                return []
            names = _display_names(buf, lang or i18n.language())
    except OSError:
        return []
    return [{"key": k, "category": c, "prob": p, "name": names.get(k, (k, ""))[0], "desc": names.get(k, (k, ""))[1]}
            for k, c, p in items]
