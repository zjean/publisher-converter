"""Group libmspub debug-trace block reads by shape.

Requires a libmspub built with --enable-debug; see actions.md. Emits one
record per shape: its sequence number and every (id, type, value) block
libmspub read while parsing it, including blocks it then ignored.
"""
import re, sys, json, collections

BLOCK = re.compile(r'parseBlock dataOffset (0x[0-9a-f]+), id (0x[0-9a-f]+), '
                   r'type (0x[0-9a-f]+), dataLength (0x[0-9a-f]+), integral data (0x[0-9a-f]+)')
SHAPE = re.compile(r'parseShape: seqNum (0x[0-9a-f]+)')
SHAPECHUNK = re.compile(r'shape chunk: offset (0x[0-9a-f]+), seqnum (0x[0-9a-f]+)')

def parse(path):
    shapes = collections.OrderedDict()
    current = None
    for line in open(path, errors='replace'):
        m = SHAPE.search(line)
        if m:
            current = int(m.group(1), 16)
            shapes.setdefault(current, [])
            continue
        m = BLOCK.search(line)
        if m and current is not None:
            off, bid, btype, blen, data = (int(g, 16) for g in m.groups())
            shapes[current].append({"offset": off, "id": bid, "type": btype,
                                    "len": blen, "data": data})
    return shapes

if __name__ == "__main__":
    shapes = parse(sys.argv[1])
    print(json.dumps({hex(k): v for k, v in shapes.items()}, indent=1))
