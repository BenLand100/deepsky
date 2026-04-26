# Deep Sky

This is a single-page gallery for Deep Sky images that displays the images on a celestial sphere, allowing the viewer to navigate interactively and appreciate relative sizes of images.
It is based on Three.js in realtime and leverages Astrometry.net in initial setup for platesolving.
See [https://ben.land/deepsky/](https://ben.land/deepsky/) for a live demo.

## Initial Setup

Either AI, prior organization, or significant diligence will be needed to get this setup. 
Assuming you have a set of post-processed images (web-ready jpeg recommended), place them in a folder called `images`.
The script `platesolve.py` can then be used to generate the `patches` with transformation and `results.csv` with platesolving by using Astrometry.net.
```bash
./platesolve.py --patch-json --keep-wcs --api-key [astrometry.net_api_key] images
```
The `results.csv` file will then either need to be augmented or not depending on how much information you want to show about each image.
I used metadata about the images I had processed with Siril
```bash
find ~/astro/ -name r_*_stacked.fit -exec bash -c 'echo $(basename "$1") $(find "$(dirname "$1")" -name \*.jpg -printf "%f:"); fitshdr "$1" | egrep "LIVETIME|GAIN|EXPTIME|STACKCNT|INSTRUM|TELESCOP|FOCAL" ' _ {} \; | tee siril.meta
```
and information about the images processed with WBPP in PixInsight (via the included `xisfhdr.py`)
```bash
./xisfhdr.py  ~/astro/*/*masterLight*.xisf | tee wbpp.meta
```
when asking an AI like Claude or Gemini to generate additional columns on that CSV containing camera, telescope, filter, and exposure information.
You can customize how this is displayed, including removing or adding attributes, in the `panelHtml` function.
Finally run `thumbs.py` to generate thumbnails in the `thumbs` directory, and save your augmented CSV as `live_images.csv`.

## Hosting 

You can either use `python -m http.server` and open `http://localhost:8000/deepsky.html` to view locally or move the following files to a hosted location of your choice:
```
deepsky.html live_images.csv starmap.jpg images thumbs patches
```
optionally renaming `deepsky.html` to `index.html` as desired.
No databases or other services are required.

## Adding New Images

Currently this can be done by repeating the above steps and adding the result to `live_images.csv`. Later a tool will help with this.
