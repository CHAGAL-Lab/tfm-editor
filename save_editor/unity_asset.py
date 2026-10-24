"""Replace the data of one object in a Unity serialized file (*.assets) with data of a different size. Standard library only.

It does only what is needed, without UnityPy. Only serialization version 21 (Unity 2019.4) is supported; anything else is rejected.

File layout (version 21):
  [16-byte header: metadata size, file size, version, data start position (all big-endian)][byte order 1 + reserved 3]
  [metadata: Unity version, platform, type tree, type list, object position table, script types, external files, reference types, user information]
  [data area: the object data (8-byte aligned)]
An entry of the object position table is 20 bytes (path_id 8, byte_start 4, byte_size 4, type_id 4) and byte_start is relative to the data start position.

When an object's size changes, the objects after it are shifted by the same amount (in 8-byte units) and only the position table's byte_size, the later objects' byte_start
and the file size in the header are fixed. The other bytes are left as they are. Putting it back at the same size gives a file identical to the original byte for byte.
"""
import mmap
import shutil
import struct
from pathlib import Path

from .core import EditError
from .i18n import t

SUPPORTED_VERSION = 21
HEADER_SIZE = 20                  # 16-byte header + 1 byte order + 3 reserved
ENTRY_SIZE = 20
ALIGN = 8
MAX_META = 64 << 20


class Entry:
    def __init__(self, path_id, byte_start, byte_size, type_id, pos):
        self.path_id, self.byte_start, self.byte_size, self.type_id = path_id, byte_start, byte_size, type_id
        self.pos = pos            # position in the file where this entry starts in the position table


class _Reader:
    """A small cursor for reading metadata (little-endian). ValueError if it goes out of range."""

    def __init__(self, buf, pos):
        self.buf, self.pos = buf, pos

    def _take(self, n):
        if n < 0 or self.pos + n > len(self.buf):
            raise ValueError(t('unity.short_meta'))
        p = self.pos
        self.pos += n
        return p

    def skip(self, n):
        self._take(n)

    def unpack(self, fmt):
        n = struct.calcsize(fmt)
        return struct.unpack_from(fmt, self.buf, self._take(n))[0]

    def i32(self):
        return self.unpack('<i')

    def cstring(self):
        end = self.buf.find(b'\0', self.pos)
        if end < 0:
            raise ValueError(t('unity.string_end'))
        s = bytes(self.buf[self.pos:end])
        self.pos = end + 1
        return s

    def align(self, n):
        self.pos += (-self.pos) % n


def _skip_type(r, enable_tree, is_ref):
    """Skip one type (version 21)."""
    class_id = r.i32()
    r.skip(1)                                  # is_stripped_type
    script_type_index = r.unpack('<h')
    if (is_ref and script_type_index >= 0) or class_id == 114:       # a MonoBehaviour carries a script hash
        r.skip(16)
    r.skip(16)                                 # type hash
    if enable_tree:
        node_count, string_size = r.i32(), r.i32()
        r.skip(node_count * 32 + string_size)  # 32-byte node (including an 8-byte reference hash for version 19 and up) + strings
        if is_ref:
            r.cstring(), r.cstring(), r.cstring()
        else:
            r.skip(4 * r.i32())                # type dependencies


