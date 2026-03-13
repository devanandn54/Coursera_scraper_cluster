#!/usr/bin/env python3
"""
submit_cs_scraper.py
====================
Follows Professor Cheng's exact pattern from slurm_4get_wikidata_subnet.py:
  - Generate one .slurm file per chunk
  - Submit all chunks simultaneously via sbatch
  - Each chunk runs on a DIFFERENT NODE = different IP
  - No threading inside jobs (parallelism via many jobs, not threads)

50 jobs x 53 courses = all 2635 courses scraped in parallel
Each node gets its own IP quota from Coursera.

Usage:
    python3 submit_cs_scraper.py <CAUTH>              # submit 50 parallel jobs
    python3 submit_cs_scraper.py "" --merge           # merge after all done
    python3 submit_cs_scraper.py <CAUTH> --chunks 20  # custom chunk count
"""

import os, sys, json, subprocess

# ── Config (mirrors professor's hardcoded paths) ───────────────────────
CUR_PATH = "/users/dnagendr/coursera_scraper"
EXCEL    = f"{CUR_PATH}/Coursera Career Academy PLUS+ Catalog.xlsx"
N_CHUNKS = 50    # 50 jobs = 50 different nodes = 50 different IPs
DELAY    = 3.0   # seconds between requests per job
# ───────────────────────────────────────────────────────────────────────

def submit_chunks(cauth, n_chunks):
    slurm_dir = f"{CUR_PATH}/submit_slurm"
    log_dir   = f"{CUR_PATH}/logs"
    os.makedirs(slurm_dir, exist_ok=True)
    os.makedirs(log_dir,   exist_ok=True)

    job_ids = []
    for i in range(n_chunks):
        label      = f"{i:02d}of{n_chunks:02d}"
        slurm_file = f"{slurm_dir}/cs_chunk{label}.slurm"

        # Professor's exact SLURM template structure
        content = f"""#!/bin/bash
#SBATCH --partition=Orion
#SBATCH --job-name=cs_chunk{i:02d}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --time=02:00:00

echo "======================================================"
echo "Start Time  : $(date)"
echo "Submit Dir  : $SLURM_SUBMIT_DIR"
echo "Job ID/Name : $SLURM_JOBID / $SLURM_JOB_NAME"
echo "Num Tasks   : $SLURM_NTASKS total [$SLURM_NNODES nodes @ $SLURM_CPUS_ON_NODE CPUs/node]"
echo "Node        : $(hostname)"
echo "Node IP     : $(hostname -I | awk '{{print $1}}')"
echo "Chunk       : {i} of {n_chunks}"
echo "======================================================"

cd $SLURM_SUBMIT_DIR
source {CUR_PATH}/scraper_venv/bin/activate

python3 {CUR_PATH}/cs_scraper.py \\
    --excel       "{EXCEL}" \\
    --cauth       "{cauth}" \\
    --workers     1 \\
    --delay       {DELAY} \\
    --chunk-index {i} \\
    --chunk-total {n_chunks}

echo "Finished chunk {i} of {n_chunks}"

echo "======================================================"
echo "End Time : $(date)"
echo "======================================================"
"""
        with open(slurm_file, "w") as f:
            f.write(content)
        print(f"Created {slurm_file}")

        # Submit — exactly like professor's submit_4get_wikidata_subnet.sh
        result = subprocess.run(["sbatch", slurm_file],
                                capture_output=True, text=True)
        out = result.stdout.strip()
        print(f"  --> {out}")
        if result.returncode == 0:
            job_ids.append(out.split()[-1])

    print(f"\n{'='*60}")
    print(f"Submitted {len(job_ids)}/{n_chunks} jobs")
    print(f"Each job runs on a different node (different IP)")
    print(f"Expected completion: ~30-60 minutes")
    print(f"\nMonitor:")
    print(f"  squeue -u dnagendr")
    print(f"  watch -n30 'squeue -u dnagendr | wc -l'")
    print(f"\nCheck progress of one chunk:")
    print(f"  tail -f {log_dir}/cs_chunk00of{n_chunks:02d}_*.log")
    print(f"\nWhen ALL jobs finish:")
    print(f"  python3 {CUR_PATH}/submit_cs_scraper.py \"\" --merge --chunks {n_chunks}")


def merge_outputs(n_chunks):
    """Merge all chunk outputs — run after all jobs complete"""
    try:
        import pandas as pd
    except ImportError:
        print("Run: source scraper_venv/bin/activate first")
        sys.exit(1)

    all_results, seen = [], set()

    print(f"Merging {n_chunks} chunks...")
    for i in range(n_chunks):
        label = f"_chunk{i:02d}of{n_chunks:02d}"
        found = False

        # Try final JSON first, then checkpoint
        for fname in [f"{CUR_PATH}/cs_dataset{label}.json",
                      f"{CUR_PATH}/cs_checkpoint{label}.json"]:
            if os.path.exists(fname):
                with open(fname) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data = data.get("results", [])
                added = 0
                for r in data:
                    slug = r.get("slug", "")
                    if slug and slug not in seen:
                        all_results.append(r)
                        seen.add(slug)
                        added += 1
                success = sum(1 for r in data
                              if r.get("scrape_status") == "success")
                print(f"  Chunk {i:02d}: {added} courses, {success} success")
                found = True
                break

        if not found:
            print(f"  Chunk {i:02d}: NO OUTPUT — may still be running or failed")

    if not all_results:
        print("No results found. Are jobs still running? Check: squeue -u dnagendr")
        return

    # Save final merged JSON
    out_json = f"{CUR_PATH}/cs_dataset_final.json"
    with open(out_json, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    # Save final merged CSV
    out_csv = f"{CUR_PATH}/cs_dataset_final.csv"
    rows = []
    for r in all_results:
        rows.append({
            "slug":               r.get("slug", ""),
            "name":               r.get("scraped_name") or r.get("xlsx_name", ""),
            "status":             r.get("scrape_status", ""),
            "skills":             "|".join(r.get("final_skills", [])),
            "tools":              "|".join(r.get("final_tools", [])),
            "objectives":         "|".join(r.get("learning_objectives", [])),
            "level":              r.get("level", ""),
            "rating":             r.get("course_rating", ""),
            "instructors":        "|".join(r.get("instructor_names", [])),
            "total_modules":      r.get("total_modules", 0),
            "total_videos":       r.get("total_videos", 0),
            "total_readings":     r.get("total_readings", 0),
            "total_assignments":  r.get("total_assignments", 0),
            "total_duration_min": r.get("total_duration_minutes", 0),
        })
    pd.DataFrame(rows).to_csv(out_csv, index=False)

    success_total = sum(1 for r in all_results
                        if r.get("scrape_status") == "success")
    failed_total  = sum(1 for r in all_results
                        if r.get("scrape_status") == "failed")
    print(f"\n{'='*60}")
    print(f"MERGED RESULTS:")
    print(f"  Total courses : {len(all_results)}")
    print(f"  Success       : {success_total}")
    print(f"  Failed        : {failed_total}")
    print(f"  Output JSON   : {out_json}")
    print(f"  Output CSV    : {out_csv}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cauth    = sys.argv[1]
    n_chunks = N_CHUNKS
    merge    = "--merge" in sys.argv

    for i, arg in enumerate(sys.argv):
        if arg == "--chunks" and i + 1 < len(sys.argv):
            n_chunks = int(sys.argv[i + 1])

    if merge:
        merge_outputs(n_chunks)
    else:
        if not cauth:
            print("ERROR: CAUTH is required")
            sys.exit(1)
        submit_chunks(cauth, n_chunks)


if __name__ == "__main__":
    main()
