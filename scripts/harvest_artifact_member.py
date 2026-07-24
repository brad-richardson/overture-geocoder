#!/usr/bin/env python3
"""Extract one member from a huge remote ZIP using HTTP Range requests.

GitHub artifact zips are served from blob storage that honours Range, so we can
read the ZIP64 central directory and inflate a single member without pulling the
whole 63 GB archive.
"""
import io
import struct
import subprocess
import sys
import urllib.request
import zlib

ARTIFACT_ID = sys.argv[1] if len(sys.argv) > 1 else "8608473339"
WANTED_SUFFIX = sys.argv[2] if len(sys.argv) > 2 else "plan/plan.json"
REPO = "brad-richardson/overture-geocoder"


def signed_url() -> str:
    token = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True
    ).stdout.strip()
    api = f"https://api.github.com/repos/{REPO}/actions/artifacts/{ARTIFACT_ID}/zip"
    request = urllib.request.Request(api, headers={"Authorization": f"Bearer {token}"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise _Redirect(newurl)

    class _Redirect(Exception):
        def __init__(self, url):
            self.url = url

    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(request)
    except _Redirect as redirect:
        return redirect.url
    raise SystemExit("expected a redirect to blob storage")


URL = signed_url()


def get_range(start: int, length: int) -> bytes:
    request = urllib.request.Request(
        URL, headers={"Range": f"bytes={start}-{start + length - 1}"}
    )
    with urllib.request.urlopen(request) as response:
        return response.read()


def total_size() -> int:
    request = urllib.request.Request(URL, method="HEAD")
    with urllib.request.urlopen(request) as response:
        return int(response.headers["Content-Length"])


size = total_size()
print(f"archive bytes: {size:,}")

tail = get_range(max(0, size - 65_536), min(65_536, size))
locator = tail.rfind(b"PK\x06\x07")
if locator < 0:
    raise SystemExit("no ZIP64 end-of-central-directory locator found")
zip64_eocd_offset = struct.unpack("<Q", tail[locator + 8 : locator + 16])[0]

record = get_range(zip64_eocd_offset, 56)
if record[:4] != b"PK\x06\x06":
    raise SystemExit("bad ZIP64 EOCD signature")
entries, cd_size, cd_offset = struct.unpack("<QQQ", record[32:56])
print(f"central directory: {entries:,} entries, {cd_size:,} bytes @ {cd_offset:,}")

directory = get_range(cd_offset, cd_size)
position = 0
found = None
scanned = 0
while position < len(directory) - 4 and directory[position : position + 4] == b"PK\x01\x02":
    name_len, extra_len, comment_len = struct.unpack(
        "<HHH", directory[position + 28 : position + 34]
    )
    name = directory[position + 46 : position + 46 + name_len].decode(
        "utf-8", "replace"
    )
    method = struct.unpack("<H", directory[position + 10 : position + 12])[0]
    comp_size, uncomp_size = struct.unpack(
        "<II", directory[position + 20 : position + 28]
    )
    local_offset = struct.unpack("<I", directory[position + 42 : position + 46])[0]
    extra = directory[
        position + 46 + name_len : position + 46 + name_len + extra_len
    ]
    # ZIP64 extended information overrides any 0xFFFFFFFF placeholder.
    ptr = 0
    while ptr + 4 <= len(extra):
        tag, tag_size = struct.unpack("<HH", extra[ptr : ptr + 4])
        if tag == 0x0001:
            values = extra[ptr + 4 : ptr + 4 + tag_size]
            index = 0
            if uncomp_size == 0xFFFFFFFF:
                uncomp_size = struct.unpack("<Q", values[index : index + 8])[0]
                index += 8
            if comp_size == 0xFFFFFFFF:
                comp_size = struct.unpack("<Q", values[index : index + 8])[0]
                index += 8
            if local_offset == 0xFFFFFFFF:
                local_offset = struct.unpack("<Q", values[index : index + 8])[0]
                index += 8
        ptr += 4 + tag_size
    scanned += 1
    if name.endswith(WANTED_SUFFIX):
        found = (name, method, comp_size, uncomp_size, local_offset)
        break
    position += 46 + name_len + extra_len + comment_len

if not found:
    raise SystemExit(f"{WANTED_SUFFIX} not found after scanning {scanned:,} entries")

name, method, comp_size, uncomp_size, local_offset = found
print(
    f"found {name}: method={method} compressed={comp_size:,} "
    f"uncompressed={uncomp_size:,} @ {local_offset:,} (entry {scanned:,})"
)

header = get_range(local_offset, 30)
if header[:4] != b"PK\x03\x04":
    raise SystemExit("bad local file header signature")
h_name_len, h_extra_len = struct.unpack("<HH", header[26:30])
data_start = local_offset + 30 + h_name_len + h_extra_len
payload = get_range(data_start, comp_size)
if method == 0:
    content = payload
else:
    content = zlib.decompress(payload, -15)
print(f"inflated {len(content):,} bytes")
with open(sys.argv[3] if len(sys.argv) > 3 else "harvested.json", "wb") as handle:
    handle.write(content)
