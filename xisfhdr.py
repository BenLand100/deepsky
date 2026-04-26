#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

WANTED = ["FILTER", "LIVETIME", "GAIN", "EXPTIME", "STACKCNT", "INSTRUM", "TELESCOP", "FOCAL", "TOTALEXP"]
TOTAL_KEYS = [
    "TotalExposureTime",
    "TotalIntegrationTime",
    "ImageIntegration:TotalExposureTime",
    "ImageIntegration:TotalIntegrationTime",
]
STACK_KEYS = [
    "ImageIntegration:NumberOfImages",
    "NumberOfImages",
    "StackCount",
    "IntegrationNumberOfImages",
]
FILTER_KEYS = [
    "Instrument:Filter:Name",
    "Filter:Name",
    "Filter",
    "Instrument:Filter",
]


def import_xisf():
    try:
        from xisf import XISF
        return XISF
    except Exception:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "xisf"], stdout=subprocess.DEVNULL)
        from xisf import XISF
        return XISF


def stringify(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        if "value" in value:
            return stringify(value["value"])
        return str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(stringify(v) for v in value)
    return str(value)


def normalize_keywords(fitskeywords):
    out = {}
    fk = fitskeywords or {}
    for key, vals in fk.items():
        first = vals[0] if isinstance(vals, list) else vals
        out[key] = stringify(first)
    return out


def normalize_props(props):
    out = {}
    for key, item in (props or {}).items():
        if isinstance(item, dict) and "value" in item:
            out[key] = stringify(item["value"])
        else:
            out[key] = stringify(item)
    return out


def first_present(mapping, names):
    for name in names:
        if name in mapping and mapping[name] != "":
            return mapping[name]
    return None

def collect_selected(image_meta, file_meta):
    data = {}
    fk = normalize_keywords(image_meta.get("FITSKeywords", {}))

    props = {}
    props.update(normalize_props(file_meta))
    props.update(normalize_props(image_meta.get("XISFProperties", {})))

    direct_keyword_names = {
        "FILTER":   ["FILTER"],
        "LIVETIME": ["LIVETIME"],
        "GAIN":     ["GAIN"],
        "EXPTIME":  ["EXPTIME"],
        "STACKCNT": ["STACKCNT", "NCOMBINE", "NCOMBINED"],
        "INSTRUM":  ["INSTRUM"],
        "TELESCOP": ["TELESCOP"],
        "FOCAL":    ["FOCALLEN", "FOCAL"],
    }
    for outkey, candidates in direct_keyword_names.items():
        value = first_present(fk, candidates)
        if value is not None:
            data[outkey] = value

    aliases = {
        "FILTER": FILTER_KEYS,
        "LIVETIME": ["Instrument:ExposureTime", "ExposureTime", "PCL:AstrometricSolution:ExposureTime"],
        "EXPTIME": ["Instrument:ExposureTime", "ExposureTime", "PCL:AstrometricSolution:ExposureTime"],
        "GAIN": ["Instrument:Gain", "Gain"],
        "STACKCNT": STACK_KEYS,
        "INSTRUM": ["Instrument:Camera:Name", "Instrument:Name", "Camera:Name", "Instrument"],
        "TELESCOP": ["Instrument:Telescope:Name", "Telescope:Name", "Telescope"],
        "FOCAL": ["Instrument:Telescope:FocalLength", "Telescope:FocalLength", "FocalLength"],
        "TOTALEXP": TOTAL_KEYS,
    }
    for key, names in aliases.items():
        if key in data:
            continue
        value = first_present(props, names)
        if value is not None:
            data[key] = value

    # Parse PixInsight processing history for stacked-image count
    if "STACKCNT" not in data:
        raw_hist = props.get("PixInsight:ProcessingHistory")
        if raw_hist:
            try:
                root = ET.fromstring(raw_hist.strip())

                # Prefer explicit ImageIntegration result parameter
                node = root.find(".//instance[@class='ImageIntegration']/parameter[@id='numberOfImages']")
                if node is not None:
                    value = node.get("value")
                    if value:
                        data["STACKCNT"] = value

                # Fallback: count rows in the input images table
                if "STACKCNT" not in data:
                    table = root.find(".//instance[@class='ImageIntegration']/table[@id='images']")
                    if table is not None:
                        rows_attr = table.get("rows")
                        if rows_attr:
                            data["STACKCNT"] = rows_attr
                        else:
                            data["STACKCNT"] = str(len(table.findall("./tr")))
            except Exception:
                pass

    if "TOTALEXP" not in data:
        exptime = data.get("EXPTIME") or data.get("LIVETIME")
        stackcnt = data.get("STACKCNT")
        try:
            if exptime is not None and stackcnt is not None:
                data["TOTALEXP"] = str(float(exptime) * float(stackcnt))
        except Exception:
            pass

    return data, fk, props


def print_keyvals(keyvals, keys=None):
    items = keyvals.items() if keys is None else [(k, keyvals[k]) for k in keys if k in keyvals]
    for key, value in items:
        print(f"{key:8} = {value}")


def print_xml_header(x):
    root = x.get_metadata_xml()
    try:
        ET.indent(root)
    except Exception:
        pass
    print(ET.tostring(root, encoding="unicode"))


def main():
    p = argparse.ArgumentParser(description="fitshdr-like metadata dump for XISF files")
    p.add_argument("files", nargs="+", help="XISF files to inspect")
    p.add_argument("--header", action="store_true", help="dump full XISF XML header")
    p.add_argument("--fits", action="store_true", help="dump all FITSKeywords in fitshdr-like format")
    p.add_argument("--props", action="store_true", help="dump all normalized XISF properties")
    args = p.parse_args()

    XISF = import_xisf()
    multi = len(args.files) > 1
    exit_code = 0

    for f in args.files:
        path = Path(f)
        if multi:
            print(f"==> {path} <==")
        try:
            x = XISF(str(path))
            if args.header:
                print_xml_header(x)
            else:
                file_meta = x.get_file_metadata() or {}
                images = x.get_images_metadata() or []
                if not images:
                    print(f"{path}: no image metadata found", file=sys.stderr)
                    exit_code = 1
                    continue
                for idx, image_meta in enumerate(images):
                    selected, fk, props = collect_selected(image_meta, file_meta)
                    if len(images) > 1:
                        print(f"[Image {idx}]")
                    if args.fits:
                        print_keyvals(dict(sorted(fk.items())))
                    elif args.props:
                        print_keyvals(dict(sorted(props.items())))
                    else:
                        print_keyvals(selected, WANTED)
            if multi:
                print()
        except Exception as e:
            print(f"{path}: {e}", file=sys.stderr)
            exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
