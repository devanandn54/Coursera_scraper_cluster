# Coursera CS Course Scraper — Research Project

**Author:** Devanand Nagendrababu (`dnagendr`)  
**Course:** Research Assistant — Prof. Dr. Qiong Cheng  
**Cluster:** UNC Charlotte HPC (`hpc.charlotte.edu`) — Orion Partition  
**Goal:** Scrape all Computer Science domain courses from the Coursera Career Academy+ catalog to build a course recommendation system.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Data Source — Excel Catalog](#2-data-source--excel-catalog)
3. [What the Scraper Collects](#3-what-the-scraper-collects)
4. [Repository Structure](#4-repository-structure)
5. [Setup on HPC Cluster](#5-setup-on-hpc-cluster)
6. [How to Run](#6-how-to-run)
7. [Scraper Architecture](#7-scraper-architecture)
8. [HPC Deployment Journey — What We Tried](#8-hpc-deployment-journey--what-we-tried)
9. [Root Cause: NAT Gateway Discovery](#9-root-cause-nat-gateway-discovery)
10. [Current Blocker — Help Needed](#10-current-blocker--help-needed)
11. [Proposed Solutions](#11-proposed-solutions)
12. [Output Format](#12-output-format)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Project Overview

This scraper targets **2,635 Computer Science domain courses** from Coursera's Career Academy+ program. It uses Coursera's internal APIs (not the public API) along with authenticated page scraping to collect rich course metadata including learning objectives, skills, tools, module structure, video counts, and instructor information — all fields needed for building a content-based recommendation engine.

The scraper is written in Python 3.9, follows Professor Cheng's HPC deployment pattern (virtual environment + SLURM job generation), and has been extensively tested and debugged over multiple cluster runs.

---

## 2. Data Source — Excel Catalog

**File:** `Coursera Career Academy PLUS+ Catalog.xlsx`

| Sheet | Header Row | Courses | CS Domain | URL Column | Status |
|---|---|---|---|---|---|
| CA+ 825 | Row 3 | 2,450 | ✅ Yes | ✅ Yes | Scraped |
| CA+ Guided Projects | Row 3 | 456 | ✅ Yes | ✅ Yes | Scraped |
| ZzIndustry Specializations | Row 1 | 184 | ✅ Yes | ❌ No URL | Skipped |

**Total after deduplication: 2,635 courses**

The ZzIndustry Specializations sheet has no Course URL column so slugs cannot be derived — these 184 courses are logged as `skipped_no_slug` in the output.

---

## 3. What the Scraper Collects

| Field | API Source | Notes |
|---|---|---|
| Course name | `onDemandCourses.v1` | Verified against Excel |
| Difficulty level | `onDemandCourses.v1` | BEGINNER / INTERMEDIATE / ADVANCED |
| Domain types | `onDemandCourses.v1` | e.g. computer-science |
| Learning objectives | `courses.v1/{id}` | CML-encoded, parsed to plain text |
| Module titles & descriptions | `onDemandCourseMaterials.v2` | Requires CAUTH |
| Item titles & durations | `onDemandCourseMaterials.v2` | Videos, readings, assignments |
| Item type inference | Slug-based rules | video / reading / assignment / discussion |
| Instructor names | `instructors.v1` | Full name list |
| Skills | Page HTML | `[data-testid="skills-section"] a` |
| Tools | Page HTML | `[data-testid="tools-section"] a` |
| Course rating | Page HTML JSON | `avgProductRating` field |
| Total modules / videos / readings / assignments | Computed | Aggregated from materials API |
| Total duration (minutes) | Computed | Sum of item duration fields (ms → min) |

**Fields confirmed NOT available in any API:**
- Instructor rating (not returned by `instructors.v1`)
- Skills/tools (only in page HTML, not in any API endpoint)

---

## 4. Repository Structure

```
coursera_scraper/
├── cs_scraper.py            # Main scraper (v3 — current)
├── submit_cs_scraper.py     # SLURM job generator (professor's pattern)
├── setup_venv.sh            # One-time venv setup script
├── debug_api.py             # API endpoint tester / debugger
├── requirements.txt         # Python dependencies
├── README.md                # This file
├── scraper_venv/            # Virtual environment (created by setup_venv.sh)
├── submit_slurm/            # Auto-generated .slurm files
├── logs/                    # SLURM job logs
└── Coursera Career Academy PLUS+ Catalog.xlsx
```

**Output files** (generated after scraping completes):
```
cs_dataset_final.json        # Full nested JSON for all courses
cs_dataset_final.csv         # Flat CSV for analysis / recommendation engine
cs_checkpoint_chunk*.json    # Per-chunk auto-saved checkpoints (every 1 course)
```

---

## 5. Setup on HPC Cluster

Following Professor Cheng's pattern from `qiongcheng5/max_coursera`.

### One-time setup (already done on cluster)

```bash
ssh dnagendr@hpc.charlotte.edu
cd ~/coursera_scraper
bash setup_venv.sh
```

`setup_venv.sh` creates `scraper_venv/` with:
- `beautifulsoup4==4.14.3`
- `lxml==6.0.2`
- `openpyxl==3.1.5`
- `pandas==2.3.3`
- `requests==2.32.5`

### Upload files from Mac

```bash
# From Mac terminal (not SSH):
scp cs_scraper.py submit_cs_scraper.py \
    dnagendr@hpc.charlotte.edu:~/coursera_scraper/
```

### Get CAUTH cookie

1. Open Chrome → log into https://www.coursera.org
2. Press **F12** → **Application** tab → **Storage → Cookies → coursera.org**
3. Find cookie named **`CAUTH`** → copy the full Value (~400 characters)

> ⚠️ CAUTH is tied to your Coursera account, not your IP. The same CAUTH works across all cluster nodes simultaneously. It typically expires after a few hours of scraping activity.

---

## 6. How to Run

### Submit parallel jobs (professor's pattern — one job per chunk)

```bash
cd ~/coursera_scraper
python3 submit_cs_scraper.py "PASTE_CAUTH_HERE" --chunks 20
```

This generates and submits 20 SLURM jobs simultaneously, each processing ~132 courses.

### Monitor jobs

```bash
# See all running jobs with node assignments
squeue -u dnagendr -o "%.10i %.9P %.12j %.8T %.6M %R"

# Watch log for a specific chunk (logs go to ~/coursera_scraper/)
cat ~/coursera_scraper/slurm-JOBID.out

# Check progress across all chunks
for f in ~/coursera_scraper/cs_checkpoint_chunk*of20.json; do
  echo -n "$f: "
  python3 -c "import json; d=json.load(open('$f')); \
    print(len(d.get('completed_slugs',[])),' done')"
done
```

### Merge outputs after all jobs finish

```bash
source ~/coursera_scraper/scraper_venv/bin/activate
python3 submit_cs_scraper.py "" --merge --chunks 20
# Creates cs_dataset_final.json and cs_dataset_final.csv
```

### Run a single job (no chunking)

```bash
source ~/coursera_scraper/scraper_venv/bin/activate
python3 cs_scraper.py \
    --excel "Coursera Career Academy PLUS+ Catalog.xlsx" \
    --cauth "PASTE_CAUTH_HERE" \
    --workers 1 \
    --delay 3.0
```

---

## 7. Scraper Architecture

### API flow per course

```
Excel slug
    ↓
onDemandCourses.v1?q=slug&slug=SLUG
    → course_id, level, instructorIds, domainTypes
    ↓
courses.v1/{course_id}
    → learningObjectives (CML parsed to plain text)
    ↓
onDemandCourseMaterials.v2?courseId={id}   [CAUTH required]
    → modules → lessons → items (videos, readings, assignments)
    ↓
Page HTML scrape: coursera.org/learn/{slug}
    → skills, tools, avgProductRating
    ↓
instructors.v1/{id}
    → instructor full names
```

### Key design decisions

**Checkpoint every 1 course** — saves `cs_checkpoint.json` after every single course so no work is lost if CAUTH expires or the job is killed.

**Thread-safe CAUTH detection** — uses `threading.Lock()`. When 5 consecutive API calls return 401/403, sets a global `_cauth_dead = True` flag. All threads stop cleanly and save partial output before exit.

**Slug-based item type inference** — `onDemandCourseMaterials.v2` does not return item types directly. Types are inferred from item slugs using keyword matching rules (e.g. `quiz`, `graded-`, `programming-assignment` → `assignment`).

**CML learning objective parsing** — Coursera encodes learning objectives in CML (Coursera Markup Language). The scraper recursively walks the CML AST to extract plain text strings.

**Chunk mode** — `--chunk-index` and `--chunk-total` arguments slice the full 2,635 course list. Each chunk writes its own checkpoint and output files independently to avoid conflicts between parallel jobs.

---

## 8. HPC Deployment Journey — What We Tried

This section documents every approach attempted and the outcome of each.

### Run 1 — Basic SLURM, `module load python3`
- **Job:** 10233444 | **Time limit:** 12h
- **Result:** 23 courses scraped, CAUTH died ~2 hours in
- **Issue:** Low request volume still triggered rate limiting after ~2 hours

### Run 2 — Multi-worker (3 threads)
- **Job:** 10234780
- **Result:** CAUTH died within 10 minutes, 0 courses saved
- **Issue:** 3 parallel threads tripled the API request rate → faster rate limiting

### Runs 3–4 — Threading bug
- **Jobs:** 10238302, 10238346, 10238360
- **Result:** CAUTH died within 5–10 minutes
- **Issue:** Race condition in CAUTH expiry detection — fixed with `threading.Lock()`

### Run 5 — `random` import bug
- **Job:** 10245040
- **Result:** Crashed immediately with `NameError: name 'random' is not defined`
- **Fix:** Added `random` to main import line

### Run 6 — Professor's venv pattern
- **Job:** 10260995
- **Setup:** `setup_venv.sh` creates `scraper_venv/` following professor's `qiongcheng5/max_coursera` pattern
- **Result:** Venv working ✅, course 1 scraped successfully ✅, then 403s within 6 minutes
- **Log evidence:**
  ```
  17:08:07 [INFO] .NET & .NET Core Mastery → success | skills=6 tools=5 mods=1 vids=27
  17:08:11 [WARNING] HTTP 403 on API (cauth_fail_count=1)
  17:14:59 [WARNING] HTTP 403 on API (cauth_fail_count=1)
  ```

### Run 7 — 20 parallel chunk jobs (professor's multi-job pattern)
- **Jobs:** 10261227–10261246 (20 jobs across nodes str-c9, str-c19, str-c26)
- **Setup:** `submit_cs_scraper.py` generates one `.slurm` per chunk and submits all simultaneously — directly mirrors professor's `slurm_4get_wikidata_subnet.py` + `submit_4get_wikidata_subnet.sh` approach
- **Result:** All 20 jobs ran, same 1-course-then-403 pattern on every chunk
- **Discovery:** Root cause found — see Section 9

---

## 9. Root Cause: NAT Gateway Discovery

**This is the key finding that explains all failures.**

Inside the SLURM job log for chunk 0 (job 10261227, node str-c9):

```
Node     : str-c9.charlotte.edu
Node IP  : 192.168.170.9        ← RFC 1918 private address
Chunk    : 0 of 20
...
17:08:07 [INFO] Course 1/132 → success
17:08:11 [WARNING] HTTP 403 on API
```

`192.168.x.x` is a **private IP address**. Every compute node on Orion sits behind a **NAT (Network Address Translation) gateway**. All outbound internet traffic — regardless of which physical node the SLURM job runs on — exits through **one shared external IP address**.

### Impact on our multi-node strategy

| What we assumed | What is actually true |
|---|---|
| 20 jobs on 20 nodes = 20 different external IPs | 20 jobs on 20 nodes = **1 external IP** |
| More nodes = better rate limit bypass | More nodes = zero benefit for IP bypass |
| Professor's multi-job pattern bypasses rate limiting | Pattern works for Wikidata (no rate limits); does not help with Coursera's IP-level blocking |

Coursera applies rate limiting at the **external IP level**. Since the entire UNCC HPC cluster shares one NAT'd external IP, Coursera sees every request — from every node, every chunk, every job — as coming from the same machine and triggers rate limiting after 1–2 API calls.

**The scraper itself is correct and working.** Course 1 always succeeds. The problem is entirely at the network infrastructure level.

### Cluster node state during runs

```
STATE   NODES   NODELIST
mix       6     str-c[19,96-97,155,165,201]        ← available
drng     42     str-c[10,12,16-17,20-21,...]       ← draining for maintenance
drain     2     str-c[14,164]                       ← offline
alloc    37     str-bm5,str-c[1,3,7-9,...]         ← busy (other users)
idle      2     str-abm1,str-bm1                    ← free
```

42 of ~100 nodes were draining for maintenance. All 20 submitted jobs landed on just 3 nodes (str-c9, str-c19, str-c26) — each sharing the same NAT external IP.

---

## 10. Current Blocker — Help Needed

The scraper is fully implemented, tested, and working correctly. The only remaining problem is **Coursera's IP-level rate limiting on the HPC cluster's shared NAT gateway**.

**Specific questions for Professor Cheng:**

1. **Does UNCC HPC have a way to assign a dedicated outbound IP for research jobs?**  
   Some HPC systems have a separate research egress IP or proxy distinct from the general NAT gateway.

2. **Can HPC IT (`ithelp@uncc.edu`) configure a dedicated external IP for this scraping task?**  
   Even a temporarily dedicated IP would allow the full 2,635-course run to complete.

3. **Is there a university proxy or VPN endpoint that provides a different external IP?**

4. **Is running on a cloud VM (GCP/AWS free tier) acceptable for this research?**  
   A cloud VM has its own public IP, never previously seen by Coursera, and would resolve the issue entirely.

5. **Would a very slow single-threaded run (45–60s delay) be feasible?**  
   2,635 × 45s ≈ 33 hours. Coursera may not trigger rate limiting at this rate. Requires a job time limit of 48+ hours and a CAUTH that stays valid that long.

---

## 11. Proposed Solutions

In order of preference:

### Option A — HPC dedicated egress IP
Ask HPC IT to assign a dedicated outbound IP for this research job. Zero code changes needed — just resubmit the existing job.

### Option B — Cloud VM (recommended if Option A unavailable)
```bash
# On GCP e2-micro or AWS t2.micro (both free tier):
git clone <this-repo>
pip install -r requirements.txt
python3 cs_scraper.py \
    --excel "Coursera Career Academy PLUS+ Catalog.xlsx" \
    --cauth "FRESH_CAUTH" \
    --workers 2 \
    --delay 2.0
```
A fresh cloud IP has no Coursera rate limit history. Expected completion: 2–4 hours.

### Option C — Ultra-slow HPC run
```bash
python3 cs_scraper.py \
    --excel "Coursera Career Academy PLUS+ Catalog.xlsx" \
    --cauth "FRESH_CAUTH" \
    --workers 1 \
    --delay 45.0
```
2,635 × 45s ≈ 33 hours. Submit as a 2-day SLURM job. Risk: CAUTH may expire before completion.

### Option D — Incremental resume (current workaround)
Since the scraper checkpoints after every course:
1. Submit job with fresh CAUTH → scrapes ~1–5 courses before 403
2. Get new CAUTH, resubmit with `--resume`
3. Repeat ~500–2000 times

Tedious but would eventually complete all 2,635 courses.

---

## 12. Output Format

### cs_dataset_final.json (nested per course)

```json
[
  {
    "slug": "net-core-mastery",
    "xlsx_name": ".NET & .NET Core Mastery: Cross-Platform",
    "scraped_name": ".NET & .NET Core Mastery: Cross-Platform",
    "scrape_status": "success",
    "level": "BEGINNER",
    "course_rating": 4.7,
    "instructor_names": ["John Smith"],
    "learning_objectives": ["Build cross-platform apps", "..."],
    "final_skills": ["C#", ".NET", "ASP.NET"],
    "final_tools": ["Visual Studio", "Docker"],
    "total_modules": 8,
    "total_videos": 42,
    "total_readings": 12,
    "total_assignments": 6,
    "total_duration_minutes": 380,
    "modules": [
      {
        "title": "Introduction to .NET",
        "description": "...",
        "items": [
          { "title": "What is .NET?", "type": "video", "duration_minutes": 8.5 }
        ]
      }
    ]
  }
]
```

### cs_dataset_final.csv (flat, pipe-separated multi-values)

| slug | name | status | skills | tools | objectives | level | rating | instructors | total_modules | total_videos | total_readings | total_assignments | total_duration_min |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| net-core-mastery | .NET & .NET Core Mastery | success | C#\|.NET\|ASP.NET | Visual Studio\|Docker | Build apps\|Deploy | BEGINNER | 4.7 | John Smith | 8 | 42 | 12 | 6 | 380 |

---

## 13. Troubleshooting

**HTTP 403 after 1 course on HPC**  
Coursera is rate limiting the cluster's shared NAT IP. See Sections 9 and 10. The scraper is working correctly.

**CAUTH expired (401 errors)**  
Get a fresh CAUTH: Chrome → F12 → Application → Cookies → coursera.org → CAUTH value.

**Log files not in `logs/` folder**  
SLURM writes logs to the working directory. Check `~/coursera_scraper/slurm-JOBID.out`:
```bash
cat ~/coursera_scraper/slurm-10261227.out
```

**Checkpoint not found on resume**  
Chunk checkpoints are named `cs_checkpoint_chunk00of20.json`. Single-job checkpoint is `cs_checkpoint.json`. Ensure `--chunks` matches the original run.

**`No module named openpyxl`**  
Activate the venv first:
```bash
source ~/coursera_scraper/scraper_venv/bin/activate
```

**ZzIndustry Specializations courses missing from output**  
Expected — that sheet has no Course URL column. The 184 courses are recorded as `skipped_no_slug`.

---

## API Reference

| Endpoint | Auth Required | Returns |
|---|---|---|
| `https://www.coursera.org/api/onDemandCourses.v1?q=slug&slug=SLUG` | None | id, name, level, instructorIds, domainTypes |
| `https://www.coursera.org/api/courses.v1/{id}` | None | learningObjectives (CML encoded) |
| `https://www.coursera.org/api/onDemandCourseMaterials.v2?courseId={id}` | **CAUTH** | modules, lessons, items, durations |
| `https://www.coursera.org/api/instructors.v1/{id}` | None | fullName |
| `https://www.coursera.org/learn/{slug}` | None (HTML) | skills, tools, avgProductRating |

---

*Last updated: March 12, 2026*  
*Scraper version: cs_scraper.py v3*  
*Platform: UNCC HPC Orion partition, RHEL 9.7, Python 3.9.23*
