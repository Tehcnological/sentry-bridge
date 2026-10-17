"""Whether a signed APK is exactly an unsigned build plus an APK Signing Block: the reproducible-build
check F-Droid makes before publishing the developer-signed APK. Exit 0 when they match.

Usage: python tools/apk_matches.py <unsigned.apk> <signed.apk>"""
import io
import struct
import sys
import zipfile


def parts(data):
    eocd = data.rfind(b"PK\x05\x06")
    if eocd < 0 or eocd + 22 > len(data):
        raise ValueError("Missing ZIP end record")
    disk, cd_disk, disk_entries, entries, cd_size, cd_offset, comment = struct.unpack_from("<4H2IH", data, eocd + 4)
    if (disk or cd_disk or disk_entries != entries or entries == 65535
            or cd_offset + cd_size != eocd or eocd + 22 + comment != len(data)):
        raise ValueError("Invalid or unsupported ZIP end record")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) != entries:
            raise ValueError("ZIP entry count differs from the central directory")
    entries_end = cd_offset
    if data[cd_offset - 16:cd_offset] == b"APK Sig Block 42":
        block_size = struct.unpack_from("<Q", data, cd_offset - 24)[0]
        entries_end = cd_offset - block_size - 8
        if block_size < 24 or entries_end < 0 or struct.unpack_from("<Q", data, entries_end)[0] != block_size:
            raise ValueError("Invalid APK signing block size")
    # Only this offset changes when the signing block is inserted. Preserve every other EOCD byte.
    tail = bytearray(data[eocd:])
    struct.pack_into("<I", tail, 16, entries_end)
    return data[:entries_end], data[cd_offset:eocd], tail, entries_end != cd_offset


def matches(unsigned, signed):
    try:
        ue, ucd, utail, usig = parts(unsigned)
        se, scd, stail, ssig = parts(signed)
    except (ValueError, struct.error, zipfile.BadZipFile):
        return False
    return not usig and ssig and ue == se and ucd == scd and utail == stail


if __name__ == "__main__":
    unsigned, signed = (open(p, "rb").read() for p in sys.argv[1:3])
    ok = matches(unsigned, signed)
    print("Reproducible: the signed APK is the unsigned build plus its signature." if ok else
          "Not reproducible: the APKs differ outside the signing block.")
    sys.exit(0 if ok else 1)
