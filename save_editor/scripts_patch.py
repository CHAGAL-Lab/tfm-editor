"""Scripts.dll (UBTFM+ 1.0) IL patch: two places for using 4-star and 5-star athletes safely.

1. MemberSlot.Init: replace the two loops that turn the 3 stars of an athlete list slot on and off with one loop that runs only as many times as the star list is long.
   The original code turns on Stars[i] up to the grade (Potential), so from 4 stars up it goes out of range at Stars[3].
   The new code calls SetActive(i < Potential) for every cell. 4 and 5 stars are shown as 3 stars.
2. Athlete.get_SalaryCoef: the salary coefficient is 1.6 only when the grade == 3 and 1.2 otherwise. Change == to >= so 4 and 5 stars are also 1.6.

Apart from the star patch there is one more optional patch (TRAIT_PATCH): make the squad screen (PropertyViewer.Init) draw at most 3 traits
so that the 4th trait (the hidden trait) does not show on screen.

Each patch is applied 'only when the original bytes match exactly'. Otherwise (a game/mod update) nothing is changed.
The file size and the bytes at other positions do not change.
"""
from .i18n import t

# The 100-byte star loop in MemberSlot.Init (file offset 0xB0491-0xB04F4).
#   i = 0; while (i < Stars.Count) { Stars[i].SetActive(i < athlete.Potential); i++; }
# The tokens below were taken as they are from the original code: Stars=0x04002291, athlete=0x0400229B, Potential=0x04000281,
# List.get_Item=0x0A00036B, List.get_Count=0x0A0003C4, GameObject.SetActive=0x0A000059.
_STAR_ORIG = bytes.fromhex(
    "1613042b19"                                                          # i = 0; br.s cond1
    "027b9122000411046f6b03000a176f5900000a1104175813041104"              # Stars[i].SetActive(true); i++
    "067b9b2200047b8102000432d8"                                          # cond1: i < Potential
    "067b9b2200047b81020004" "13052b19"                                   # j = Potential; br.s cond2
    "027b9122000411056f6b03000a166f5900000a1105175813051105"              # Stars[j].SetActive(false); j++
    "027b912200046fc403000a32d8")                                         # cond2: j < Stars.Count

_STARS = bytes.fromhex("027b91220004")                     # ldarg.0; ldfld Stars
_LDLOC_I = bytes.fromhex("1104")                           # ldloc.s i
_GET_ITEM = bytes.fromhex("6f6b03000a")                    # callvirt List.get_Item
_SET_ACTIVE = bytes.fromhex("6f5900000a")                  # callvirt GameObject.SetActive
_ATHLETE_POTENTIAL = bytes.fromhex("067b9b2200047b81020004")  # ldloc.0; ldfld athlete; ldfld Potential
_GET_COUNT = bytes.fromhex("6fc403000a")                   # callvirt List.get_Count
_CLT = bytes.fromhex("fe04")                               # clt: i < Potential -> 0/1


def _star_new():
    body = (_STARS + _LDLOC_I + _GET_ITEM +
            _LDLOC_I + _ATHLETE_POTENTIAL + _CLT + _SET_ACTIVE +
            _LDLOC_I + bytes.fromhex("1758") + bytes.fromhex("1304"))        # i++
    cond = _LDLOC_I + _STARS + _GET_COUNT
    code = (bytes.fromhex("16") + bytes.fromhex("1304") +                    # i = 0
            bytes([0x2b, len(body)]) + body + cond +                         # br.s cond
            bytes([0x32, (-(len(body) + len(cond) + 2)) & 0xff]))            # blt.s body
    assert len(code) <= len(_STAR_ORIG), "the new code is longer than the original"
    return code + b"\x00" * (len(_STAR_ORIG) - len(code))                    # the leftover bytes are nop


# Salary coefficient: ldarg.0; ldfld Potential; ldc.i4.3; beq.s(0x2E)  ->  bge.s(0x2F)
_SAL_ORIG = bytes.fromhex("027b81020004192e06229a99993f2a22cdcccc3f2a")
_SAL_NEW = _SAL_ORIG[:7] + b"\x2f" + _SAL_ORIG[8:]

PATCHES = [
    ("MemberSlot.Init star display", 0xB0491, _STAR_ORIG, _star_new()),
    ("Athlete.get_SalaryCoef salary coefficient", 0x2162B, _SAL_ORIG, _SAL_NEW),
]


def state(data):
    """'original' (all original), 'patched' (all applied), 'partial' (only partly applied), 'unknown' (contains unknown bytes)."""
    kinds = []
    for _name, off, orig, new in PATCHES:
        cur = data[off:off + len(orig)]
        kinds.append("original" if cur == orig else "patched" if cur == new else "unknown")
    if "unknown" in kinds:
        return "unknown"
    if all(k == "original" for k in kinds):
        return "original"
    if all(k == "patched" for k in kinds):
        return "patched"
    return "partial"


