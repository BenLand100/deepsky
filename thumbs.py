#!/usr/bin/env python3
import argparse
from pathlib import Path
from PIL import Image, ImageOps

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp'}


def iter_images(folder: Path, recursive=False):
    if recursive:
        yield from (p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    else:
        yield from (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


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


def main():
    ap = argparse.ArgumentParser(description='Generate thumbnail JPEGs for sky patch textures')
    ap.add_argument('input', help='Input image directory')
    ap.add_argument('--output-dir', default='thumbs', help='Thumbnail output directory')
    ap.add_argument('--recursive', action='store_true')
    ap.add_argument('--max-size', type=int, default=1024, help='Max width/height in pixels, default 512')
    ap.add_argument('--quality', type=int, default=88, help='JPEG quality, default 88')
    args = ap.parse_args()

    src_dir = Path(args.input)
    out_dir = Path(args.output_dir)
    files = sorted(iter_images(src_dir, recursive=args.recursive))
    if not files:
        raise SystemExit('No images found')

    for i, src in enumerate(files, start=1):
        rel = src.relative_to(src_dir)
        dst = out_dir / rel
        out = make_thumb(src, dst, args.max_size, args.quality)
        print(f'[{i}/{len(files)}] {src} -> {out}', flush=True)


if __name__ == '__main__':
    main()