class SerializedFile:
    def __init__(self, path):
        self.path = Path(path)
        with open(self.path, 'rb') as f:
            head = f.read(HEADER_SIZE)
            if len(head) < HEADER_SIZE:
                raise EditError(t('unity.not_unity', name=self.path.name))
            self.meta_size, self.file_size, self.version, self.data_offset = struct.unpack('>IIII', head[:16])
            self.endian = head[16]
            if self.version != SUPPORTED_VERSION or self.endian != 0:
                raise EditError(t('unity.unsupported', version=self.version, endian=self.endian, supported=SUPPORTED_VERSION))
            if not 0 < self.data_offset <= MAX_META or self.data_offset < HEADER_SIZE + self.meta_size:
                raise EditError(t('unity.bad_header'))
            if self.path.stat().st_size != self.file_size:
                raise EditError(t('unity.size_mismatch'))
            f.seek(0)
            self.head = bytearray(f.read(self.data_offset))        # header + the whole metadata (modified and written to the new file)
        try:
            self._parse()
        except (ValueError, struct.error) as e:
            raise EditError(t('unity.meta_unreadable', error=e))

    def _parse(self):
        r = _Reader(self.head, HEADER_SIZE)
        self.unity_version = r.cstring().decode('ascii', 'replace')
        r.skip(4)                                      # target platform
        enable_tree = bool(r.unpack('<B'))
        for _ in range(r.i32()):
            _skip_type(r, enable_tree, False)
        count = r.i32()
        if not 0 <= count < 5_000_000:
            raise ValueError(t('unity.bad_count', n=count))
        self.entries = []
        for _ in range(count):
            r.align(4)
            pos = r.pos
            path_id, byte_start, byte_size, type_id = struct.unpack_from('<qIIi', self.head, r._take(ENTRY_SIZE))
            self.entries.append(Entry(path_id, byte_start, byte_size, type_id, pos))
        for _ in range(r.i32()):                       # script types
            r.i32()
            r.align(4)
            r.skip(8)
        for _ in range(r.i32()):                       # external files
            r.cstring()
            r.skip(20)
            r.cstring()
        for _ in range(r.i32()):                       # reference types
            _skip_type(r, enable_tree, True)
        r.cstring()                                    # user information
        # The end position must match the metadata end the header states for the parser to have read it correctly (the data starts after that, 16-byte aligned).
        if r.pos != HEADER_SIZE + self.meta_size or r.pos + (-r.pos) % 16 != self.data_offset:
            raise ValueError(t('unity.meta_end', read=r.pos, header=HEADER_SIZE + self.meta_size))
        self._check_layout()

    def _check_layout(self):
        """Check that the objects sit in the data area 8-byte aligned and without overlapping."""
        data_size = self.file_size - self.data_offset
        end = 0
        ids = set()
        for e in sorted(self.entries, key=lambda x: x.byte_start):
            if e.path_id in ids:
                raise ValueError(t('unity.dup_path_id', id=e.path_id))
            ids.add(e.path_id)
            if e.byte_start % ALIGN or e.byte_start < end or e.byte_start + e.byte_size > data_size:
                raise ValueError(t('unity.bad_layout', id=e.path_id))
            end = e.byte_start + e.byte_size

    def entry(self, path_id):
        for e in self.entries:
            if e.path_id == path_id:
                return e
        raise EditError(t('unity.no_object', id=path_id))

    def entry_at(self, file_offset):
        """Find an object by its position in the file."""
        found = [e for e in self.entries if self.data_offset + e.byte_start == file_offset]
        if len(found) != 1:
            raise EditError(t('unity.objects_at', offset=file_offset, n=len(found)))
        return found[0]

    def read_object(self, entry):
        with open(self.path, 'rb') as f:
            f.seek(self.data_offset + entry.byte_start)
            return f.read(entry.byte_size)

    def replace_object(self, dst, path_id, new_data, progress=None):
        """Make in dst a file where the data of the path_id object is replaced with new_data. The original is not changed."""
        tell = progress or (lambda m: None)
        e = self.entry(path_id)
        old_abs = self.data_offset + e.byte_start
        old_end = old_abs + e.byte_size
        later = sorted((x for x in self.entries if x.byte_start > e.byte_start), key=lambda x: x.byte_start)
        next_old = self.data_offset + later[0].byte_start if later else self.file_size
        with open(self.path, 'rb') as f:
            f.seek(old_end)
            gap = f.read(next_old - old_end)
            f.seek(next_old)
            tail = f.read()
        if len(gap) >= ALIGN or any(gap):
            raise EditError(t('unity.gap'))
        pad = (-len(new_data)) % ALIGN if later else 0
        next_new = old_abs + len(new_data) + pad
        shift = next_new - next_old if later else (old_abs + len(new_data)) - old_end
        new_size = self.file_size + shift

        head = bytearray(self.head)
        struct.pack_into('>I', head, 4, new_size)
        struct.pack_into('<I', head, e.pos + 12, len(new_data))                       # byte_size
        for x in later:
            struct.pack_into('<I', head, x.pos + 8, x.byte_start + shift)             # byte_start

        tell(t('unity.building'))
        shutil.copyfile(self.path, dst)
        with open(dst, 'r+b') as f:
            f.write(head)
            f.seek(old_abs)
            f.write(new_data + b'\0' * pad + tail)
            f.truncate(new_size)
        tell(t('unity.verifying'))
        self._verify(dst, path_id, new_data, shift, head)

    def _verify(self, dst, path_id, new_data, shift, head):
        """Read dst again and check that only the given object has the new data and the other objects and the metadata are the same."""
        out = SerializedFile(dst)
        if len(out.entries) != len(self.entries) or out.head != head:
            raise EditError(t('unity.verify_meta'))
        with open(self.path, 'rb') as fa, open(dst, 'rb') as fb, \
                mmap.mmap(fa.fileno(), 0, access=mmap.ACCESS_READ) as ma, mmap.mmap(fb.fileno(), 0, access=mmap.ACCESS_READ) as mb:
            for e in self.entries:
                n = out.entry(e.path_id)
                a = ma[self.data_offset + e.byte_start:self.data_offset + e.byte_start + e.byte_size]
                b = mb[out.data_offset + n.byte_start:out.data_offset + n.byte_start + n.byte_size]
                if e.path_id == path_id:
                    ok = bytes(b) == new_data
                else:
                    ok = a == b and n.byte_start - e.byte_start in (0, shift)
                if not ok:
                    raise EditError(t('unity.verify_object', id=e.path_id))
