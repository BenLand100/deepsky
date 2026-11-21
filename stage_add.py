#!/usr/bin/env python3
"""Wrapper around add_image.py that matches staged images to /mnt/scratch/astro/
folders, extracts FITS/XISF acquisition metadata, and invokes add_image.py
without manual prompting.
"""
import argparse
import base64
import csv
import difflib
import os
import re
import shlex
import struct
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

from astropy.io import fits

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
FITS_EXTS = {".fit", ".fits"}
SOLVE_FIELD_SUFFIXES = ("-objs.png", "-indx.png", "-ngc.png")
CALIB_NAME_HINTS = ("bias", "dark", "flat", "master_dark", "master_flat", "masterdark", "masterflat", "masterbias")

CANONICAL_CHANNELS = {
    "osc": "OSC",
    "nofilter": "OSC",
    "none": "OSC",
    "l": "L",
    "lum": "L",
    "luminance": "L",
    "r": "R",
    "red": "R",
    "g": "G",
    "green": "G",
    "b": "B",
    "blue": "B",
    "sii": "Sii",
    "s2": "Sii",
    "ha": "Ha",
    "h": "Ha",
    "halpha": "Ha",
    "oiii": "Oiii",
    "o3": "Oiii",
    "o": "Oiii",
}

CATALOG_RE = re.compile(
    r"^(?:M\d+|NGC\d+|IC\d+|Sh2-\d+|H[CG]{2}\d+|Arp\d+|B\d+|LDN\d+|LBN\d+|Abell\d+|SN\d+[a-z]+|C\d+|UGC\d+|PGC\d+)$",
    re.IGNORECASE,
)

VARIANT_TOKENS = {
    "v1", "v2", "v3", "v4", "v5",
    "crop", "annotate", "annotated", "full", "dark", "darker", "rough", "raw", "final",
    "hoo", "sho", "hss", "halrgb", "hargb", "lrgb", "rgb", "osc", "combo", "ifn",
    "post", "pix", "natural", "standard", "recombined", "drizzle", "stacked",
}


def is_stage_image(path: Path) -> bool:
    if not path.is_file():
        return False
    name_l = path.name.lower()
    if any(name_l.endswith(s) for s in SOLVE_FIELD_SUFFIXES):
        return False
    return path.suffix.lower() in IMAGE_EXTS


def load_existing_images(csv_path: Path) -> set:
    existing = set()
    if csv_path.exists():
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rel = row.get("file", "").strip()
                if rel:
                    existing.add(Path(rel).name)
    return existing


def tokenize(name: str) -> list:
    return [t for t in re.split(r"[^a-zA-Z0-9]+", name.lower()) if t]


