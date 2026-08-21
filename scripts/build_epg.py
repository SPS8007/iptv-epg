#!/usr/bin/env python3
"""
Build an XMLTV EPG for the M3U in this repository.

Design:
- playlist.m3u is the master channel list.
- The existing iptv_epg_all_six_filtered.xml is used as the safe baseline.
- Current AU/CA/NZ/ZA/GB/US feeds are downloaded.
- Exact tvg-id matches are preferred.
- If IDs differ between providers, country-aware channel-name matching is used.
- Existing programme data is retained when a current source cannot be matched.
- The output always uses the exact M3U tvg-id values.
- A bad/new source cannot wipe out the working EPG.
"""

import gzip
import re
import shutil
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
M3U = ROOT / "playlist.m3u"
BASELINE = ROOT / "iptv_epg_all_six_filtered.xml"
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

def norm_name(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode().lower()

    # Remove common provider/country prefixes and presentation suffixes.
    value = re.sub(r"\b(?:au|australia|ca|canada|nz|new zealand|za|south africa|gb|uk|us|usa)\b", " ", value)
    value = re.sub(
        r"\b(?:hd|uhd|fhd|sd|4k|east|west|eastern|western|pacific|central|atlantic|"
        r"north|south|channel|network|tv|television)\b",
        " ",
        value,
    )
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def country_from_id(tvg_id):
    x = (tvg_id or "").lower()
    if x.endswith(".uk"): return "GB"
    if x.endswith(".us"): return "US"
    if x.endswith(".ca"): return "CA"
    if x.endswith(".au"): return "AU"
    if x.endswith(".nz"): return "NZ"
    if x.endswith(".ze") or x.endswith(".za"): return "ZA"
    return None

def load_m3u():
    lines = M3U.read_text(encoding="utf-8", errors="ignore").splitlines()
    channels = []
    seen = set()
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue
        url = lines[i + 1].strip() if i + 1 < len(lines) else ""
        m = re.search(r'tvg-id="([^"]*)"', line)
        tvg_id = m.group(1).strip() if m else ""
        name = line.split(",", 1)[1].strip() if "," in line else tvg_id
        if tvg_id and tvg_id not in seen:
            seen.add(tvg_id)
            channels.append((tvg_id, name))
    return channels

def fetch(url, dest):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SPS8007-IPTV-EPG/2.0",
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as response:
        data = response.read()
    # The feeds are expected to be gzip, but accept plain XML too.
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    dest.write_bytes(data)

def read_baseline():
    if not BASELINE.exists():
        return {}, {}, {}
    channels = {}
    programmes = defaultdict(list)
    channel_order = []

    for event, elem in ET.iterparse(BASELINE, events=("end",)):
        if elem.tag == "channel":
            cid = elem.get("id", "")
            if cid and cid not in channels:
                channels[cid] = ET.tostring(elem, encoding="unicode")
                channel_order.append(cid)
            elem.clear()
        elif elem.tag == "programme":
            cid = elem.get("channel", "")
            if cid:
                programmes[cid].append(ET.tostring(elem, encoding="unicode"))
            elem.clear()

    return channels, programmes, channel_order

def source_channel_index(path):
    """
    Build:
      id -> display name
      normalized display name -> list of source ids
    """
    by_id = {}
    by_name = defaultdict(list)

    for event, elem in ET.iterparse(path, events=("end",)):
        if elem.tag == "channel":
            sid = elem.get("id", "")
            names = [
                (x.text or "").strip()
                for x in elem.findall("display-name")
                if (x.text or "").strip()
            ]
            display = names[0] if names else sid
            if sid:
                by_id[sid] = display
                n = norm_name(display)
                if n:
                    by_name[n].append(sid)
            elem.clear()

    return by_id, by_name

def source_programmes(path, source_to_target, output):
    count = 0
    for event, elem in ET.iterparse(path, events=("end",)):
        if elem.tag == "programme":
            sid = elem.get("channel", "")
            target = source_to_target.get(sid)
            if target:
                elem.set("channel", target)
                output[target].append(ET.tostring(elem, encoding="unicode"))
                count += 1
            elem.clear()
    return count

def replace_channel_id(xml, new_id):
    return re.sub(
        r'(<channel\b[^>]*\bid=")[^"]+(")',
        lambda m: m.group(1) + new_id + m.group(2),
        xml,
        count=1,
    )

def main():
    m3u_channels = load_m3u()
    if not m3u_channels:
        raise SystemExit("playlist.m3u contains no tvg-id channels")

    baseline_channels, baseline_programmes, baseline_order = read_baseline()
    if not baseline_channels:
        raise SystemExit("Existing baseline EPG is missing or invalid")

    # Baseline channel IDs are mapped back to the exact current M3U IDs.
    m3u_by_norm = {norm_id(cid): (cid, name) for cid, name in m3u_channels}

    current_sources = []
    warnings = []

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        for label, url in SOURCES:
            path = td / f"{label}.xml"
            try:
                print(f"Downloading {label}: {url}")
                fetch(url, path)
                current_sources.append((label, path))
            except Exception as exc:
                warnings.append(f"{label} download failed: {exc}")
                print(f"WARNING: {label} download failed: {exc}")

        # Map each source channel to an exact M3U tvg-id.
        all_current_programmes = defaultdict(list)
        refreshed = set()
        exact_id_matches = 0
        name_matches = 0

        for label, path in current_sources:
            try:
                by_id, by_name = source_channel_index(path)
            except Exception as exc:
                warnings.append(f"{label} channel index failed: {exc}")
                print(f"WARNING: {label} channel index failed: {exc}")
                continue

            source_to_target = {}

            # 1) Exact ID match, with .ze/.za alias support.
            for sid in by_id:
                nsid = norm_id(sid)
                if nsid in m3u_by_norm:
                    target = m3u_by_norm[nsid][0]
                    source_to_target[sid] = target
                    exact_id_matches += 1

            # 2) Country-aware display-name match for IDs that differ.
            for tvg_id, m3u_name in m3u_channels:
                if tvg_id in source_to_target.values():
                    continue

                wanted_country = country_from_id(tvg_id)
                if wanted_country and wanted_country != label:
                    continue

                nn = norm_name(m3u_name)
                candidates = by_name.get(nn, [])
                if len(candidates) == 1:
                    source_to_target[candidates[0]] = tvg_id
                    name_matches += 1

            try:
                n = source_programmes(path, source_to_target, all_current_programmes)
                print(
                    f"{label}: exact IDs={sum(1 for x in source_to_target.values())}, "
                    f"programmes={n}"
                )
            except Exception as exc:
                warnings.append(f"{label} programme parse failed: {exc}")
                print(f"WARNING: {label} programme parse failed: {exc}")

        # Build a stable 301-channel XMLTV. Channels with no current guide
        # remain present but keep baseline programmes if available.
        current_by_id = {cid: name for cid, name in m3u_channels}

        with open(TMP, "w", encoding="utf-8", newline="\n") as out:
            out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            out.write(
                '<tv generator-info-name="SPS8007 Automated IPTV EPG" '
                'source-info-name="AU CA NZ ZA GB US">\n'
            )

            for tvg_id, m3u_name in m3u_channels:
                # Prefer a baseline channel definition where available.
                base_xml = None
                if tvg_id in baseline_channels:
                    base_xml = baseline_channels[tvg_id]
                else:
                    # Try the .ze/.za normalized baseline ID.
                    for bid, xml in baseline_channels.items():
                        if norm_id(bid) == norm_id(tvg_id):
                            base_xml = xml
                            break

                if base_xml:
                    ch_xml = replace_channel_id(base_xml, tvg_id)
                    out.write(ch_xml + "\n")
                else:
                    # Include all M3U channels even if they have no guide yet.
                    ch = ET.Element("channel", {"id": tvg_id})
                    dn = ET.SubElement(ch, "display-name")
                    dn.text = m3u_name
                    out.write(ET.tostring(ch, encoding="unicode") + "\n")

            # Current programmes replace baseline programmes where available.
            # This means a failed provider cannot wipe the working guide.
            for tvg_id, m3u_name in m3u_channels:
                current = all_current_programmes.get(tvg_id)
                if current:
                    for p in current:
                        out.write(p + "\n")
                else:
                    # Fall back to the baseline channel's existing programmes.
                    if tvg_id in baseline_programmes:
                        for p in baseline_programmes[tvg_id]:
                            out.write(p + "\n")

            out.write("</tv>\n")

    # Validate before replacing the repository copy.
    ET.parse(TMP)
    TMP.replace(OUT)

    print(f"M3U channels: {len(m3u_channels)}")
    print(f"Baseline channels: {len(baseline_channels)}")
    print(f"Current exact/name mapping attempts: {exact_id_matches}/{name_matches}")
    print(f"Published: {OUT}")

    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(" -", warning)

if __name__ == "__main__":
    main()
