# Coursera CS Course Scraper

Scrapes Computer Science domain courses from the Coursera Career Academy PLUS+ catalog, collecting rich metadata including skills, tools, module structure, video/reading/assignment counts, learning objectives, and course ratings.

**Built for:** ITCS Course Recommendation System (UNC Charlotte)  
**Author:** Devanand Nagendrababu

---

## What It Collects

For each of ~2,635 CS courses:

| Field | Source |
|---|---|
| Course name, URL, slug | Excel catalog |
| Skills (e.g. Python, SQL) | Coursera page HTML |
| Tools (e.g. TensorFlow, Docker) | Coursera page HTML |
| Learning objectives | Coursera API |
| Module titles + descriptions | Coursera API |
| Video / Reading / Assignment counts | Coursera API |
| Total duration (minutes) | Coursera API |
| Course rating | Coursera page HTML |
| Instructor names | Coursera API |
| Difficulty level | Coursera API |

---

## Repository Structure

```
coursera_scraper/
├── cs_scraper.py                        # Main scraper (v3)
├── debug_api.py                         # API endpoint explorer/debugger
├── run_cs_full.sh                       # SLURM job — full run from scratch
├── run_cs_resume.sh                     # SLURM job — resume after CAUTH expiry
├── run_cs_test.sh                       # SLURM job — test run (4 courses)
├── requirements.txt                     # Python dependencies
├── Coursera Career Academy PLUS+ Catalog.xlsx   # Input catalog (not in repo — see below)
├── cs_dataset.json                      # Output: full nested data (generated)
├── cs_dataset.csv                       # Output: flat CSV (generated)
└── cs_checkpoint.json                   # Checkpoint for resuming (generated)
```

> **Note:** The Excel catalog file and output data files are not committed to the repository due to size. See the **Input File** section below.

---

## Prerequisites

### 1. Python Dependencies

```bash
pip install -r requirements.txt
```

Or manually:

```bash
pip install requests beautifulsoup4 openpyxl pandas lxml
```

### 2. Coursera CAUTH Cookie

The scraper requires a valid Coursera session cookie (`CAUTH`) to access course content and APIs.

