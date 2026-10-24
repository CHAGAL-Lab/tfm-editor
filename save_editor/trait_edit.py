"""Change athlete traits. Standard library only.

In the save, an athlete's trait slot (Athlete.Properties) points to one object (AthleteXxxProperty) each. The object's class decides the trait
(hero_mind -> AthleteHeroMindProperty), and inside is only the trait key string (Name). So the slot's object is rewritten as
the target trait's object. Object ids are kept so the references on the athlete side are not touched.

- The object being changed gets its own class definition (record 5) and name string in full. It depends on no other object.
- If that object was where the class was first defined and a later object uses the same class, the first such user takes over the definition
  and the remaining users just have their class id pointed at the new definition (a 4-byte replacement of the same length).
- The file length changes. A BinaryFormatter stream has no length table so this is possible, but it is verified by reading it again afterwards.
"""
import os
import re
import struct

from . import core
from .core import EditError
from .i18n import t

MEMBER = 'AthleteProperty+<Name>k__BackingField'
EMPTY = 'null'
MAX_SLOTS = 6                   # The game screen shows 3 slots; every slot after that is a hidden trait (up to 3).
_KEY = re.compile(r'^[a-z][a-z0-9_]*$')


def class_name(key):
    """Trait key -> class name (hero_mind -> AthleteHeroMindProperty). All 31 names in the save were checked to match."""
    return 'Athlete' + ''.join(w.capitalize() for w in key.split('_')) + 'Property'


def _lps(s):
    b = s.encode('utf-8')
    assert len(b) < 128
    return bytes([len(b)]) + b


def _definer(oid, cname, lib):
    """The head of a class definition record (5): one name string member. The value record follows separately."""
    return (b'\x05' + struct.pack('<i', oid) + _lps(cname) + struct.pack('<i', 1) + _lps(MEMBER)
            + b'\x01' + struct.pack('<i', lib))


class _View:
    """A save read from bytes (uses only the same read methods as Save)."""

    def __init__(self, data):
        self.data = data
        self.ps, self.teams, self.aths = core.analyze(data)

    text = core.Save.text
    traits = core.Save.traits


def _slot_ids(view, aid):
    v = view.aths[aid]['vals']['Properties']
    lst = view.ps.objs[core.ref_id(v)]
    arr = view.ps.objs[core.ref_id(lst['vals']['_items'])]
    items = arr['items'][:lst['vals']['_size'][2]]
    if any(it[0] not in ('ref', 'obj') for it in items):
        raise EditError(t('edit.slot_format'))
    return [it[1] for it in items]


def _next_id(ps):
    return max(max(ps.objs), max(ps.classes)) + 1


def _plan(view, apid, key):
    """The edits [(start, end, new bytes)] that rewrite object apid as trait key."""
    ps, d = view.ps, view.data
    o = ps.objs[apid]
    start, end = ps.spans[apid]
    cname, names, bts, _ = ps.classes[apid] if apid in ps.classes else ps.classes[
        struct.unpack_from('<i', d, start + 5)[0]]
    if d[start] not in (1, 5) or names != [MEMBER] or bts != [1]:
        raise EditError(t('edit.object_format', pos=start))
    libs = [lib for c, lib in ps.libs.items() if ps.classes[c][1] == [MEMBER]]
    if not libs:
        raise EditError(t('edit.no_library'))
    lib = libs[0]

    new_id = _next_id(ps)
    edits = []
    name_v = o['vals'][MEMBER]
    if name_v[0] == 'obj':
        sid = name_v[1]
        # If another place references this name string, write the string directly there (a string is a value, so whether it is shared does not matter).
        old_text = ps.objs[sid]['str']
        for rs, re_ in ps.refs.get(sid, ()):
            edits.append((rs, re_, b'\x06' + struct.pack('<i', new_id) + _lps(old_text)))
            new_id += 1
    else:
        sid, new_id = new_id, new_id + 1
    if d[start] == 5:
        users = sorted(ps.meta_use.get(apid, ()), key=lambda u: ps.spans[u[0]][0])
        if users:
            # This object first defined the class and later objects use that definition: hand the definition over to the first user.
            uoid, _ = users[0]
            us, ue = ps.spans[uoid]
            if d[us] != 1 or struct.unpack_from('<ii', d, us + 1) != (uoid, apid):
                raise EditError(t('edit.user_record'))
            head = _definer(apid, cname, lib)
            if d[start:start + len(head)] != head:
                raise EditError(t('edit.definition_record'))
            edits.append((us, us + 9, _definer(uoid, cname, lib)))
            edits.extend((at, at + 4, struct.pack('<i', uoid)) for _o, at in users[1:])
    new = _definer(apid, class_name(key), lib) + b'\x06' + struct.pack('<i', sid) + _lps(key)
    edits.append((start, end, new))
    return edits


def _nulls(n):
    """A record that stands for n empty slots."""
    if n == 0:
        return b''
    if n == 1:
        return b'\x0a'
    return b'\x0d' + bytes([n]) if n < 256 else b'\x0e' + struct.pack('<i', n)