def extract_date_prefix(name: str):
    m = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?", name)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def find_astro_folder(img_path: Path, astro_dir: Path):
    """Find the best matching folder in astro_dir for img_path.

    Returns (folder_path, match_reason) or (None, None).
    """
    if not astro_dir.is_dir():
        return None, None

    folders = sorted([d for d in astro_dir.iterdir() if d.is_dir()])

    # Tier 1: Exact image filename exists inside the folder
    exact_file_hits = [d for d in folders if (d / img_path.name).exists()]
    if len(exact_file_hits) == 1:
        return exact_file_hits[0], "exact-file"
    if len(exact_file_hits) > 1:
        best = max(
            exact_file_hits,
            key=lambda d: difflib.SequenceMatcher(None, img_path.stem.lower(), d.name.lower()).ratio(),
        )
        return best, "exact-file"

    # Tier 2: Same-stem .fit / .fits file exists inside the folder
    stem_fits_hits = [
        d for d in folders
        if (d / f"{img_path.stem}.fit").exists() or (d / f"{img_path.stem}.fits").exists()
    ]
    if len(stem_fits_hits) == 1:
        return stem_fits_hits[0], "exact-stem-fits"

    # Tier 3: Prefix / date + token similarity scoring
    img_stem_l = img_path.stem.lower()
    img_date = extract_date_prefix(img_stem_l)
    img_tokens = set(tokenize(img_stem_l))
    if img_date:
        img_tokens -= {p for p in img_date if p}
    img_tokens -= VARIANT_TOKENS

    best_folder = None
    best_score = 0.0

    for d in folders:
        d_l = d.name.lower()
        score = 0.0

        # Exact or strong prefix match
        if d_l == img_stem_l:
            return d, "exact-folder"
        if img_stem_l.startswith(d_l) or d_l.startswith(img_stem_l):
            score += 3.0

        # Date matching (including month+day match for year typo like 2036 vs 2026)
        d_date = extract_date_prefix(d_l)
        if img_date and d_date:
            if img_date == d_date:
                score += 2.5
            elif img_date[0] == d_date[0] and img_date[1] == d_date[1]:
                score += 1.5
            elif img_date[1] == d_date[1] and img_date[2] and img_date[2] == d_date[2]:
                score += 1.0

        d_tokens = set(tokenize(d_l))
        if d_date:
            d_tokens -= {p for p in d_date if p}

        if img_tokens and d_tokens:
            overlap = img_tokens & d_tokens
            # Fuzzy token overlap (e.g. adromeda vs andromeda)
            fuzzy_hits = 0.0
            for t in img_tokens - overlap:
                best_t = max(
                    (difflib.SequenceMatcher(None, t, dt).ratio() for dt in d_tokens),
                    default=0.0,
                )
                if best_t >= 0.8:
                    fuzzy_hits += best_t
            union_size = len(img_tokens | d_tokens)
            score += 4.0 * (len(overlap) + fuzzy_hits) / max(1, union_size)
            # Bonus if a catalog token matches directly
            for tok in overlap:
                if CATALOG_RE.match(tok):
                    score += 1.5

        score += difflib.SequenceMatcher(None, img_stem_l, d_l).ratio()

        if score > best_score:
            best_score = score
            best_folder = d

    if best_folder is not None and best_score >= 2.2:
        return best_folder, f"fuzzy({best_score:.2f})"
    return None, None


def read_xisf_xml_header(path: Path):
    """Read the XML header from an XISF file using only Python stdlib."""
    try:
        with open(path, "rb") as f:
            sig = f.read(8)
            if sig != b"XISF0100":
                return None
            header_len = struct.unpack("<I", f.read(4))[0]
            _reserved = f.read(4)
            xml_bytes = f.read(header_len)
        return ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return None


