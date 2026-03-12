#!/bin/bash
#SBATCH --job-name=cs_full
#SBATCH --output=slurm_cs_full_%j.log
#SBATCH --time=12:00:00
#SBATCH --mem=8G
#SBATCH --cpus-per-task=2

EXCEL="Coursera Career Academy PLUS+ Catalog.xlsx"
CAUTH="PASTE_YOUR_FRESH_CAUTH_HERE"

echo "======================================================"
echo "Coursera CS Resume Scrape"
echo "Started: $(date)"
echo "Node: $(hostname)"
echo "======================================================"

module load python/3.11 2>/dev/null \
  || module load python/3.10 2>/dev/null \
  || module load python/3.9  2>/dev/null \
  || module load python3     2>/dev/null \
  || true

echo "Python: $(python3 --version 2>&1)"

# Show checkpoint status before starting
python3 -c "
import json, sys
try:
    cp = json.load(open('cs_checkpoint.json'))
    done = len(cp.get('completed_slugs', []))
    print(f'Resuming from checkpoint: {done} / 2635 courses already done')
except:
    print('No checkpoint found — starting fresh')
"

pip install --user -r requirements.txt -q 2>&1 | tail -2

python3 cs_scraper.py \
  --excel   "$EXCEL" \
  --cauth   "$CAUTH" \
  --workers 1 \
  --delay   3.0 \
  --resume

echo "Finished: $(date)"
ls -lh cs_dataset.json cs_dataset.csv cs_checkpoint.json 2>/dev/null
