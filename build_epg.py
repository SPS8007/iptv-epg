#!/usr/bin/env python3
import gzip
import io
import re
import shutil
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
M3U = ROOT / "playlist.m3u"
OUT = ROOT / "iptv_epg_all_six_filtered.xml"
TMP = ROOT / "iptv_epg_all_six_filtered.xml.tmp"

SOURCES = [
    ("AU", "https://iptv-epg.org/files/epg-au.xml.gz"),
    ("CA", "https://iptv-epg.org/files/epg-ca.xml.gz"),
    ("NZ", "https://iptv-epg.org/files/epg-nz.xml.gz"),
    ("ZA", "https://iptv-epg.org/files/epg-za.xml.gz"),
    ("GB", "https://iptv-epg.org/files/epg-gb.xml.gz"),
    ("US", "https://iptv-epg.org/files/epg-us.xml.gz"),
]

# Aliases used by the playlist/source feeds. We preserve the M3U tvg-id
# in the output, but allow known country-code variants to match.
SUFFIX_ALIASES = {
    ".ze": ".za",
    ".gb": ".uk",
}

def norm_id(value):
    value = (value or "").strip().lower()
    for a, b in SUFFIX_ALIASES.items():
        if value.endswith(a):
            value = value[:-len(a)] + b
    return value

def load_m3u_ids():
    text = M3U.read_text(encoding="utf-8", errors="ignore")
    ids = []
    seen = set()
    for m in re.finditer(r'tvg-id="([^"]*)"', text):
        tvg = m.group(1).strip()
        if tvg and tvg not in seen:
            seen.add(tvg)
            ids.append(tvg)
    return ids

def fetch(url, dest):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "SPS8007-iptv-epg/1.0"}
    )
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, length=1024 * 1024)

def open_xml(path):
    # Accept either .xml or gzip-compressed XML.
    raw = open(path, "rb")
    head = raw.read(2)
    raw.seek(0)
    if head == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=raw, mode="rb"), raw
    return raw, None

def source_index(path, wanted_norm):
    """Return normalized source channel id -> (original id, channel XML)."""
    fh, parent = open_xml(path)
    found = {}
    try:
        for event, elem in ET.iterparse(fh, events=("end",)):
            if elem.tag == "channel":
                sid = elem.get("id", "")
                nid = norm_id(sid)
                if nid in wanted_norm and nid not in found:
                    found[nid] = (sid, ET.tostring(elem, encoding="unicode"))
                elem.clear()
    finally:
        fh.close()
        if parent:
            parent.close()
    return found

def source_programmes(path, wanted_map, programme_out):
    """Append matching programme elements into lists keyed by M3U tvg-id."""
    fh, parent = open_xml(path)
    count = 0
    try:
        for event, elem in ET.iterparse(fh, events=("end",)):
            if elem.tag == "programme":
                sid = elem.get("channel", "")
                nid = norm_id(sid)
                target = wanted_map.get(nid)
                if target:
                    elem.set("channel", target)
                    programme_out.setdefault(target, []).append(
                        ET.tostring(elem, encoding="unicode")
                    )
                    count += 1
                elem.clear()
    finally:
        fh.close()
        if parent:
            parent.close()
    return count

def main():
    m3u_ids = load_m3u_ids()
    if not m3u_ids:
        raise SystemExit("No tvg-id values found in playlist.m3u")

    wanted_norm = {norm_id(x) for x in m3u_ids}
    channel_xml = {}
    source_for = {}
    failures = []

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # First pass: download/index channels. Earlier sources win on collisions.
        for label, url in SOURCES:
            dest = td / f"{label}.xml.gz"
            try:
                print(f"Downloading {label}: {url}")
                fetch(url, dest)
                idx = source_index(dest, wanted_norm)
                for nid, data in idx.items():
                    if nid not in channel_xml:
                        channel_xml[nid] = data
                        source_for[nid] = label
                print(f"  matched source channel IDs: {len(idx)}")
            except Exception as e:
                failures.append(f"{label}: {e}")
                print(f"  FAILED: {e}")

        # Map normalized IDs to the exact M3U IDs that must appear in XMLTV.
        wanted_map = {}
        for tvg in m3u_ids:
            nid = norm_id(tvg)
            if nid in channel_xml:
                wanted_map[nid] = tvg

        programmes = {}
        total_programmes = 0

        # Second pass: stream programmes from each source.
        for label, url in SOURCES:
            dest = td / f"{label}.xml.gz"
            if not dest.exists():
                continue
            try:
                n = source_programmes(dest, wanted_map, programmes)
                total_programmes += n
                print(f"Programmes from {label}: {n}")
            except Exception as e:
                failures.append(f"{label} programmes: {e}")
                print(f"  PROGRAMME PASS FAILED: {e}")

    matched = sum(1 for tvg in m3u_ids if norm_id(tvg) in channel_xml)
    print(f"M3U channels: {len(m3u_ids)}")
    print(f"Matched channels: {matched}")
    print(f"Programme entries: {total_programmes}")

    # Safety gate: never replace a known-good published EPG with an empty/broken one.
    if matched < 150 or total_programmes < 1000:
        raise SystemExit(
            f"Safety check failed: only {matched} channels / {total_programmes} programmes. "
            "Existing published EPG was not replaced."
        )

    # Build valid XMLTV. Channels are emitted in M3U order.
    with open(TMP, "w", encoding="utf-8", newline="\n") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write('<tv generator-info-name="SPS8007 Automated IPTV EPG" '
                  'source-info-name="AU CA NZ ZA GB US">\n')

        for tvg in m3u_ids:
            nid = norm_id(tvg)
            if nid not in channel_xml:
                continue
            _, xml = channel_xml[nid]
            # Replace source channel id with the exact M3U tvg-id.
            xml = re.sub(
                r'(<channel\b[^>]*\bid=")[^"]+(")',
                lambda m: m.group(1) + tvg + m.group(2),
                xml,
                count=1,
            )
            out.write(xml)
            out.write("\n")

        for tvg in m3u_ids:
            for p in programmes.get(tvg, []):
                out.write(p)
                out.write("\n")

        out.write("</tv>\n")

    # Validate before replacing the published file.
    ET.parse(TMP)
    TMP.replace(OUT)

    if failures:
        print("Warnings (source failures):")
        for f in failures:
            print(" -", f)

    print(f"Published: {OUT}")

if __name__ == "__main__":
    main()