def parse_xisf_metadata(path: Path) -> dict:
    """Extract FITSKeywords, XISF Properties, and stacked count from an XISF file."""
    root = read_xisf_xml_header(path)
    if root is None:
        return {}

    fk = {}
    props = {}

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "FITSKeyword":
            name = elem.attrib.get("name", "").strip()
            val = elem.attrib.get("value", "").strip().strip("'").strip()
            if name and name not in fk:
                fk[name] = val
        elif tag == "Property":
            pid = elem.attrib.get("id", "").strip()
            if not pid or pid in props:
                continue
            ptype = elem.attrib.get("type", "")
            loc = elem.attrib.get("location", "")
            if ptype == "F64Vector" and loc == "inline:base64" and elem.text:
                try:
                    raw = base64.b64decode(elem.text.strip())
                    if len(raw) >= 8:
                        props[pid] = str(struct.unpack("<d", raw[:8])[0])
                        continue
                except Exception:
                    pass
            val = elem.attrib.get("value", elem.text or "").strip()
            props[pid] = val

    out = dict(fk)

    # Extract STACKCNT from ProcessingHistory if not already present
    if "STACKCNT" not in out and "NCOMBINE" not in out:
        raw_hist = props.get("PixInsight:ProcessingHistory", "")
        if raw_hist:
            try:
                hist_root = ET.fromstring(raw_hist.strip())
                for cls in ("ImageIntegration", "DrizzleIntegration"):
                    node = hist_root.find(f".//instance[@class='{cls}']/parameter[@id='numberOfImages']")
                    if node is not None and node.get("value"):
                        out["STACKCNT"] = node.get("value")
                        break
                    table = hist_root.find(f".//instance[@class='{cls}']/table[@id='images']")
                    if table is not None:
                        out["STACKCNT"] = table.get("rows") or str(len(table.findall("./tr")))
                        break
            except Exception:
                pass

    # Fallback aliases from XISF properties
    prop_aliases = {
        "FILTER": ["Instrument:Filter:Name", "Filter:Name", "Filter"],
        "EXPTIME": ["Instrument:FrameExposureTime", "Instrument:ExposureTime", "ExposureTime"],
        "INSTRUME": ["Instrument:Camera:Name", "Instrument:Name", "Camera:Name"],
        "TELESCOP": ["Instrument:Telescope:Name", "Telescope:Name"],
        "FOCALLEN": ["Instrument:Telescope:FocalLength", "Telescope:FocalLength"],
        "STACKCNT": ["ImageIntegration:NumberOfImages", "NumberOfImages", "StackCount"],
    }
    for key, aliases in prop_aliases.items():
        if key not in out:
            for a in aliases:
                if props.get(a):
                    out[key] = props[a]
                    break

    # Compute STACKCNT from PCL:TotalExposureTime / EXPTIME if still missing
    if "STACKCNT" not in out and "NCOMBINE" not in out:
        tot = props.get("PCL:TotalExposureTime")
        exp = out.get("EXPTIME")
        if tot and exp:
            try:
                if float(exp) > 0:
                    out["STACKCNT"] = str(int(round(float(tot) / float(exp))))
            except ValueError:
                pass

    return out


def normalize_channel(raw_filter: str, is_osc: bool = False):
    if not raw_filter:
        return "OSC" if is_osc else None
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", raw_filter).lower()
    if cleaned in CANONICAL_CHANNELS:
        return CANONICAL_CHANNELS[cleaned]
    if is_osc:
        return "OSC"
    return None


