#!/usr/bin/env python3
"""
cobpack.py - repack and verify Ascendancy .COB archives.

The format here is inferred from daumiller/ascendancy's cob.py, not from a
spec, so use the `verify` command against your own archives first:

    <int32 LE>  entry count N
    N x 50 bytes  name records (null-terminated, null padded, backslash paths)
    N x uint32 LE  absolute offset of each entry's data
    file data, back to back (no compression, no stored sizes; an entry's size
    is the next entry's offset minus its own, the last runs to end of file)

Usage:
    python cobpack.py verify ORIGINAL.COB EXTRACTED_DIR
        Rebuild the archive from the extracted folder and compare it to the
        original, byte for byte. Run this first. It writes nothing.

    python cobpack.py pack EXTRACTED_DIR OUTPUT.COB --order ORIGINAL.COB
        Pack the folder, keeping the original entry order and name records.
        Files in the folder that are not in the original are appended at the
        end. Files missing from the folder are an error unless you pass
        --fill-missing (then the original's bytes are used).

    python cobpack.py pack EXTRACTED_DIR OUTPUT.COB
        Pack without an original; entries are sorted by path.

Refuses to overwrite an existing output file unless you pass --force, and
never writes over the --order archive.
"""
import argparse
import os
import struct
import sys

NAME_LEN = 50


def die(msg):
    sys.stderr.write("error: %s\n" % msg)
    sys.exit(1)


def read_cob(path):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 4:
        die("%s is too small to be a COB archive" % path)
    (count,) = struct.unpack_from("<i", data, 0)
    if count < 1:
        die("%s reports %d entries" % (path, count))
    offs_at = 4 + NAME_LEN * count
    if offs_at + 4 * count > len(data):
        die("%s: header would extend past the end of the file" % path)
    raw_names = [data[4 + i * NAME_LEN: 4 + (i + 1) * NAME_LEN] for i in range(count)]
    offsets = list(struct.unpack_from("<%dI" % count, data, offs_at))
    entries = []
    for i in range(count):
        start = offsets[i]
        end = offsets[i + 1] if i + 1 < count else len(data)
        if start > end or end > len(data):
            die("%s: entry %d has bad offsets (%d..%d)" % (path, i, start, end))
        name = raw_names[i].split(b"\0", 1)[0].decode("utf-8", errors="replace")
        entries.append((name, raw_names[i], data[start:end]))
    return entries, offsets, data


def make_name_record(name):
    b = name.encode("utf-8")
    if len(b) >= NAME_LEN:
        die("name too long for a COB record (max %d bytes): %s" % (NAME_LEN - 1, name))
    return b + b"\0" * (NAME_LEN - len(b))


def build_cob(items):
    """items: list of (50-byte name record, content bytes)."""
    n = len(items)
    pos = 4 + NAME_LEN * n + 4 * n
    offsets = []
    for _, content in items:
        offsets.append(pos)
        pos += len(content)
    out = bytearray(struct.pack("<i", n))
    for rec, _ in items:
        out += rec
    out += struct.pack("<%dI" % n, *offsets)
    for _, content in items:
        out += content
    return bytes(out)


def scan_dir(src):
    if not os.path.isdir(src):
        die("%s is not a folder" % src)
    found = {}
    for root, _dirs, files in os.walk(src):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            found[rel] = full
    return found


def items_from_dir(src, original_entries, fill_missing):
    disk = scan_dir(src)
    lower = {}
    for rel in disk:
        lower.setdefault(rel.lower(), rel)
    items, used, missing = [], set(), []
    for name, raw, orig_bytes in original_entries:
        key = name.replace("\\", "/")
        if key not in disk and key.lower() in lower:
            key = lower[key.lower()]
        if key in disk:
            with open(disk[key], "rb") as f:
                items.append((raw, f.read()))
            used.add(key)
        elif fill_missing:
            items.append((raw, orig_bytes))
            sys.stderr.write("note: %s not in folder, using original bytes\n" % name)
        else:
            missing.append(name)
    if missing:
        die("%d file(s) from the original archive are missing from the folder "
            "(first: %s). Use --fill-missing to take them from the original."
            % (len(missing), missing[0]))
    extras = sorted(k for k in disk if k not in used)
    for key in extras:
        with open(disk[key], "rb") as f:
            items.append((make_name_record(key.replace("/", "\\")), f.read()))
        sys.stderr.write("note: %s is not in the original, appended at the end\n" % key)
    return items


def cmd_verify(args):
    entries, _offsets, original = read_cob(args.original)
    items = items_from_dir(args.folder, entries, fill_missing=False)
    rebuilt = build_cob(items)
    if rebuilt == original:
        print("OK: rebuilt archive is byte-identical to %s (%d entries, %d bytes)."
              % (args.original, len(entries), len(original)))
        return
    print("MISMATCH: rebuilt %d bytes, original %d bytes." % (len(rebuilt), len(original)))
    for i, (a, b) in enumerate(zip(rebuilt, original)):
        if a != b:
            print("first difference at byte offset %d (0x%x)." % (i, i))
            break
    header = 4 + NAME_LEN * len(entries) + 4 * len(entries)
    first_off = struct.unpack_from("<I", original, 4 + NAME_LEN * len(entries))[0]
    if first_off != header:
        print("the original's first data offset is %d but a packed header is %d bytes; "
              "the original may have extra bytes between header and data." % (first_off, header))
    sys.exit(2)


def cmd_pack(args):
    out_abs = os.path.abspath(args.output)
    if args.order and os.path.abspath(args.order) == out_abs:
        die("output path is the same as the --order archive; pick a new name")
    if os.path.exists(args.output) and not args.force:
        die("%s already exists (use --force to overwrite)" % args.output)
    if args.order:
        entries, _o, _d = read_cob(args.order)
        items = items_from_dir(args.folder, entries, args.fill_missing)
    else:
        disk = scan_dir(args.folder)
        if not disk:
            die("no files found in %s" % args.folder)
        items = []
        for key in sorted(disk):
            with open(disk[key], "rb") as f:
                items.append((make_name_record(key.replace("/", "\\")), f.read()))
    data = build_cob(items)
    with open(args.output, "wb") as f:
        f.write(data)
    print("wrote %s: %d entries, %d bytes." % (args.output, len(items), len(data)))


def main():
    p = argparse.ArgumentParser(description="Pack and verify Ascendancy .COB archives.")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="rebuild from a folder and compare to the original")
    v.add_argument("original")
    v.add_argument("folder")
    v.set_defaults(func=cmd_verify)
    k = sub.add_parser("pack", help="pack a folder into a COB archive")
    k.add_argument("folder")
    k.add_argument("output")
    k.add_argument("--order", metavar="ORIGINAL.COB",
                   help="keep this archive's entry order and name records")
    k.add_argument("--fill-missing", action="store_true",
                   help="with --order, use the original's bytes for files missing from the folder")
    k.add_argument("--force", action="store_true", help="overwrite an existing output file")
    k.set_defaults(func=cmd_pack)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
