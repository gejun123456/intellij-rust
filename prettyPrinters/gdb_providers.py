from sys import version_info

import gdb
from gdb import lookup_type

if version_info[0] >= 3:
    xrange = range

ZERO_FIELD = "__0"
FIRST_FIELD = "__1"


def unwrap_unique_or_non_null(unique_or_nonnull):
    # BACKCOMPAT: rust 1.32 https://github.com/rust-lang/rust/commit/7a0911528058e87d22ea305695f4047572c5e067
    ptr = unique_or_nonnull["pointer"]
    return ptr if ptr.type.code == gdb.TYPE_CODE_PTR else ptr[ZERO_FIELD]


class StructProvider:
    def __init__(self, valobj):
        self.valobj = valobj
        self.fields = self.valobj.type.fields()

    def to_string(self):
        return self.valobj.type.name

    def children(self):
        for field in self.fields:
            yield (field.name, self.valobj[field.name])


class TupleProvider:
    def __init__(self, valobj):
        self.valobj = valobj
        self.fields = self.valobj.type.fields()

    def to_string(self):
        return "size={}".format(len(self.fields))

    def children(self):
        for i, field in enumerate(self.fields):
            yield ((str(i), self.valobj[field.name]))

    @staticmethod
    def display_hint():
        return "array"


class EnumProvider:
    def __init__(self, valobj):
        content = valobj[valobj.type.fields()[0]]
        fields = content.type.fields()
        self.empty = len(fields) == 0
        if not self.empty:
            if len(fields) == 1:
                discriminant = 0
            else:
                discriminant = int(content[fields[0]]) + 1
            self.active_variant = content[fields[discriminant]]
            self.name = fields[discriminant].name
            self.full_name = "{}::{}".format(valobj.type.name, self.name)
        else:
            self.full_name = valobj.type.name

    def to_string(self):
        return self.full_name

    def children(self):
        if not self.empty:
            yield (self.name, self.active_variant)


class StdStringProvider:
    def __init__(self, valobj):
        self.valobj = valobj
        vec = valobj["vec"]
        self.length = int(vec["len"])
        self.data_ptr = unwrap_unique_or_non_null(vec["buf"]["ptr"])

    def to_string(self):
        return self.data_ptr.lazy_string(encoding="utf-8", length=self.length)

    @staticmethod
    def display_hint():
        return "string"


class StdOsStringProvider:
    def __init__(self, valobj):
        self.valobj = valobj
        buf = self.valobj["inner"]["inner"]
        is_windows = "Wtf8Buf" in buf.type.name
        vec = buf[ZERO_FIELD] if is_windows else buf

        self.length = int(vec["len"])
        self.data_ptr = unwrap_unique_or_non_null(vec["buf"]["ptr"])

    def to_string(self):
        return self.data_ptr.lazy_string(encoding="utf-8", length=self.length)

    def display_hint(self):
        return "string"


class StdStrProvider:
    def __init__(self, valobj):
        self.valobj = valobj
        self.length = int(valobj["length"])
        self.data_ptr = valobj["data_ptr"]

    def to_string(self):
        return self.data_ptr.lazy_string(encoding="utf-8", length=self.length)

    @staticmethod
    def display_hint():
        return "string"


class StdVecProvider:
    def __init__(self, valobj):
        self.valobj = valobj
        self.length = int(valobj["len"])
        self.data_ptr = unwrap_unique_or_non_null(valobj["buf"]["ptr"])

    def to_string(self):
        return "size={}".format(self.length)

    def children(self):
        for index in xrange(self.length):
            yield ("[{}]".format(index), (self.data_ptr + index).dereference())

    @staticmethod
    def display_hint():
        return "array"


class StdVecDequeProvider:
    def __init__(self, valobj):
        self.valobj = valobj
        self.head = int(valobj["head"])
        self.tail = int(valobj["tail"])
        self.cap = int(valobj["buf"]["cap"])
        self.data_ptr = unwrap_unique_or_non_null(valobj["buf"]["ptr"])
        if self.head >= self.tail:
            self.size = self.head - self.tail
        else:
            self.size = self.cap + self.head - self.tail

    def to_string(self):
        return "size={}".format(self.size)

    def children(self):
        for index in xrange(0, self.size):
            yield ("[{}]".format(index), (self.data_ptr + ((self.tail + index) % self.cap)).dereference())

    @staticmethod
    def display_hint():
        return "array"


class StdRcProvider:
    def __init__(self, valobj, is_atomic=False):
        self.valobj = valobj
        self.ptr = unwrap_unique_or_non_null(valobj["ptr"])
        self.value = self.ptr["data" if is_atomic else "value"]
        self.strong = self.ptr["strong"]["v" if is_atomic else "value"]["value"]
        self.weak = self.ptr["weak"]["v" if is_atomic else "value"]["value"] - 1

    def to_string(self):
        return "strong={}, weak={}".format(int(self.strong), int(self.weak))

    def children(self):
        yield ("value", self.value)
        yield ("strong", self.strong)
        yield ("weak", self.weak)


class StdCellProvider:
    def __init__(self, valobj):
        self.value = valobj["value"]["value"]

    def children(self):
        yield ("value", self.value)


class StdRefProvider:
    def __init__(self, valobj):
        self.value = valobj["value"].dereference()
        self.borrow = valobj["borrow"]["borrow"]["value"]["value"]

    def to_string(self):
        borrow = int(self.borrow)
        return "borrow={}".format(borrow) if borrow >= 0 else "borrow_mut={}".format(-borrow)

    def children(self):
        yield ("*value", self.value)
        yield ("borrow", self.borrow)