def normalize_telescope(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip()
    sl = s.lower()
    if "quattro" in sl and "200p" in sl:
        return "Sky Watcher Quattro 200P F/4"
    if "fra300" in sl:
        return "Askar FRA300 F/5"
    if "rokinon" in sl and "135" in sl:
        return "Rokinon 135mm F/2"
    if "tamron" in sl and "300" in sl:
        return "Tamron 70-300mm @ 300mm F/9"
    if "sony" in sl and "90" in sl:
        return "Sony 90mm F/2.8 G"
    return s.replace("\\", "/")


def normalize_camera(raw: str, has_bayer: bool = False) -> str:
    if not raw:
        return ""
    s = raw.strip()
    sl = s.lower()
    if "asi2600mc" in sl or ("zwo" in sl and ("osc" in sl or has_bayer)):
        return "ZWO ASI2600MC Pro"
    if "asi2600mm" in sl or ("zwo" in sl and "mono" in sl):
        return "ZWO ASI2600MM Pro"
    if "ilce-7c" in sl or "a7c" in sl:
        return "Sony ILCE-7C"
    return s


def parse_catalog_and_name(img_path: Path):
    """Parse catalog_identifier and common_name from filename stem."""
    stem = img_path.stem
    # Strip leading date: YYYY-MM-DD_ or YYYY-MM_
    rest = re.sub(r"^\d{4}-\d{2}(?:-\d{2})?[_ -]+", "", stem)
    tokens = [t for t in rest.split("_") if t]

    # Strip trailing variant tokens
    while tokens and tokens[-1].lower() in VARIANT_TOKENS:
        tokens.pop()

    catalogs = []
    name_parts = []
    for tok in tokens:
        if CATALOG_RE.match(tok):
            # Fix common typo HGC92 -> HCG92
            fixed = re.sub(r"^HGC(\d+)$", r"HCG\1", tok, flags=re.IGNORECASE)
            catalogs.append(fixed)
        else:
            name_parts.append(tok)

    catalog_id = " / ".join(catalogs)
    common_name = " ".join(name_parts)
    # Small polish for common names
    common_name = re.sub(r"\bStephans\b", "Stephan's", common_name)
    common_name = re.sub(r"\bThors\b", "Thor's", common_name)
    common_name = re.sub(r"\bdumbell\b", "Dumbbell", common_name, flags=re.IGNORECASE)
    return catalog_id, common_name


def is_calibration_file(path: Path) -> bool:
    nl = path.name.lower()
    return any(h in nl for h in CALIB_NAME_HINTS)


def find_light_dirs(folder: Path) -> list:
    return sorted([
        d for d in folder.iterdir()
        if d.is_dir() and d.name.lower().startswith("light")
    ])


def get_first_useful_fits_header(folder: Path):
    """Find a FITS header with acquisition metadata (RA/DEC/TELESCOP/FOCALLEN)."""
    # 1. Check raw Light/ subdirs first (most reliable for full acquisition headers)
    for ld in find_light_dirs(folder):
        for f in sorted(ld.rglob("*")):
            if f.is_file() and f.suffix.lower() in FITS_EXTS:
                try:
                    hdr = fits.getheader(str(f))
                    if any(k in hdr for k in ("RA", "OBJCTRA", "TELESCOP", "FOCALLEN")):
                        return hdr, f
                except Exception:
                    continue

    # 2. Check top-level non-calibration FITS files
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.suffix.lower() in FITS_EXTS and not is_calibration_file(f):
            try:
                hdr = fits.getheader(str(f))
                if any(k in hdr for k in ("RA", "OBJCTRA", "TELESCOP", "FOCALLEN")):
                    return hdr, f
            except Exception:
                continue

    return None, None


def extract_exposures(folder: Path, is_osc: bool) -> dict:
    """Return dict mapping canonical channel -> (count_int, exptime_float)."""
    exposures = {}

    # Priority 1: masterLight*.xisf at folder root (or masters/ subdir)
    xisf_lights = sorted(folder.glob("masterLight*.xisf"))
    if not xisf_lights and (folder / "masters").is_dir():
        xisf_lights = sorted((folder / "masters").glob("masterLight*.xisf"))

    if xisf_lights:
        for xf in xisf_lights:
            meta = parse_xisf_metadata(xf)
            filt = meta.get("FILTER", "")
            # Also parse filter from filename if needed: FILTER-Ha_mono
            if not filt:
                m = re.search(r"FILTER-([^_]+)", xf.name)
                if m:
                    filt = m.group(1)
            ch = normalize_channel(filt, is_osc=is_osc)
            if not ch:
                continue
            exptime = meta.get("EXPTIME")
            if not exptime:
                m = re.search(r"EXPOSURE-([0-9.]+)s", xf.name)
                if m:
                    exptime = m.group(1)
            stackcnt = meta.get("STACKCNT") or meta.get("NCOMBINE")
            if ch and stackcnt and exptime:
                try:
                    cnt = int(round(float(stackcnt)))
                    exp = float(exptime)
                    # If multiple masterLights for same channel (e.g. autocrop vs full), keep largest
                    if ch not in exposures or cnt * exp > exposures[ch][0] * exposures[ch][1]:
                        exposures[ch] = (cnt, exp)
                except ValueError:
                    pass
        if exposures:
            return exposures

    # Priority 2: Siril stacked FITS at folder root
    stacked_fits = [
        f for f in sorted(folder.iterdir())
        if f.is_file() and f.suffix.lower() in FITS_EXTS and not is_calibration_file(f)
    ]
    for sf in stacked_fits:
        try:
            hdr = fits.getheader(str(sf))
        except Exception:
            continue
        stackcnt = hdr.get("STACKCNT") or hdr.get("NCOMBINE")
        exptime = hdr.get("EXPTIME") or hdr.get("EXPOSURE")
        if stackcnt and exptime and float(stackcnt) > 1 and float(exptime) > 0:
            ch = normalize_channel(str(hdr.get("FILTER", "")), is_osc=is_osc or ("BAYERPAT" in hdr))
            if ch:
                cnt = int(round(float(stackcnt)))
                exp = float(exptime)
                if ch not in exposures or cnt * exp > exposures[ch][0] * exposures[ch][1]:
                    exposures[ch] = (cnt, exp)
    if exposures:
        return exposures

    # Priority 3: Count raw FITS files in Light* directories
    counts = defaultdict(int)
    exptimes = {}
    for ld in find_light_dirs(folder):
        # Group by immediate parent dir so we only read 1 FITS header per filter dir
        by_dir = defaultdict(list)
        for f in sorted(ld.rglob("*")):
            if f.is_file() and f.suffix.lower() in FITS_EXTS:
                by_dir[f.parent].append(f)
        for pdir, flist in by_dir.items():
            try:
                hdr = fits.getheader(str(flist[0]))
            except Exception:
                continue
            filt = str(hdr.get("FILTER", "")).strip()
            if not filt and pdir != ld:
                filt = pdir.name
            ch = normalize_channel(filt, is_osc=is_osc or ("BAYERPAT" in hdr))
            exp = hdr.get("EXPTIME") or hdr.get("EXPOSURE")
            if ch and exp:
                counts[ch] += len(flist)
                exptimes[ch] = float(exp)

    for ch, cnt in counts.items():
        exposures[ch] = (cnt, exptimes[ch])
    return exposures


def infer_processor(folder: Path) -> str:
    if list(folder.glob("masterLight*.xisf")) or (folder / "masters").is_dir():
        return "PixInsight/WBPP"
    if (folder / "process").is_dir() and list((folder / "process").glob("*.xpsm")):
        return "PixInsight/WBPP"
    if list(folder.glob("r_*_stacked.fit")) or list(folder.glob("r_*_stacked.fits")):
        return "PixInsight/Siril"
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.suffix.lower() in FITS_EXTS and not is_calibration_file(f):
            try:
                prog = str(fits.getheader(str(f)).get("PROGRAM", "")).lower()
                if "pixinsight" in prog:
                    return "PixInsight/WBPP"
                if "siril" in prog:
                    return "PixInsight/Siril"
            except Exception:
                continue
    return "PixInsight/Siril"


def infer_image_type(img_path: Path, folder: Path, exposures: dict, is_osc: bool) -> str:
    stem_l = img_path.stem.lower()
    folder_l = folder.name.lower()
    channels = set(exposures.keys())
    has_rgb = {"R", "G", "B"}.issubset(channels)

    if is_osc:
        has_broadband = any(d.name.lower() == "light_broadband" for d in folder.iterdir() if d.is_dir())
        has_haoiii_file = any(
            "haoiii" in f.name.lower() or "haoii" in f.name.lower()
            for f in folder.iterdir() if f.is_file()
        )
        if has_broadband or "hargb" in stem_l or "rgbstars" in stem_l:
            return "OSC / Dualband + Broadband"
        if "haoiii" in stem_l or "haoiii" in folder_l or has_haoiii_file:
            return "OSC / Dualband"
        return "OSC"

    if "_sho" in stem_l:
        return "SHO+RGB" if has_rgb else "SHO"
    if "_hoo" in stem_l:
        return "HOO+RGB" if has_rgb else "HOO"
    if "_hss" in stem_l:
        return "HSS+RGB" if has_rgb else "HSS"
    if "halrgb" in stem_l or ({"L", "R", "G", "B", "Ha"}.issubset(channels)):
        return "HaLRGB"
    if {"Sii", "Ha", "Oiii"}.issubset(channels):
        return "SHO+RGB" if has_rgb else "SHO"
    if {"Ha", "Oiii"}.issubset(channels):
        return "HOO+RGB" if has_rgb else "HOO"
    if {"L", "R", "G", "B"}.issubset(channels):
        return "LRGB"
    if has_rgb:
        return "RGB"
    return "OSC"


def extract_folder_metadata(folder: Path, img_path: Path) -> dict:
    meta = {}
    hdr, hdr_file = get_first_useful_fits_header(folder)

    # Fallback to XISF if no FITS header found
    xisf_hdr = {}
    if hdr is None:
        for xf in sorted(folder.glob("masterLight*.xisf")):
            xisf_hdr = parse_xisf_metadata(xf)
            if xisf_hdr:
                hdr_file = xf
                break

    def get_key(k):
        if hdr is not None and k in hdr:
            return hdr[k]
        return xisf_hdr.get(k)

    ra = get_key("RA")
    dec = get_key("DEC")
    if ra is None:
        ra = get_key("OBJCTRA")
    if dec is None:
        dec = get_key("OBJCTDEC")
    if ra is not None:
        meta["ra"] = str(ra).strip()
    if dec is not None:
        meta["dec"] = str(dec).strip()

    focallen = get_key("FOCALLEN")
    xpixsz = get_key("XPIXSZ") or get_key("PIXSIZE1")
    if focallen and xpixsz:
        try:
            scale = 206.264806 * float(xpixsz) / float(focallen)
            # Generous bounds handle both crops (same scale) and 2x drizzle (half scale)
            meta["scale_low"] = round(scale * 0.45, 3)
            meta["scale_high"] = round(scale * 1.5, 3)
            meta["scale_units"] = "arcsecperpix"
            meta["nominal_scale"] = round(scale, 3)
        except (ValueError, ZeroDivisionError):
            pass

    has_bayer = get_key("BAYERPAT") is not None
    raw_instr = str(get_key("INSTRUME") or "")
    raw_tele = str(get_key("TELESCOP") or "")
    is_osc = has_bayer or ("osc" in raw_instr.lower()) or ("2600mc" in raw_instr.lower())

    meta["telescope"] = normalize_telescope(raw_tele)
    meta["camera"] = normalize_camera(raw_instr, has_bayer=has_bayer)
    meta["processor"] = infer_processor(folder)
    meta["exposures"] = extract_exposures(folder, is_osc=is_osc)
    meta["image_type"] = infer_image_type(img_path, folder, meta["exposures"], is_osc=is_osc)
    meta["catalog_id"], meta["common_name"] = parse_catalog_and_name(img_path)
    meta["source_file"] = str(hdr_file) if hdr_file else ""
    return meta


def format_exptime(val: float) -> str:
    return str(int(val)) if float(val).is_integer() else f"{val:g}"


def build_add_image_cmd(add_image_script: Path, img_path: Path, meta: dict, args) -> list:
    cmd = [sys.executable, str(add_image_script), str(img_path)]
    if args.api_key:
        cmd.extend(["--api-key", args.api_key])
    if args.api_fallback:
        cmd.append("--api-fallback")
    if args.local_timeout is not None:
        cmd.extend(["--local-timeout", str(args.local_timeout)])
    if meta.get("ra"):
        cmd.extend(["--ra", meta["ra"]])
    if meta.get("dec"):
        cmd.extend(["--dec", meta["dec"]])
    if args.radius is not None:
        cmd.extend(["--radius", str(args.radius)])
    if meta.get("scale_low") is not None and meta.get("scale_high") is not None:
        cmd.extend([
            "--scale-low", str(meta["scale_low"]),
            "--scale-high", str(meta["scale_high"]),
            "--scale-units", meta.get("scale_units", "arcsecperpix"),
        ])
    if meta.get("telescope"):
        cmd.extend(["--telescope", meta["telescope"]])
    if meta.get("camera"):
        cmd.extend(["--camera", meta["camera"]])
    if meta.get("processor"):
        cmd.extend(["--processor", meta["processor"]])
    if meta.get("image_type"):
        cmd.extend(["--image-type", meta["image_type"]])
    if "catalog_id" in meta:
        cmd.extend(["--catalog-id", meta["catalog_id"]])
    if "common_name" in meta:
        cmd.extend(["--common-name", meta["common_name"]])
    for ch, (cnt, exp) in sorted(meta.get("exposures", {}).items()):
        cmd.extend(["--exposure", f"{ch}:{cnt}:{format_exptime(exp)}"])
    if not args.prompt:
        cmd.append("--non-interactive")
    return cmd


def main():
    base_dir = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(
        description="Match staged images to /mnt/scratch/astro/ metadata and run add_image.py"
    )
    ap.add_argument(
        "targets",
        nargs="*",
        help="Image file(s) or directory to process (default: ./stage)",
    )
    ap.add_argument(
        "--astro-dir",
        type=Path,
        default=Path("/mnt/scratch/astro"),
        help="Root directory containing raw astro session folders (default: /mnt/scratch/astro)",
    )
    ap.add_argument("--api-key", help="Forward astrometry.net API key to add_image.py")
    ap.add_argument("--api-fallback", action="store_true", help="Try local solve-field first, falling back to API on failure/timeout (requires --api-key)")
    ap.add_argument("--local-timeout", type=float, default=None, help="Max seconds for local solve-field (default in add_image.py: 60)")
    ap.add_argument("--radius", type=float, default=None, help="Override search radius in degrees")
    ap.add_argument("--dry-run", action="store_true", help="Print matched metadata and commands without executing")
    ap.add_argument("--prompt", action="store_true", help="Do not pass --non-interactive to add_image.py")
    ap.add_argument("--force", action="store_true", help="Process images even if already in live_images.csv")
    args = ap.parse_args()
    if args.api_fallback and not args.api_key:
        ap.error("--api-fallback requires --api-key")

    targets = [Path(t) for t in args.targets] if args.targets else [base_dir / "stage"]
    images = []
    for t in targets:
        if t.is_dir():
            images.extend(sorted(p for p in t.iterdir() if is_stage_image(p)))
        elif t.is_file():
            images.append(t)
        else:
            print(f"Warning: {t} not found, skipping.", file=sys.stderr)

    if not images:
        print("No images found to process.")
        sys.exit(0)

    existing = load_existing_images(base_dir / "live_images.csv")
    add_image_script = base_dir / "add_image.py"

    failed = []
    for img in images:
        if not args.force and img.name in existing:
            print(f"[SKIP] {img.name} (already in live_images.csv; use --force to re-add)")
            continue

        folder, reason = find_astro_folder(img, args.astro_dir)
        if folder is None:
            print(f"[NO MATCH] {img.name} -> no matching folder in {args.astro_dir}")
            meta = {}
            meta["catalog_id"], meta["common_name"] = parse_catalog_and_name(img)
        else:
            meta = extract_folder_metadata(folder, img)
            exp_summary = ", ".join(
                f"{ch}:{cnt}x{format_exptime(exp)}s"
                for ch, (cnt, exp) in sorted(meta.get("exposures", {}).items())
            )
            print(f"[MATCH:{reason}] {img.name} -> {folder.name}")
            print(
                f"  RA={meta.get('ra', '?')} DEC={meta.get('dec', '?')} "
                f"scale={meta.get('nominal_scale', '?')}\"/px | "
                f"{meta.get('telescope', '?')} | {meta.get('camera', '?')} | "
                f"{meta.get('processor', '?')} | {meta.get('image_type', '?')}"
            )
            print(
                f"  Catalog='{meta.get('catalog_id', '')}' Name='{meta.get('common_name', '')}' "
                f"| Exposures: {exp_summary or 'none'}"
            )

        cmd = build_add_image_cmd(add_image_script, img, meta, args)
        if args.dry_run:
            print(f"  CMD: {shlex.join(cmd)}\n")
            continue

        res = subprocess.run(cmd)
        if res.returncode != 0:
            print(f"[ERROR] add_image.py failed for {img.name} (exit {res.returncode})", file=sys.stderr)
            failed.append(img.name)

    if failed:
        print(f"\nFinished with {len(failed)} failure(s): {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
