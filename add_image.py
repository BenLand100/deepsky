#!/usr/bin/env python3
import argparse
import csv
import json
import mimetypes
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image, ImageOps
from astropy.io import fits
from astropy.wcs import WCS
import astropy.wcs.utils
from astropy.coordinates import Angle
import astropy.units as u

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
    if upload_args.get("center_ra") is not None:
        payload["center_ra"] = float(upload_args["center_ra"])
    if upload_args.get("center_dec") is not None:
        payload["center_dec"] = float(upload_args["center_dec"])
    if upload_args.get("radius") is not None:
        payload["radius"] = float(upload_args["radius"])
        
    for k in ["scale_type", "scale_units", "scale_lower", "scale_upper"]:
        if upload_args.get(k) is not None:
            payload[k] = upload_args[k]
            
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

def make_thumb(src: Path, dst: Path, max_size: int, quality: int):
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ('RGB', 'L'):
            im = im.convert('RGB')
        else:
            im = im.convert('RGB')
        im.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        dst = dst.with_suffix('.jpg')
        im.save(dst, 'JPEG', quality=quality, optimize=True, progressive=True, subsampling=1)
        return dst

def get_last_metadata(csv_file):
    last_row = {}
    if os.path.exists(csv_file):
        with open(csv_file, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                last_row = row
    return last_row

def prompt(message, default=''):
    if default:
        msg = f"{message} [{default}]: "
    else:
        msg = f"{message}: "
    val = input(msg).strip()
    return val if val else default

def solve_local(image_path, wcs_path, ra=None, dec=None, radius=None, scale_low=None, scale_high=None, scale_units="degwidth", timeout=60.0):
    image_path = Path(image_path).resolve()
    wcs_path = Path(wcs_path).resolve()
    timeout_val = timeout if (timeout is not None and timeout > 0) else None
    print(f"Solving {image_path} locally with solve-field" + (f" (timeout: {timeout_val:g}s)..." if timeout_val else "..."))
    with tempfile.TemporaryDirectory(prefix=".solve_field_", dir=image_path.parent) as tmp_dir:
        cmd = [
            "solve-field",
            "--dir", tmp_dir,
            "--wcs", str(wcs_path),
            "--overwrite",
            "--no-plots"
        ]
        if ra is not None:
            cmd.extend(["--ra", str(ra)])
        if dec is not None:
            cmd.extend(["--dec", str(dec)])
        if radius is not None:
            cmd.extend(["--radius", str(radius)])
        if scale_low is not None:
            cmd.extend(["--scale-low", str(scale_low)])
        if scale_high is not None:
            cmd.extend(["--scale-high", str(scale_high)])
        if scale_low is not None or scale_high is not None:
            cmd.extend(["--scale-units", scale_units])

        cmd.append(str(image_path))

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout_val)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                proc.wait()
            raise TimeoutError(f"solve-field timed out after {timeout_val:g}s.")

        if not wcs_path.exists():
            print(stdout)
            raise RuntimeError("solve-field failed to produce a WCS file.")
    print("Local solve-field succeeded.")

def solve_api(image_path, wcs_path, api_key, ra=None, dec=None, radius=None, scale_low=None, scale_high=None, scale_units="degwidth"):
    print(f"Solving {image_path} via astrometry.net API...")
    http = requests.Session()
    http.headers.update({"User-Agent": "astrometry-batch-curved/1.0"})
    session_key = login(http, api_key)
    
    upload_args = {
        "publicly_visible": "n",
        "allow_commercial_use": "d",
        "allow_modifications": "d",
        "downsample_factor": 2,
        "tweak_order": 2,
        "crpix_center": True,
        "center_ra": ra,
        "center_dec": dec,
        "radius": radius
    }
    if scale_low is not None or scale_high is not None:
        upload_args["scale_type"] = "ul"
        upload_args["scale_units"] = scale_units
        if scale_low is not None:
            upload_args["scale_lower"] = scale_low
        if scale_high is not None:
            upload_args["scale_upper"] = scale_high
    subid = upload_file(http, session_key, str(image_path), upload_args)
    print(f"Uploaded. Submission ID: {subid}")
    time.sleep(2)
    jobid, _ = wait_for_job(http, subid, poll_seconds=10)
    print(f"Job ID: {jobid}. Waiting for calibration...")
    cal = wait_for_calibration(http, jobid, poll_seconds=10)
    
    fetch_job_file(http, jobid, "wcs_file", wcs_path)
    print("API solve succeeded.")
    return cal, subid, jobid