class StdRefCellProvider:
    def __init__(self, valobj):
        self.value = valobj["value"]["value"]
        self.borrow = valobj["borrow"]["value"]["value"]

    def to_string(self):
        borrow = int(self.borrow)
        return "borrow={}".format(borrow) if borrow >= 0 else "borrow_mut={}".format(-borrow)

    def children(self):
        yield ("value", self.value)
        yield ("borrow", self.borrow)


# Yield each key (and optionally value) from a BoxedNode.
def children_of_node(boxed_node, height, want_values):
    def cast_to_internal(node):
        internal_type_name = str(node.type.target()).replace('LeafNode', 'InternalNode')
        internal_type = lookup_type(internal_type_name)
        return node.cast(internal_type.pointer())

    ptr = boxed_node['ptr']['pointer']
    node = ptr[ZERO_FIELD]
    node = cast_to_internal(node) if height > 0 else node
    leaf = node['data'] if height > 0 else node.dereference()
    keys = leaf['keys']['value']['value']
    values = leaf['vals']['value']['value']
    length = int(leaf['len'])

    for i in xrange(0, length + 1):
        if height > 0:
            for child in children_of_node(node['edges'][i], height - 1, want_values):
                yield child
        if i < length:
            if want_values:
                yield (keys[i], values[i])
            else:
                yield keys[i]


class StdBTreeSetProvider:
    def __init__(self, valobj):
        self.valobj = valobj

    def to_string(self):
        return "size=".format(self.valobj["map"]["length"])

    def children(self):
        root = self.valobj["map"]["root"]
        node_ptr = root["node"]
        for i, child in enumerate(children_of_node(node_ptr, root["height"], want_values=False)):
            yield ("[{}]".format(i), child)

    @staticmethod
    def display_hint():
        return "array"


class StdBTreeMapProvider:
    def __init__(self, valobj):
        self.valobj = valobj

    def to_string(self):
        return "size=".format(self.valobj["length"])

    def children(self):
        root = self.valobj["root"]
        node_ptr = root["node"]
        i = 0
        for child in children_of_node(node_ptr, root["height"], want_values=True):
            yield ("key{}".format(i), child[0])
            yield ("val{}".format(i), child[1])
            i = i + 1

    @staticmethod
    def display_hint():
        return "map"


# BACKCOMPAT: rust 1.35
class StdOldHashMapProvider:
    def __init__(self, valobj, show_values=True):
        self.valobj = valobj
        self.show_values = show_values

        self.table = self.valobj["table"]
        self.size = int(self.table["size"])
        self.hashes = self.table["hashes"]
        self.hash_uint_type = self.hashes.type
        self.hash_uint_size = self.hashes.type.sizeof
        self.modulo = 2 ** self.hash_uint_size
        self.data_ptr = self.hashes[ZERO_FIELD]["pointer"]

        self.capacity_mask = int(self.table["capacity_mask"])
        self.capacity = (self.capacity_mask + 1) % self.modulo

        marker = self.table["marker"].type
        self.pair_type = marker.template_argument(0)
        self.pair_type_size = self.pair_type.sizeof

        self.valid_indices = []
        for idx in range(self.capacity):
            data_ptr = self.data_ptr.cast(self.hash_uint_type.pointer())
            address = data_ptr + idx
            hash_uint = address.dereference()
            hash_ptr = hash_uint[ZERO_FIELD]["pointer"]
            if int(hash_ptr) != 0:
                self.valid_indices.append(idx)

    def to_string(self):
        return "size={}".format(self.size)

    def children(self):
        start = int(self.data_ptr) & ~1

        hashes = self.hash_uint_size * self.capacity
        align = self.pair_type_size
        len_rounded_up = (((((hashes + align) % self.modulo - 1) % self.modulo) & ~(
                (align - 1) % self.modulo)) % self.modulo - hashes) % self.modulo

        pairs_offset = hashes + len_rounded_up
        pairs_start = gdb.Value(start + pairs_offset).cast(self.pair_type.pointer())

        for index in range(self.size):
            table_index = self.valid_indices[index]
            idx = table_index & self.capacity_mask
            element = (pairs_start + idx).dereference()
            if self.show_values:
                yield ("key{}".format(index), element[ZERO_FIELD])
                yield ("val{}".format(index), element[FIRST_FIELD])
            else:
                yield ("[{}]".format(index), element[ZERO_FIELD])

    def display_hint(self):
        return "map" if self.show_values else "array"


class StdHashMapProvider:
    def __init__(self, valobj, show_values=True):
        self.valobj = valobj
        self.show_values = show_values

        table = self.valobj["base"]["table"]
        capacity = int(table["bucket_mask"]) + 1
        ctrl = table["ctrl"]["pointer"]

        self.size = int(table["items"])
        self.data_ptr = table["data"]["pointer"]
        self.pair_type = self.data_ptr.dereference().type

        self.valid_indices = []
        for idx in range(capacity):
            address = ctrl + idx
            value = address.dereference()
            is_presented = value & 128 == 0
            if is_presented:
                self.valid_indices.append(idx)

    def to_string(self):
        return "size={}".format(self.size)

    def children(self):
        pairs_start = self.data_ptr

        for index in range(self.size):
            idx = self.valid_indices[index]
            element = (pairs_start + idx).dereference()
            if self.show_values:
                yield ("key{}".format(index), element[ZERO_FIELD])
                yield ("val{}".format(index), element[FIRST_FIELD])
            else:
                yield ("[{}]".format(index), element[ZERO_FIELD])

    def display_hint(self):
        return "map" if self.show_values else "array"