**How to get it:**
1. Open Chrome and go to [https://www.coursera.org](https://www.coursera.org)
2. Log in to your Coursera account
3. Open DevTools: `F12` (Windows/Linux) or `Cmd+Option+I` (Mac)
4. Go to **Application** tab → **Cookies** → **https://www.coursera.org**
5. Find the cookie named `CAUTH` and copy its value (it is ~400+ characters long)

> **Important:** The CAUTH cookie expires after a few hours. If scraping is interrupted with 403 errors, get a fresh CAUTH and resume using `run_cs_resume.sh`.

### 3. Input File

The scraper reads from the **Coursera Career Academy PLUS+ Catalog.xlsx** file.

This file contains three sheets:
- `CA+ 825` — 2,450 CS courses (header on row 3)
- `CA+ Guided Projects` — 456 CS courses (header on row 3)
- `ZzIndustry Specializations` — 184 CS courses (no URL column, skipped)

Total after deduplication: **~2,635 courses**

Place this file in the same directory as `cs_scraper.py`.

---

## Running on UNC Charlotte HPC Cluster

### Step 1 — SSH into the cluster

```bash
ssh dnagendr@hpc.charlotte.edu
# Enter password + Duo 2FA
```

### Step 2 — Upload files from your Mac

Run this from your **local Mac terminal** (not SSH):

```bash
scp cs_scraper.py run_cs_full.sh run_cs_resume.sh run_cs_test.sh requirements.txt \
    "Coursera Career Academy PLUS+ Catalog.xlsx" \
    dnagendr@hpc.charlotte.edu:~/coursera_scraper/
```

### Step 3 — Set up on cluster

```bash
cd ~/coursera_scraper
pip install --user -r requirements.txt
```

### Step 4 — Add your CAUTH cookie

```bash
nano run_cs_full.sh
# Replace PASTE_YOUR_FRESH_CAUTH_HERE with your actual CAUTH value
# Save: Ctrl+O → Enter → Ctrl+X
```

### Step 5 — Submit the job

```bash
sbatch run_cs_full.sh
squeue -u dnagendr        # check job status
```

### Step 6 — Monitor progress

```bash
# Watch live log
tail -f slurm_cs_full_<JOBID>.log

# Check how many courses are done
python3 -c "
import json
cp = json.load(open('cs_checkpoint.json'))
print('Done:', len(cp.get('completed_slugs', [])), '/ 2635')
"
```

### Step 7 — If CAUTH expires mid-run

The scraper automatically detects CAUTH expiry (5 consecutive 403 errors) and stops cleanly, saving all progress to `cs_checkpoint.json`.

To resume:

```bash
nano run_cs_resume.sh     # paste fresh CAUTH
sbatch run_cs_resume.sh
```

Repeat until all 2,635 courses are done.

### Step 8 — Download results to your Mac

```bash
# Run from local Mac terminal
scp dnagendr@hpc.charlotte.edu:~/coursera_scraper/cs_dataset.json ~/Downloads/
scp dnagendr@hpc.charlotte.edu:~/coursera_scraper/cs_dataset.csv ~/Downloads/
```

---

## Running Locally (Mac/Linux)

If the HPC cluster's IP is blocked by Coursera, run the scraper locally instead:

```bash
python3 cs_scraper.py \
  --excel "Coursera Career Academy PLUS+ Catalog.xlsx" \
  --cauth "YOUR_CAUTH_HERE" \
  --workers 1 \
  --delay 3.0
```

To resume a previous run:

```bash
python3 cs_scraper.py \
  --excel "Coursera Career Academy PLUS+ Catalog.xlsx" \
  --cauth "YOUR_CAUTH_HERE" \
  --workers 1 \
  --delay 3.0 \
  --resume
```

---

## Command Line Arguments

| Argument | Default | Description |
|---|---|---|
| `--excel` | required | Path to the Coursera catalog Excel file |
| `--cauth` | required | Coursera CAUTH session cookie value |
| `--workers` | 3 | Number of parallel worker threads |
| `--delay` | 2.0 | Seconds to wait between requests |
| `--resume` | False | Resume from existing checkpoint |
| `--test` | False | Test mode — scrape only 3 courses per sheet |
| `--output` | `cs_dataset` | Output filename prefix |

---

## Output Files

### cs_dataset.json

Nested JSON array. Each entry contains:

```json
{
  "slug": "machine-learning",
  "scraped_name": "Machine Learning Specialization",
  "final_skills": ["Regression Analysis", "Unsupervised Learning"],
  "final_tools": ["Python", "NumPy", "Scikit Learn"],
  "learning_objectives": ["Build ML models", "Apply supervised learning"],
  "total_modules": 12,
  "total_videos": 98,
  "total_readings": 22,
  "total_assignments": 18,
  "total_duration_minutes": 1842,
  "course_rating": 4.9,
  "instructor_names": ["Andrew Ng"],
  "level": "BEGINNER",
  "scrape_status": "success",
  "modules": [
    {
      "title": "Introduction to Machine Learning",
      "description": "...",
      "videos": [...],
      "readings": [...],
      "assignments": [...]
    }
  ]
}
```

### cs_dataset.csv

Flat CSV with all scalar fields. List fields (skills, tools, objectives) are pipe-separated (`|`).

### cs_checkpoint.json

Internal checkpoint file used for resuming. Contains list of completed slugs and all results so far. Saved after every course.

---

## How the Scraper Works

The scraper uses three data sources for each course:

1. **`onDemandCourses.v1` API** — course ID, level, instructor IDs, domain
2. **`onDemandCourseMaterials.v2` API** — full module/lesson/item tree (requires CAUTH)
3. **Page HTML** — skills, tools, course rating (parsed with BeautifulSoup)

Item types (video/reading/assignment) are inferred from item slugs since the API does not return a type field directly.

---

## Known Limitations

- **3rd-party courses** (EDUCBA, Packt, BoardInfinity) return module titles but no item-level data — these are marked `api_no_items`
- **Instructor ratings** are not available via the Coursera API — only names are collected
- **CAUTH expiry** — Coursera invalidates session cookies faster when scraping from data center IPs. Running locally on a home IP is more reliable.
- **ZzIndustry Specializations** sheet has no URL/slug column and is skipped (`skipped_no_slug`)

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `HTTP 403` errors immediately | CAUTH is expired — get a fresh one from Chrome |
| `cs_checkpoint.json not found` | No courses completed yet — check the log for errors |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` |
| Job cancelled by SLURM | Time limit hit — resume with `run_cs_resume.sh` |
| All courses showing `failed` | CAUTH was invalid from the start — recheck the cookie value |