def apply(data):
    """Return the patched bytes. Places already applied are left as they are. ValueError if state() is 'unknown'."""
    if state(data) == "unknown":
        raise ValueError(t('patch.unknown_bytes'))
    out = bytearray(data)
    for _name, off, orig, new in PATCHES:
        out[off:off + len(orig)] = new
    assert len(out) == len(data)
    return bytes(out)


# ---- Optional patch: limit the trait display on the screens to 3 slots ----
# The screens below loop over an athlete's whole trait list while creating slots.
#   AthleteBrief.Init    : squad screen (called by MemberDetail.Open)
#   PropertyViewer.Init  : recruit screen (called by DraftSlot and DraftSelectSlot)
#   MatchSelectSlot.Init : athlete cards and hover descriptions on the selection and ban/pick screen
#   MatchAthleteSlot.Init: athlete slots on the match screen
# Adding "skip if PropertySlots.Count >= 3" before creating a slot takes 14 bytes, and there is no room because the next method starts right after.
# A List enumerator's Dispose does nothing, so the trait loop's try/finally (16 bytes) is removed to make room.
#   - AthleteBrief.Init: delete the exception table entry of the removed try/finally (the last entry) and shrink the table. The code is rewritten at the same length and
#     the 2 leftover bytes are nop, so the code after it does not move.
#   - PropertyViewer.Init: it has only two try/finally blocks, so both are removed and the MoreSects flag (0x08) in the method header is cleared so no exception table is used.
# Every token the new code uses was taken from the original code (List<GameObject>.get_Count = 0x0A0003C4 is the same as in the star patch).
# Each span is changed only when it is exactly the original.
_TRAIT_ORIG = bytes.fromhex(
    "1b300300a400000044040011027b341b00046f7200000a0a2b0c1200287300000a285700000a1200287400000a2debde0e1200fe163000001b6f0100000adc027b341b00046f7500000a037b860200046f4b01000a0b2b3e1201284c01000a0c027b321b0004280f00002b0d096ff702002b086fab280006096f6b00000a027b331b0004166f7603000a027b341b0004096f7100000a1201285001000a2db9de0e1201fe167200001b6f0100000adc2a")
_BRIEF_LOOP = bytes.fromhex(
    "037b860200046f4b01000a13042b441204284c01000a1305027b2e210004280f00002b130611066ff702002b11056fab28000611066f6b00000a027b30210004166f7603000a027b3f21000411066f7100000a1204285001000a2db3de0e1204fe167200001b6f0100000adc")
_BRIEF_EH = bytes.fromhex(
    "0140000002003600194f000e00000000020069001982000e0000000002009c0019b5000e000000000200fd004d4a010e000000000200720151c3010e00000000")
_GET_COUNT_3 = bytes.fromhex("6fc403000a" "19")                # callvirt List<GameObject>.get_Count; ldc.i4.3


def _viewer_new():
    hdr, code = _TRAIT_ORIG[:12], _TRAIT_ORIG[12:]
    a, b1, body, cond = code[0:35], code[51:74], code[76:138], code[138:145]
    skip = bytes.fromhex("027b341b0004") + _GET_COUNT_3          # ldarg.0; ldfld PropertySlots; get_Count; ldc.i4.3
    body_at = len(a) + len(b1) + 2
    cond_at = body_at + len(skip) + 2 + len(body)
    new = (a + b1 + bytes([0x2b, cond_at - body_at])                          # br.s cond
           + skip + bytes([0x2f, cond_at - (body_at + len(skip) + 2)])        # bge.s cond (already 3 slots)
           + body + cond)
    new += bytes([0x2d, (body_at - (len(new) + 2)) & 0xff, 0x2a])             # brtrue.s body; ret
    assert len(new) <= len(code), "the new code is longer than the original"
    new += b"\x00" * (len(code) - len(new))
    return bytes([hdr[0] & ~0x08]) + hdr[1:] + new


def _brief_loop_new():
    o = _BRIEF_LOOP
    head, body, cond = o[0:13], o[15:83], o[83:90]              # ... stloc.s 4 | loop body | ldloca.s 4; call MoveNext
    skip = bytes.fromhex("027b3f210004") + _GET_COUNT_3          # ldarg.0; ldfld PropertySlots; get_Count; ldc.i4.3
    body_at = len(head) + 2
    cond_at = body_at + len(skip) + 2 + len(body)
    new = (head + bytes([0x2b, cond_at - body_at])
           + skip + bytes([0x2f, cond_at - (body_at + len(skip) + 2)])
           + body + cond)
    new += bytes([0x2d, (body_at - (len(new) + 2)) & 0xff])
    assert len(new) <= len(o), "the new code is longer than the original"
    return new + b"\x00" * (len(o) - len(new))