def parse_ra(val):
    if val is None: return None
    try:
        return float(val)
    except ValueError:
        return Angle(val.replace('.', ':'), unit=u.hourangle).deg

def parse_dec(val):
    if val is None: return None
    try:
        return float(val)
    except ValueError:
        return Angle(val.replace('.', ':'), unit=u.deg).deg

def parse_exposure_arg(val):
    parts = val.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            f"Invalid --exposure '{val}': expected CHANNEL:COUNT:EXPTIME (e.g. Ha:20:300)"
        )
    channel, count, exptime = parts[0].strip(), parts[1].strip(), parts[2].strip()
    valid_channels = {"OSC", "L", "R", "G", "B", "Sii", "Ha", "Oiii"}
    channel_map = {c.lower(): c for c in valid_channels}
    if channel.lower() not in channel_map:
        raise argparse.ArgumentTypeError(
            f"Invalid exposure channel '{channel}': must be one of {sorted(valid_channels)}"
        )
    return channel_map[channel.lower()], count, exptime

def main():
    ap = argparse.ArgumentParser(description="Add a new image to DeepSky")
    ap.add_argument("image", help="Path to the image to add")
    ap.add_argument("--api-key", help="Astrometry.net API key (uses API only unless --api-fallback is set)")
    ap.add_argument("--api-fallback", action="store_true", help="Try local solve-field first, falling back to astrometry.net API on failure or timeout (requires --api-key)")
    ap.add_argument("--local-timeout", type=float, default=60.0, help="Max seconds for local solve-field before timing out (default: 60, <=0 disables)")
    ap.add_argument("--ra", type=str, help="Right Ascension hint in degrees (float) or HH:MM:SS / HH.MM.SS")
    ap.add_argument("--dec", type=str, help="Declination hint in degrees (float) or DD:MM:SS / DD.MM.SS")
    ap.add_argument("--radius", type=float, default=2.0, help="Search radius around RA/DEC hint in degrees (default: 2.0)")
    ap.add_argument("--scale-low", type=float, help="Lower bound of image scale estimate")
    ap.add_argument("--scale-high", type=float, help="Upper bound of image scale estimate")
    ap.add_argument("--scale-units", type=str, choices=["degwidth", "arcminwidth", "arcsecperpix"], default="degwidth", help="Units for scale bounds (default: degwidth)")
    ap.add_argument("--telescope", type=str, default=None, help="Telescope name (bypasses prompt)")
    ap.add_argument("--camera", type=str, default=None, help="Camera name (bypasses prompt)")
    ap.add_argument("--processor", type=str, default=None, help="Processor name (bypasses prompt)")
    ap.add_argument("--image-type", type=str, default=None, help="Image type, e.g. OSC, LRGB, SHO+RGB (bypasses prompt)")
    ap.add_argument("--catalog-id", type=str, default=None, help="Catalog identifier, e.g. M42 (bypasses prompt)")
    ap.add_argument("--common-name", type=str, default=None, help="Common name, e.g. Orion Nebula (bypasses prompt)")
    ap.add_argument("--osc-filter", type=str, default=None, help="OSC filter name (optional)")
    ap.add_argument("--exposure", action="append", type=parse_exposure_arg, default=[], help="Channel exposure as CHANNEL:COUNT:EXPTIME (repeatable, e.g. --exposure Ha:20:300)")
    ap.add_argument("-y", "--non-interactive", action="store_true", help="Never prompt; use provided values or defaults")
    transfer_grp = ap.add_mutually_exclusive_group()
    transfer_grp.add_argument("--move", action="store_true", help="Move source image into images/ after updating CSV (default for images in stage/)")
    transfer_grp.add_argument("--copy", action="store_true", help="Copy source image into images/ after updating CSV (default outside stage/)")
    args = ap.parse_args()
    if args.api_fallback and not args.api_key:
        ap.error("--api-fallback requires --api-key")
    
    parsed_ra = parse_ra(args.ra)
    parsed_dec = parse_dec(args.dec)

    src_img = Path(args.image)
    if not src_img.exists():
        print(f"Error: {src_img} does not exist.")
        sys.exit(1)

    # Use the script's directory as the base deepsky directory
    base_dir = Path(__file__).resolve().parent
    csv_file = base_dir / "live_images.csv"
    images_dir = base_dir / "images"
    thumbs_dir = base_dir / "thumbs"
    wcs_dir = base_dir / "wcs"
    patches_dir = base_dir / "patches"

    # Ensure dirs exist
    for d in [images_dir, thumbs_dir, wcs_dir, patches_dir]:
        d.mkdir(parents=True, exist_ok=True)

    dest_img = images_dir / src_img.name
    stem = dest_img.stem
    wcs_path = wcs_dir / f"{stem}.wcs"
    patch_path = patches_dir / f"{stem}.patch.json"
    thumb_path = thumbs_dir / f"{stem}.jpg"

    last_meta = get_last_metadata(csv_file)
    fieldnames = list(last_meta.keys()) if last_meta else [
        "file", "mtime", "width_px", "height_px", "submission_id", "job_id",
        "center_ra_deg", "center_dec_deg", "pixscale_arcsec_per_px",
        "extent_width_deg", "extent_height_deg", "orientation_deg", "radius_deg",
        "solve_status", "saved_wcs_file", "patch_json_file", "error",
        "Telescope", "Camera", "Processor", "Image Type", "catalog_identifier", "common_name"
    ]

    def resolve_meta(label, cli_val, default=''):
        if cli_val is not None:
            return cli_val
        if args.non_interactive:
            return default
        return prompt(label, default)

    meta = {}
    need_prompt = (not args.non_interactive) and any(
        v is None for v in (
            args.telescope, args.camera, args.processor,
            args.image_type, args.catalog_id, args.common_name
        )
    )
    if need_prompt:
        print("\n--- Metadata ---")
    meta["Telescope"] = resolve_meta("Telescope", args.telescope, last_meta.get("Telescope", ""))
    meta["Camera"] = resolve_meta("Camera", args.camera, last_meta.get("Camera", ""))
    meta["Processor"] = resolve_meta("Processor", args.processor, last_meta.get("Processor", "Siril"))
    meta["Image Type"] = resolve_meta("Image Type", args.image_type, last_meta.get("Image Type", "OSC"))
    meta["catalog_identifier"] = resolve_meta("Catalog Identifier (e.g. M42)", args.catalog_id, "")
    meta["common_name"] = resolve_meta("Common Name (e.g. Orion Nebula)", args.common_name, "")
    if args.osc_filter is not None:
        meta["OSC filter"] = args.osc_filter
    for channel, count, exptime in args.exposure:
        meta[f"{channel} total images"] = count
        meta[f"{channel} exptime"] = exptime

    subid = ""
    jobid = ""
    center_ra = ""
    center_dec = ""
    pixscale = ""
    radius = ""
    orientation = ""
    
    solve_radius = args.radius if (parsed_ra is not None or parsed_dec is not None) else None
    use_api = bool(args.api_key and not args.api_fallback)

    if not use_api:
        try:
            solve_local(
                src_img, wcs_path,
                ra=parsed_ra, dec=parsed_dec, radius=solve_radius,
                scale_low=args.scale_low, scale_high=args.scale_high, scale_units=args.scale_units,
                timeout=args.local_timeout
            )
        except (TimeoutError, RuntimeError) as e:
            if args.api_fallback and args.api_key:
                print(f"Local solve failed ({e}). Falling back to astrometry.net API...")
                use_api = True
            else:
                raise

    if use_api:
        cal, subid, jobid = solve_api(
            src_img, wcs_path, args.api_key,
            ra=parsed_ra, dec=parsed_dec, radius=solve_radius,
            scale_low=args.scale_low, scale_high=args.scale_high, scale_units=args.scale_units
        )
        center_ra = cal.get("ra", "")
        center_dec = cal.get("dec", "")
        pixscale = cal.get("pixscale", "")
        radius = cal.get("radius", "")
        orientation = cal.get("orientation", "")
        
    width_px, height_px = image_size(src_img)

    # Always extract from WCS if local, or missing
    if not (center_ra and center_dec and pixscale):
        header = fits.getheader(str(wcs_path))
        w = WCS(header)
        center = w.pixel_to_world(width_px / 2, height_px / 2)
        center_ra = float(center.ra.deg)
        center_dec = float(center.dec.deg)
        
        pixscale_deg = astropy.wcs.utils.proj_plane_pixel_scales(w)[0]
        pixscale = pixscale_deg * 3600
        
        c1 = w.pixel_to_world(0, 0)
        c2 = w.pixel_to_world(width_px, height_px)
        radius = float(c1.separation(c2).deg / 2)

    wdeg, hdeg = approx_extent_deg(width_px, height_px, float(pixscale))
    
    print(f"Exporting patch JSON to {patch_path}...")
    export_patch_json(wcs_path, width_px, height_px, patch_path, grid=16)
    
    print(f"Generating thumbnail to {thumb_path}...")
    make_thumb(src_img, thumb_path, 1024, 88)
    
    row = {
        "file": f"images/{dest_img.name}",
        "mtime": iso_mtime(src_img),
        "width_px": width_px,
        "height_px": height_px,
        "submission_id": subid,
        "job_id": jobid,
        "center_ra_deg": center_ra,
        "center_dec_deg": center_dec,
        "pixscale_arcsec_per_px": pixscale,
        "extent_width_deg": wdeg,
        "extent_height_deg": hdeg,
        "orientation_deg": orientation,
        "radius_deg": radius,
        "solve_status": "solved",
        "saved_wcs_file": f"wcs/{wcs_path.name}",
        "patch_json_file": f"patches/{patch_path.name}",
        "error": "",
        "Telescope": meta["Telescope"],
        "Camera": meta["Camera"],
        "Processor": meta["Processor"],
        "Image Type": meta["Image Type"],
        "catalog_identifier": meta["catalog_identifier"],
        "common_name": meta["common_name"]
    }
    for k, v in meta.items():
        if k not in row:
            row[k] = v
    
    for k in row.keys():
        if k not in fieldnames:
            fieldnames.append(k)
            
    file_exists = csv_file.exists()
    with open(csv_file, "a", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        
        out_row = {f: row.get(f, "") for f in fieldnames}
        writer.writerow(out_row)

    if src_img.resolve() != dest_img.resolve():
        stage_dir = (base_dir / "stage").resolve()
        in_stage = src_img.resolve().parent == stage_dir
        should_move = args.move or (in_stage and not args.copy)
        if should_move:
            print(f"Moving {src_img} to {dest_img}...")
            shutil.move(str(src_img), str(dest_img))
        else:
            print(f"Copying {src_img} to {dest_img}...")
            shutil.copy2(src_img, dest_img)

    print(f"Successfully added {dest_img.name} to {csv_file.name}")

if __name__ == '__main__':
    main()