def _plan_add(view, aid, key):
    """The edits that add trait key to the end of an athlete's trait list. Raise the count (_size) and, if the array has a spare slot, write there;
        otherwise grow the array length by one and append at the end."""
    ps = view.ps
    lst = ps.objs[core.ref_id(view.aths[aid]['vals']['Properties'])]
    arr = ps.objs[core.ref_id(lst['vals']['_items'])]
    size = lst['vals']['_size'][2]
    if size >= MAX_SLOTS:
        raise EditError(t('edit.max_slots', max=MAX_SLOTS))
    libs = [lib for c, lib in ps.libs.items() if ps.classes[c][1] == [MEMBER]]
    if not libs or arr.get('len_at') is None:
        raise EditError(t('edit.cannot_grow'))
    new_id = _next_id(ps)
    new = _definer(new_id, class_name(key), libs[0]) + b'\x06' + struct.pack('<i', new_id + 1) + _lps(key)
    edits = [(lst['offs']['_size'], lst['offs']['_size'] + 4, struct.pack('<i', size + 1))]
    cap = sum(it[1] if it[0] == 'null' else 1 for it in arr['items'])
    if cap > size:
        acc = 0
        for i, it in enumerate(arr['items']):
            if acc == size and it[0] == 'null':
                edits.append((arr['spans'][i], arr['spans'][i + 1], new + _nulls(it[1] - 1)))
                return edits
            acc += it[1] if it[0] == 'null' else 1
        raise EditError(t('edit.no_spare'))
    edits.append((arr['len_at'], arr['len_at'] + 4, struct.pack('<i', cap + 1)))
    edits.append((arr['spans'][-1], arr['spans'][-1], new))
    return edits


def _apply(data, edits):
    out, pos = bytearray(), 0
    for s, e, new in sorted(edits):
        if s < pos:
            raise EditError(t('edit.overlap'))
        out += data[pos:s] + new
        pos = e
    out += data[pos:]
    return bytes(out)


def check_key(key, allowed):
    if not _KEY.match(key) or (key != EMPTY and key not in allowed):
        raise EditError(t('edit.unknown_trait', key=key))


def _expected(sv, changes):
    exp = {aid: list(sv.traits(aid)) for aid in sv.aths}
    for aid, slot, key in changes:
        if aid not in exp or not 0 <= slot <= len(exp[aid]):
            raise EditError(t('edit.no_slot'))
        if slot == len(exp[aid]):                 # the slot after the last one = add a slot
            if key == EMPTY or slot >= MAX_SLOTS:
                raise EditError(t('edit.cannot_add'))
            exp[aid].append(key)
        else:
            exp[aid][slot] = key
    for aid in {c[0] for c in changes}:
        keys = [k for k in exp[aid] if k != EMPTY]
        if len(keys) != len(set(keys)):
            raise EditError(t('edit.duplicate'))
    return exp


def count_changes(sv, changes, allowed):
    """Number of slots that actually change."""
    for _a, _s, key in changes:
        check_key(key, allowed)
    exp = _expected(sv, changes)
    return sum(1 for aid in exp if exp[aid] != sv.traits(aid))


def rebuild(sv, changes, allowed):
    """The new bytes with changes = [(athlete id, slot number, trait key)] applied. EditError if verification fails."""
    for _a, _s, key in changes:
        check_key(key, allowed)
    exp = _expected(sv, changes)
    view = _View(sv.data)
    for aid, slot, key in changes:
        if slot == len(view.traits(aid)):
            edits = _plan_add(view, aid, key)
        elif view.traits(aid)[slot] == key:
            continue
        else:
            edits = _plan(view, _slot_ids(view, aid)[slot], key)
        view = _View(_apply(view.data, edits))
    _verify(sv, view, exp)
    return view.data


def _verify(sv, view, exp):
    ps = view.ps
    if set(view.aths) != set(sv.aths):
        raise EditError(t('edit.verify_athletes'))
    for aid in sv.aths:
        if view.traits(aid) != exp[aid]:
            raise EditError(t('edit.verify_traits', id=aid))
        for f, v in sv.aths[aid]['vals'].items():
            if v[0] == 'prim' and view.aths[aid]['vals'][f] != v:
                raise EditError(t('edit.verify_field', id=aid, field=f))
    if not set(sv.ps.objs) <= set(ps.objs):
        raise EditError(t('edit.verify_objects'))
    if any(r not in ps.objs for r in ps.refs):
        raise EditError(t('edit.verify_refs'))
    for oid, o in ps.objs.items():
        if o['class'].startswith('Athlete') and o['class'].endswith('Property') and o.get('vals'):
            if o['class'] != class_name(view.text(o['vals'][MEMBER])):
                raise EditError(t('edit.verify_class'))


def write_traits(sv, changes, allowed):
    """Change athletes' traits and write them to the save. Return the number of athletes changed. If verification fails the save is not changed."""
    if not count_changes(sv, changes, allowed):
        return 0
    data = rebuild(sv, changes, allowed)
    tmp = sv.path.with_name(sv.path.name + '.new')
    tmp.write_bytes(data)
    os.replace(tmp, sv.path)
    core.prune_backups(sv.path)
    exp = _expected(sv, changes)
    return sum(1 for aid in exp if exp[aid] != sv.traits(aid))
