#!/usr/bin/env python3
import argparse
import csv
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".fits", ".fit", ".fts"}
API_BASE = "https://nova.astrometry.net/api"
BASE_SITE = "https://nova.astrometry.net"


def iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat()


def image_size(path: Path):
    with Image.open(path) as im:
        return im.width, im.height


def approx_extent_deg(width_px, height_px, pixscale_arcsec_per_px):
    deg_per_px = pixscale_arcsec_per_px / 3600.0
    return width_px * deg_per_px, height_px * deg_per_px


def parse_kv_text(text: str):
    data = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or " " not in line:
            continue
        key, val = line.split(None, 1)
        data[key.strip()] = val.strip()
    return data


def iter_images(folder: Path, recursive=False):
    if recursive:
        yield from (p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    else:
        yield from (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def read_input_paths(input_arg: str, recursive=False):
    p = Path(input_arg)
    if p.is_dir():
        return sorted(iter_images(p, recursive=recursive))
    if p.is_file() and p.suffix.lower() == ".csv":
        files = []
        with open(p, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                fp = row.get("file", "").strip()
                if fp:
                    files.append(Path(fp))
        return files
    raise SystemExit(f"Input must be a directory or a CSV file: {input_arg}")


def api_post(session, endpoint, payload, files=None, timeout=120):
    url = f"{API_BASE}/{endpoint}"
    if files is None:
        r = session.post(url, data={"request-json": json.dumps(payload)}, timeout=timeout)
    else:
        r = session.post(url, data={"request-json": json.dumps(payload)}, files=files, timeout=timeout)
    r.raise_for_status()
    return r.json()


def api_get(session, endpoint, timeout=120):
    url = f"{API_BASE}/{endpoint}"
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def login(http, apikey):
    data = api_post(http, "login", {"apikey": apikey})
    if data.get("status") != "success" or "session" not in data:
        raise RuntimeError(f"Login failed: {data}")
    return data["session"]


def upload_file(http, session_key, file_path, upload_args):
    payload = {
        "session": session_key,
        "allow_commercial_use": upload_args.get("allow_commercial_use", "d"),
        "allow_modifications": upload_args.get("allow_modifications", "d"),
        "publicly_visible": upload_args.get("publicly_visible", "n"),
        "downsample_factor": upload_args.get("downsample_factor", 2),
        "tweak_order": upload_args.get("tweak_order", 2),
        "crpix_center": upload_args.get("crpix_center", True),
    }
    for key in [
        "scale_units", "scale_type", "scale_lower", "scale_upper",
        "scale_est", "scale_err", "center_ra", "center_dec", "radius",
        "parity", "use_sextractor"
    ]:
        if upload_args.get(key) is not None:
            payload[key] = upload_args[key]

    mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f, mime)}
        data = api_post(http, "upload", payload, files=files, timeout=300)
    if data.get("status") != "success" or "subid" not in data:
        raise RuntimeError(f"Upload failed for {file_path}: {data}")
    return data["subid"]


def wait_for_job(http, subid, poll_seconds=20, max_wait_seconds=7200):
    deadline = time.time() + max_wait_seconds
    last = None
    while time.time() < deadline:
        submission = api_get(http, f"submissions/{subid}")
        last = submission
        jobs = [j for j in (submission.get("jobs") or []) if j is not None]
        if jobs:
            return jobs[0], submission
        time.sleep(poll_seconds)
    raise TimeoutError(f"Timed out waiting for job for subid={subid}; last={last}")


def wait_for_calibration(http, jobid, poll_seconds=20, max_wait_seconds=7200):
    deadline = time.time() + max_wait_seconds
    last = None
    while time.time() < deadline:
        job = api_get(http, f"jobs/{jobid}")
        last = job
        if job.get("status") == "success":
            cal = api_get(http, f"jobs/{jobid}/calibration")
            if cal and "ra" in cal and "dec" in cal:
                return cal
        elif job.get("status") in {"failure", "failed"}:
            raise RuntimeError(f"Job {jobid} failed: {job}")
        time.sleep(poll_seconds)
    raise TimeoutError(f"Timed out waiting for calibration for jobid={jobid}; last={last}")


def fetch_job_file(http, jobid, suffix, dest_path):
    url = f"{BASE_SITE}/{suffix}/{jobid}"
    r = http.get(url, timeout=300)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(r.content)
    return dest_path


def export_patch_json(wcs_file: Path, width_px: int, height_px: int, out_json: Path, grid: int):
    try:
        from astropy.io import fits
        from astropy.wcs import WCS
    except Exception as e:
        raise RuntimeError("This script requires astropy for curved patch export: pip install astropy") from e

    header = fits.getheader(str(wcs_file))
    w = WCS(header)

    xs = [i * (width_px - 1) / grid for i in range(grid + 1)]
    ys = [j * (height_px - 1) / grid for j in range(grid + 1)]

    coords = []
    for y in ys:
        for x in xs:
            world = w.pixel_to_world(x, y)
            ra = float(world.ra.deg)
            dec = float(world.dec.deg)
            coords.append({
                "x_px": x,
                "y_px": y,
                "u": x / (width_px - 1 if width_px > 1 else 1),
                "v": 1.0 - (y / (height_px - 1 if height_px > 1 else 1)),
                "ra_deg": ra,
                "dec_deg": dec,
            })

    corners = [
        [0, 0],
        [width_px - 1, 0],
        [width_px - 1, height_px - 1],
        [0, height_px - 1],
    ]
    corner_list = []
    for x, y in corners:
        world = w.pixel_to_world(x, y)
        corner_list.append({"x_px": x, "y_px": y, "ra_deg": float(world.ra.deg), "dec_deg": float(world.dec.deg)})

    out = {
        "width_px": width_px,
        "height_px": height_px,
        "grid": grid,
        "samples": coords,
        "corners": corner_list,
    }
    out_json.write_text(json.dumps(out, indent=2), encoding="utf-8")


def solve_one(path_str, api_key, upload_args, poll_seconds, submit_delay, max_wait_seconds, keep_wcs, wcs_dir, patch_json, patch_dir, patch_grid):
    path = Path(path_str)
    row = {
        "file": str(path),
        "mtime": "",
        "width_px": "",
        "height_px": "",
        "submission_id": "",
        "job_id": "",
        "center_ra_deg": "",
        "center_dec_deg": "",
        "pixscale_arcsec_per_px": "",
        "extent_width_deg": "",
        "extent_height_deg": "",
        "orientation_deg": "",
        "radius_deg": "",
        "solve_status": "pending",
        "saved_wcs_file": "",
        "patch_json_file": "",
        "error": "",
    }

    try:
        if not path.exists():
            row["solve_status"] = "error"
            row["error"] = "File does not exist"
            return row

        row["mtime"] = iso_mtime(path)
        width_px, height_px = image_size(path)
        row["width_px"] = width_px
        row["height_px"] = height_px

        http = requests.Session()
        http.headers.update({"User-Agent": "astrometry-batch-curved/1.0"})
        session_key = login(http, api_key)

        subid = upload_file(http, session_key, str(path), upload_args)
        row["submission_id"] = subid
        time.sleep(submit_delay)

        jobid, _submission = wait_for_job(http, subid, poll_seconds=poll_seconds, max_wait_seconds=max_wait_seconds)
        row["job_id"] = jobid
        cal = wait_for_calibration(http, jobid, poll_seconds=poll_seconds, max_wait_seconds=max_wait_seconds)

        row["center_ra_deg"] = cal.get("ra", "")
        row["center_dec_deg"] = cal.get("dec", "")
        row["pixscale_arcsec_per_px"] = cal.get("pixscale", "")
        row["orientation_deg"] = cal.get("orientation", "")
        row["radius_deg"] = cal.get("radius", "")

        if row["pixscale_arcsec_per_px"] not in ("", None):
            wdeg, hdeg = approx_extent_deg(width_px, height_px, float(row["pixscale_arcsec_per_px"]))
            row["extent_width_deg"] = wdeg
            row["extent_height_deg"] = hdeg

        saved_wcs = None
        if keep_wcs or patch_json:
            Path(wcs_dir).mkdir(parents=True, exist_ok=True)
            saved_wcs = Path(wcs_dir) / f"{path.stem}.wcs"
            fetch_job_file(http, jobid, "wcs_file", saved_wcs)
            row["saved_wcs_file"] = str(saved_wcs)

        if patch_json:
            Path(patch_dir).mkdir(parents=True, exist_ok=True)
            patch_out = Path(patch_dir) / f"{path.stem}.patch.json"
            export_patch_json(saved_wcs, width_px, height_px, patch_out, patch_grid)
            row["patch_json_file"] = str(patch_out)

        row["solve_status"] = "solved"
        return row

    except Exception as e:
        row["solve_status"] = "error"
        row["error"] = str(e)
        return row


def main():
    ap = argparse.ArgumentParser(description="Astrometry.net API batch solver with curved patch JSON export")
    ap.add_argument("input", help="Input directory OR CSV from a prior run")
    ap.add_argument("--api-key", required=True, help="Astrometry.net API key")
    ap.add_argument("--output", default="results.csv", help="Main output CSV")
    ap.add_argument("--failures-output", default="failures.csv", help="Failures-only CSV")
    ap.add_argument("--recursive", action="store_true", help="Recurse into subdirectories when input is a directory")
    ap.add_argument("--workers", type=int, default=2, help="Parallel files in flight; default 2")
    ap.add_argument("--poll-seconds", type=int, default=20, help="Polling interval; default 20")
    ap.add_argument("--submit-delay", type=int, default=2, help="Sleep after each upload; default 20")
    ap.add_argument("--max-wait-seconds", type=int, default=7200, help="Max wait per file")
    ap.add_argument("--publicly-visible", choices=["y", "n"], default="n")
    ap.add_argument("--allow-commercial-use", choices=["d", "y", "n"], default="d")
    ap.add_argument("--allow-modifications", choices=["d", "y", "n", "sa"], default="d")
    ap.add_argument("--downsample-factor", type=float, default=2)
    ap.add_argument("--tweak-order", type=int, default=2)
    ap.add_argument("--use-sextractor", action="store_true")
    ap.add_argument("--parity", type=int, choices=[0,1,2], default=None)
    ap.add_argument("--scale-units", choices=["degwidth", "arcminwidth", "arcsecperpix"], default=None)
    ap.add_argument("--scale-type", choices=["ul", "ev"], default=None)
    ap.add_argument("--scale-lower", type=float, default=None)
    ap.add_argument("--scale-upper", type=float, default=None)
    ap.add_argument("--scale-est", type=float, default=None)
    ap.add_argument("--scale-err", type=float, default=None)
    ap.add_argument("--center-ra", type=float, default=None)
    ap.add_argument("--center-dec", type=float, default=None)
    ap.add_argument("--radius", type=float, default=None)
    ap.add_argument("--keep-wcs", action="store_true")
    ap.add_argument("--wcs-dir", default="wcs")
    ap.add_argument("--patch-json", action="store_true")
    ap.add_argument("--patch-dir", default="patches")
    ap.add_argument("--patch-grid", type=int, default=16)
    args = ap.parse_args()

    files = [p for p in read_input_paths(args.input, recursive=args.recursive) if p.suffix.lower() in IMAGE_EXTS]
    if not files:
        print("No supported image files found.", file=sys.stderr)
        sys.exit(1)

    upload_args = {
        "publicly_visible": args.publicly_visible,
        "allow_commercial_use": args.allow_commercial_use,
        "allow_modifications": args.allow_modifications,
        "downsample_factor": args.downsample_factor,
        "tweak_order": args.tweak_order,
        "use_sextractor": args.use_sextractor,
        "parity": args.parity,
        "scale_units": args.scale_units,
        "scale_type": args.scale_type,
        "scale_lower": args.scale_lower,
        "scale_upper": args.scale_upper,
        "scale_est": args.scale_est,
        "scale_err": args.scale_err,
        "center_ra": args.center_ra,
        "center_dec": args.center_dec,
        "radius": args.radius,
        "crpix_center": True,
    }

    fieldnames = [
        "file", "mtime", "width_px", "height_px", "submission_id", "job_id",
        "center_ra_deg", "center_dec_deg", "pixscale_arcsec_per_px",
        "extent_width_deg", "extent_height_deg", "orientation_deg", "radius_deg",
        "solve_status", "saved_wcs_file", "patch_json_file", "error"
    ]

    with open(args.output, "w", newline="", encoding="utf-8") as out_f, open(args.failures_output, "w", newline="", encoding="utf-8") as fail_f:
        writer = csv.DictWriter(out_f, fieldnames=fieldnames)
        fail_writer = csv.DictWriter(fail_f, fieldnames=fieldnames)
        writer.writeheader()
        fail_writer.writeheader()

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [
                ex.submit(
                    solve_one, str(p), args.api_key, upload_args, args.poll_seconds,
                    args.submit_delay, args.max_wait_seconds, args.keep_wcs,
                    args.wcs_dir, args.patch_json, args.patch_dir, args.patch_grid
                )
                for p in files
            ]
            for n, fut in enumerate(as_completed(futs), start=1):
                row = fut.result()
                writer.writerow(row)
                out_f.flush()
                if row["solve_status"] != "solved":
                    fail_writer.writerow(row)
                    fail_f.flush()
                print(f"[{n}/{len(files)}] {row['solve_status']:>6} {row['file']}", flush=True)


if __name__ == "__main__":
    main()
