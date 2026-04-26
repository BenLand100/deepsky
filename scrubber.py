#!/bin/env python3

import pandas as pd
import os
from pathlib import Path

# Load the CSV file
df = pd.read_csv('results_enriched.csv')

# Remove rows where solve_status != 'solved'
df = df[df['solve_status'] == 'solved'].copy()

# Extract basename from 'file' column (e.g., 'images/2023-11-13T17.12.05.png' -> '2023-11-13T17.12.05.png')
df['basename'] = df['file'].apply(lambda x: Path(x).name)

# Check if basename exists in thumbs/ directory
thumbs_dir = Path('thumbs')
df['thumb_exists'] = df['basename'].apply(lambda b: (thumbs_dir / b).exists())

# Retain only rows where thumb exists
df_filtered = df[df['thumb_exists']].copy()
df_filtered.drop(['basename', 'thumb_exists'], axis=1, inplace=True)

# Save filtered CSV to ./
output_path = Path('.') / 'live_images.csv'
output_path.parent.mkdir(exist_ok=True)
df_filtered.to_csv(output_path, index=False)

print(f"Original rows: {len(df)} after solve_status filter")
print(f"Filtered rows (thumb exists): {len(df_filtered)}")
print(f"Saved to: {output_path}")