def _brief_eh_new():
    # Small-format exception table: [01, size, 0, 0] + 12-byte entries. Delete the last entry (the trait loop).
    assert _BRIEF_EH[0] == 0x01 and _BRIEF_EH[1] == len(_BRIEF_EH)
    n = _BRIEF_EH[1] - 12
    return bytes([0x01, n]) + _BRIEF_EH[2:n] + b"\x00" * (len(_BRIEF_EH) - n)


TRAIT_PATCHES = [
    ("PropertyViewer.Init draft screen, 3 traits", 0x87C30, _TRAIT_ORIG, _viewer_new()),
    ("AthleteBrief.Init squad screen, 3 traits", 0xAAEE9, _BRIEF_LOOP, _brief_loop_new()),
    ("AthleteBrief.Init exception table", 0xAB08C, _BRIEF_EH, _brief_eh_new()),
]



# Methods that have the same kind of trait loop: (name, loop file offset, original bytes, PropertySlots field token, exception table offset, original exception table, index of the entry to delete)
_LOOP_METHODS = [
    ('MatchSelectSlot.Init', 0x876F6,
     bytes.fromhex(
         '2b441206284c01000a1307027b091b0004280f00002b130811086ff702002b11076fab28000611086f6b00000a027b0a1b0004166f7603000a027b0b1b000411086f7100000a1206285001000a2db3de0e1206fe167200001b6f0100000adc'),
     bytes.fromhex('0b1b0004'), 0x87864,
     bytes.fromhex(
         '0134000002002a001943000e000000000200be007836010e000000000200560151a7010e00000000020014025569020e00000000'), 2),
    ('MatchAthleteSlot.Init', 0x85253,
     bytes.fromhex(
         '2b441206284c01000a1307027b741a0004280f00002b130811086ff702002b11076fab28000611086f6b00000a027b751a0004166f7603000a027b761a000411086f7100000a1206285001000a2db3de0e1206fe167200001b6f0100000adc'),
     bytes.fromhex('761a0004'), 0x852B4,
     bytes.fromhex(
         '0128000002006a0164ce010e000000000200e8011901020e00000000020027025178020e00000000'), 2),
]


def _loop_new(orig, slots_tok):
    """Rewrite the trait loop [br.s cond | body | cond | brtrue.s | leave.s | 14-byte finally] without try/finally and put
        "skip if PropertySlots.Count >= 3" in front of the body. The length stays the same and the 2 leftover bytes are nop."""
    rel = orig[1]
    body, cond = orig[2:2 + rel], orig[2 + rel:2 + rel + 7]
    assert orig[0] == 0x2b and len(orig) == rel + 27 and orig[2 + rel + 7] == 0x2d and orig[2 + rel + 9:2 + rel + 11] == bytes.fromhex("de0e")
    skip = b"\x02\x7b" + slots_tok + _GET_COUNT_3                  # ldarg.0; ldfld PropertySlots; get_Count; ldc.i4.3
    cond_at = 2 + len(skip) + 2 + len(body)
    new = (bytes([0x2b, cond_at - 2]) + skip + bytes([0x2f, cond_at - (2 + len(skip) + 2)]) + body + cond)
    new += bytes([0x2d, (2 - (len(new) + 2)) & 0xff])
    assert len(new) + 2 == len(orig)
    return new + b"\x00\x00"


def _eh_without(eh, k):
    """Delete the k-th entry of a small-format exception table (later entries move up and the leftover space is 0)."""
    n = eh[1] - 12
    assert eh[0] == 0x01 and eh[1] == len(eh) and k < (len(eh) - 4) // 12
    body = eh[4:4 + k * 12] + eh[4 + (k + 1) * 12:]
    return bytes([0x01, n, 0, 0]) + body + b"\x00" * (len(eh) - n)


for _name, _off, _orig, _tok, _eh_off, _eh, _k in _LOOP_METHODS:
    TRAIT_PATCHES.append((_name + " 3 traits", _off, _orig, _loop_new(_orig, _tok)))
    TRAIT_PATCHES.append((_name + " exception table", _eh_off, _eh, _eh_without(_eh, _k)))


def trait_state(data):
    """'original' (all original), 'patched' (all applied), 'partial' (only partly applied), 'unknown' (contains unknown bytes)."""
    kinds = []
    for _name, off, orig, new in TRAIT_PATCHES:
        cur = data[off:off + len(orig)]
        kinds.append("original" if cur == orig else "patched" if cur == new else "unknown")
    if "unknown" in kinds:
        return "unknown"
    if all(k == "original" for k in kinds):
        return "original"
    return "patched" if all(k == "patched" for k in kinds) else "partial"


def set_trait_limit(data, on):
    """Return the bytes patched if on, otherwise restored to the original. ValueError if the state is unknown."""
    if trait_state(data) == "unknown":
        raise ValueError(t('patch.unknown_bytes'))
    out = bytearray(data)
    for _name, off, orig, new in TRAIT_PATCHES:
        out[off:off + len(orig)] = new if on else orig
    assert len(out) == len(data)
    return bytes(out)
