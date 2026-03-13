#!/usr/bin/env python3
"""
Coursera CS-Only Scraper v3
============================
Fixes from v2:
  - Module items (videos/readings/assignments) now scraped from page HTML
    using BeautifulSoup - works without enrollment
  - Skills/Tools scraped from rendered HTML (not __NEXT_DATA__)
  - Instructor rating/learner count parsed from HTML text
  - Python 3.9 compatible (no union type hints)
"""

import argparse, ast, json, logging, random, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import openpyxl
import pandas as pd
import requests
from bs4 import BeautifulSoup
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--excel",   required=True)
parser.add_argument("--cauth",   default="")
parser.add_argument("--workers", type=int,   default=5)
parser.add_argument("--delay",   type=float, default=1.5)
parser.add_argument("--limit",   type=int,   default=None)
parser.add_argument("--resume",  action="store_true")
parser.add_argument("--chunk-index", type=int, default=None,
                    help="Which chunk to process (0-based)")
parser.add_argument("--chunk-total", type=int, default=None,
                    help="Total number of chunks")
parser.add_argument("--test",    action="store_true",
                    help="Test mode: 3 courses per sheet")
args = parser.parse_args()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(f"cs_scraper_{args.chunk_index}.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

BASE             = "https://www.coursera.org/api"
CS_DOMAIN        = "Computer Science"
CHECKPOINT_FILE  = f"results/cs_checkpoint_{args.chunk_index}.json"
OUTPUT_JSON      = f"results/cs_dataset_{args.chunk_index}.json"
OUTPUT_CSV       = f"results/cs_dataset_{args.chunk_index}.csv"
CHECKPOINT_EVERY = 1

# CAUTH expiry tracking — thread-safe
from threading import Lock as _Lock
_cauth_dead       = False
_cauth_fail_count = 0
_cauth_lock       = _Lock()   # protects both flags across threads

SHEET_CONFIGS = {
    "CA+ 825": {
        "header_row": 3,
        "url_col":    "Course URL",
        "name_col":   "Course Name",
        "domain_col": "Domain",
        "extra_cols": {
            "partner":     "University / Industry Partner",
            "domain":      "Domain",
            "subdomain":   "Sub-Domain",
            "level_xlsx":  "Difficulty Level",
            "rating_xlsx": "Course Rating",
            "asset_type":  "Asset Type",
            "hours":       "Avg Total Learning Hours",
            "certificate": "Certificate Enabled (Yes / No)",
        },
    },
    "CA+ Guided Projects": {
        "header_row": 3,
        "url_col":    "Course URL",
        "name_col":   "Course Name",
        "domain_col": "Domain",
        "extra_cols": {
            "partner":     "University / Industry Partner",
            "domain":      "Domain",
            "subdomain":   "Sub-Domain",
            "level_xlsx":  "Difficulty Level",
            "rating_xlsx": "Course Rating",
            "hours":       "Avg Total Learning Hours",
            "certificate": "Certificate Enabled (Yes / No)",
        },
    },
    "ZzIndustry Specializations": {
        "header_row": 1,
        "url_col":    None,
        "name_col":   "Specialization Name",
        "domain_col": "Domain",
        "extra_cols": {
            "partner":          "Partner Name",
            "domain":           "Domain",
            "level_xlsx":       "Level",
            "description_xlsx": "Description",
        },
    },
}

ITEM_TYPE_MAP = {
    "lecture": "video", "video": "video",
    "supplement": "reading", "reading": "reading",
    "quiz": "assignment", "exam": "assignment",
    "gradedprogramming": "assignment", "ungradedprogramming": "assignment",
    "peer": "assignment", "gradedlti": "assignment",
    "discussion": "discussion", "discussionprompt": "discussion",
}

def normalise_type(raw):
    key = str(raw).lower().replace("_","").replace("-","").replace(" ","")
    for k, v in ITEM_TYPE_MAP.items():
        if k in key:
            return v
    return "other"

# ── SESSION ──────────────────────────────────────────────────────────
def make_session(cauth):
    s = requests.Session()
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.coursera.org/",
        "Origin":          "https://www.coursera.org",
    })
    if cauth:
        s.cookies.set("CAUTH", cauth, domain="www.coursera.org", path="/")
        s.cookies.set("CAUTH", cauth, domain=".coursera.org",    path="/")
    return s

# ── RATE-LIMIT AWARE GET ─────────────────────────────────────────────
def safe_get(session, url, params=None, delay=1.5):
    if params is None:
        params = {}
    backoff = 3.0
    for attempt in range(5):
        try:
            r = session.get(url, params=params, timeout=25)
            if r.status_code == 429:
                wait = backoff * (2 ** attempt)
                ra = r.headers.get("Retry-After")
                if ra:
                    try: wait = max(wait, float(ra))
                    except: pass
                log.warning("429 rate-limited — waiting %.0fs (attempt %d/5)", wait, attempt+1)
                time.sleep(wait)
                continue
            if r.status_code in (500,502,503,504):
                wait = backoff * (2 ** attempt)
                log.warning("HTTP %d — waiting %.0fs (attempt %d/5)", r.status_code, wait, attempt+1)
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                global _cauth_fail_count, _cauth_dead
                with _cauth_lock:
                    _cauth_fail_count += 1
                    count = _cauth_fail_count
                log.warning("HTTP %d on API (cauth_fail_count=%d) — %s",
                            r.status_code, count, url.split("?")[0][-60:])
                if count >= 5:
                    log.error("CAUTH EXPIRED — 5 consecutive auth failures. "
                              "Stopping cleanly. Resume with fresh CAUTH using --resume.")
                    with _cauth_lock:
                        _cauth_dead = True
                return None
            if r.status_code != 200:
                log.warning("HTTP %d — %s", r.status_code, url.split("?")[0][-60:])
                return None
            with _cauth_lock:
                _cauth_fail_count = 0  # reset on success
            time.sleep(delay + random.uniform(0.5, 2.5))
            return r.json()
        except requests.exceptions.Timeout:
            time.sleep(backoff * (2 ** attempt))
        except requests.exceptions.ConnectionError as e:
            log.warning("Connection error (attempt %d): %s", attempt+1, str(e)[:60])
            time.sleep(backoff * (2 ** attempt))
        except Exception as e:
            log.warning("safe_get error: %s", str(e)[:80])
            return None
    log.error("All retries failed for %s", url.split("?")[0][-80:])
    return None

def page_get(session, url, delay=1.5):
    global _cauth_dead, _cauth_fail_count
    if _cauth_dead:
        return None
    backoff = 3.0
    for attempt in range(4):
        try:
            r = session.get(url, timeout=25)
            if r.status_code == 429:
                wait = backoff * (2 ** attempt)
                log.warning("429 on page — waiting %.0fs", wait)
                time.sleep(wait)
                continue
            if r.status_code in (500,502,503,504):
                time.sleep(backoff * (2 ** attempt))
                continue
            if r.status_code in (401, 403):
                _cauth_fail_count += 1
                log.warning("HTTP %d on page (cauth_fail_count=%d) — %s",
                            r.status_code, _cauth_fail_count, url[-80:])
                if _cauth_fail_count >= 3:
                    log.error("CAUTH EXPIRED — 3 consecutive auth failures on page. "
                              "Stopping cleanly. Resume with fresh CAUTH using --resume.")
                    _cauth_dead = True
                return None
            _cauth_fail_count = 0   # reset on success
            if r.status_code != 200:
                return None
            time.sleep(delay)
            return r.text
        except Exception as e:
            time.sleep(backoff * (2 ** attempt))
    return None

# ── HELPERS ──────────────────────────────────────────────────────────
def slug_from_url(url):
    if not url: return None
    m = re.search(r"/learn/([^/?#]+)", str(url))
    if m: return m.group(1)
    m2 = re.search(r"/specializations/([^/?#]+)", str(url))
    return m2.group(1) if m2 else None

def parse_cml(obj):
    try:
        if isinstance(obj, str) and obj.strip().startswith("{"):
            obj = ast.literal_eval(obj)
        if isinstance(obj, dict):
            defn = obj.get("definition", {})
            if isinstance(defn, dict):
                rh   = defn.get("renderableHtmlWithMetadata", {})
                html = (rh.get("renderableHtml","") if isinstance(rh,dict) else "") or defn.get("value","")
                text = re.sub(r"<[^>]+>"," ",html)
                return re.sub(r"\s+"," ",text).strip()
    except: pass
    return str(obj).strip()

def deep_find(obj, keys, depth=0):
    if depth > 12: return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k]: return obj[k]
        for v in obj.values():
            r = deep_find(v, keys, depth+1)
            if r: return r
    elif isinstance(obj, list):
        for item in obj:
            r = deep_find(item, keys, depth+1)
            if r: return r
    return None

def flatten_str_list(obj, max_items=40):
    if not obj: return []
    if isinstance(obj, str): return [obj.strip()] if obj.strip() else []
    if isinstance(obj, list):
        out = []
        for item in obj:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                for k in ["name","text","title","label","value","objective","skill","toolName"]:
                    if item.get(k) and isinstance(item[k], str):
                        out.append(item[k].strip()); break
            if len(out) >= max_items: break
        return out
    return []

def extract_duration_sec(text):
    total = 0
    h = re.search(r"(\d+)\s*hours?", text, re.I)
    m = re.search(r"(\d+)\s*mins?(?:utes?)?", text, re.I)
    if h: total += int(h.group(1)) * 3600
    if m: total += int(m.group(1)) * 60
    return total

# ── EXCEL READER ─────────────────────────────────────────────────────
def read_sheet_cs(filepath, sheet_name, config, limit=None):
    wb  = openpyxl.load_workbook(filepath, read_only=False, data_only=True)
    ws  = wb[sheet_name]
    hr  = config["header_row"]
    col = {ws.cell(hr,c).value: c for c in range(1, (ws.max_column or 50)+1)}
    url_c  = col.get(config.get("url_col"))
    name_c = col.get(config.get("name_col"))
    dom_c  = col.get(config.get("domain_col"))
    rows = []
    for r in range(hr+1, (ws.max_row or 1)+1):
        dom_val = ws.cell(r, dom_c).value if dom_c else None
        if not dom_val or CS_DOMAIN not in str(dom_val): continue
        url  = ws.cell(r, url_c).value  if url_c  else None
        name = ws.cell(r, name_c).value if name_c else None
        if not url and not name: continue
        url_s = str(url).strip() if url else None
        row = {"source": sheet_name, "xlsx_name": name, "url": url_s, "slug": slug_from_url(url_s)}
        for key, col_name in config.get("extra_cols",{}).items():
            ci = col.get(col_name)
            row[key] = ws.cell(r, ci).value if ci else None
        rows.append(row)
        if limit and len(rows) >= limit: break
    wb.close()
    return rows

def load_cs_courses(excel_path, limit=None):
    log.info("Loading CS courses from %s", excel_path)
    wb = openpyxl.load_workbook(excel_path, read_only=False)
    available = wb.sheetnames
    wb.close()
    all_courses = []
    for sheet, cfg in SHEET_CONFIGS.items():
        if sheet not in available:
            log.warning("Sheet '%s' not found", sheet); continue
        rows = read_sheet_cs(excel_path, sheet, cfg, limit=limit)
        log.info("  %-35s %d CS courses", sheet, len(rows))
        all_courses.extend(rows)
    seen, deduped = set(), []
    for c in all_courses:
        s = c.get("slug")
        if s and s not in seen:
            seen.add(s); deduped.append(c)
        elif not s:
            deduped.append(c)
    log.info("Total CS courses (deduped): %d", len(deduped))
    return deduped

# ── MODULE BUILDER ────────────────────────────────────────────────────
def build_module(title, description, items_flat):
    """
    items_flat: list of {title, normalised_type, duration_sec}
    Groups into videos/readings/assignments/discussions.
    """
    videos, readings, assignments, discussions, other = [], [], [], [], []
    for item in items_flat:
        t     = item.get("normalised_type","other")
        title_i = item.get("title","")
        dur   = item.get("duration_sec", 0)
        if t == "video":
            videos.append({"title": title_i, "duration_sec": dur,
                           "duration_min": round(dur/60,1) if dur else 0})
        elif t == "reading":
            readings.append({"title": title_i, "duration_sec": dur,
                             "duration_min": round(dur/60,1) if dur else 0})
        elif t == "assignment":
            assignments.append({"title": title_i})
        elif t == "discussion":
            discussions.append({"title": title_i})
        else:
            other.append({"title": title_i, "type": item.get("type","")})

    total_dur = sum(v["duration_sec"] for v in videos) + sum(r["duration_sec"] for r in readings)
    return {
        "title":            title,
        "description":      description,
        "videos":           videos,
        "readings":         readings,
        "assignments":      assignments,
        "discussions":      discussions,
        "other_items":      other,
        "video_count":      len(videos),
        "reading_count":    len(readings),
        "assignment_count": len(assignments),
        "discussion_count": len(discussions),
        "total_duration_sec": total_dur,
        "total_duration_min": round(total_dur/60,1) if total_dur else 0,
    }

# ── API: SYLLABUS ────────────────────────────────────────────────────
def parse_time_commitment(tc_str):
    """
    Parse Coursera timeCommitment string into item counts.
    e.g. "12 videos • Total 62 minutes\n15 readings • Total 73 minutes\n5 assignments"
    Returns dict with video_count, reading_count, assignment_count, discussion_count,
    total_duration_min and lists of synthetic items for each type.
    """
    if not tc_str:
        return {}
    result = {
        "video_count": 0, "reading_count": 0,
        "assignment_count": 0, "discussion_count": 0,
        "total_duration_min": 0,
        "videos": [], "readings": [], "assignments": [], "discussions": [],
    }
    lines = str(tc_str).split("\n") if "\n" in str(tc_str) else [str(tc_str)]
    for line in lines:
        line = line.strip()
        # Count: "12 videos", "15 readings", "5 assignments", "1 discussion"
        m = re.match(r"(\d+)\s+(videos?|lectures?|readings?|supplements?|assignments?|quizzes|discussions?|prompts?)",
                     line, re.I)
        if m:
            count = int(m.group(1))
            kind  = m.group(2).lower()
            if "video" in kind or "lecture" in kind:
                result["video_count"] = count
            elif "reading" in kind or "supplement" in kind:
                result["reading_count"] = count
            elif "assignment" in kind or "quiz" in kind:
                result["assignment_count"] = count
            elif "discussion" in kind or "prompt" in kind:
                result["discussion_count"] = count
        # Duration: "Total 62 minutes"
        dur = re.search(r"Total\s+(\d+)\s+minutes?", line, re.I)
        if dur:
            result["total_duration_min"] += int(dur.group(1))
    return result


def infer_type_from_slug(slug_str):
    """
    Infer item type from Coursera item slug.
    
    Debug confirmed: Coursera slugs are fully descriptive, no generic prefix.
    Rules derived from machine-learning, python-crash-course, deep-neural-network:

      ASSIGNMENT: contains 'quiz', 'graded', 'assignment', 'peer-review', 'exam'
      READING:    starts with 'optional-', 'jupyter', 'reading-', 'supplement',
                  'about-', 'welcome-to-week', 'introduction-to-week',
                  contains '-notebook', 'ungraded-lab'
      DISCUSSION: starts with 'discussion-prompt', 'discussion-forum', 'join-the-'
      OTHER:      'intake-survey', 'certificate-'
      VIDEO:      everything else (the majority)
    """
    s = str(slug_str).lower().strip()

    # ASSIGNMENT — quiz/graded keywords anywhere in slug
    if any(kw in s for kw in
           ["quiz","graded-","programming-assignment","peer-graded",
            "-assignment","ungraded-quiz","practice-assessment"]):
        return "assignment"

    # READING — lab/notebook/supplement items
    if any(s.startswith(p) for p in
           ["optional-","jupyter","reading-","supplement","ungraded-lab",
            "about-","welcome-to-week","introduction-to-week","module-introduction",
            "course-introduction","optional-reading"]):
        return "reading"
    if any(kw in s for kw in ["-notebook","-lab-","-supplement","optional-lab"]):
        return "reading"

    # DISCUSSION
    if any(s.startswith(p) for p in
           ["discussion-prompt","discussion-forum","join-the-","forum-"]):
        return "discussion"

    # OTHER (surveys, announcements, certificates)
    if any(kw in s for kw in ["intake-survey","certificate-","survey-"]):
        return "other"

    # Default: VIDEO
    return "video"


def fetch_syllabus_api(session, slug):
    """
    Fetch full module+lesson+item tree from onDemandCourseMaterials.v2.
    Debug confirmed:
      - modules have: name, description, timeCommitment(ms), id, lessonIds
      - lessons have: name, timeCommitment(ms), id, moduleId, itemIds
      - items  have: name, timeCommitment(ms), id, moduleId, lessonId, slug
      - NO type field on items — inferred from slug
    """
    data = safe_get(session, BASE + "/onDemandCourseMaterials.v2", {
        "q":               "slug",
        "slug":            slug,
        "includes":        "modules,lessons,items",
        "showLockedItems": "true",
    })
    if not data: return {}, "api_failed"

    linked      = data.get("linked", {})
    modules_raw = (linked.get("onDemandCourseMaterialModules.v1") or
                   linked.get("onDemandCourseMaterialModules.v2") or [])
    lessons_raw = (linked.get("onDemandCourseMaterialLessons.v1") or
                   linked.get("onDemandCourseMaterialLessons.v2") or [])
    items_raw   = (linked.get("onDemandCourseMaterialItems.v2")   or
                   linked.get("onDemandCourseMaterialItems.v1")   or [])

    if not modules_raw: return {}, "empty_response"

    # Index lessons and items by ID for O(1) lookup
    lessons_idx = {l["id"]: l for l in lessons_raw}
    items_idx   = {i["id"]: i for i in items_raw}

    mod_data = {}
    has_any_items = False

    for mod in modules_raw:
        title = mod.get("name","")
        if not title: continue
        desc         = mod.get("description","")
        tc_ms        = mod.get("timeCommitment", 0) or 0
        total_dur_min= round(tc_ms / 60000, 1)

        videos, readings, assignments, discussions = [], [], [], []

        # Walk: module.lessonIds → lesson.itemIds → item
        for lesson_id in mod.get("lessonIds", []):
            lesson = lessons_idx.get(lesson_id, {})
            for item_id in lesson.get("itemIds", []):
                item = items_idx.get(item_id)
                if not item: continue
                iname    = item.get("name","")
                islug    = item.get("slug","")
                itc_ms   = item.get("timeCommitment", 0) or 0
                dur_sec  = itc_ms // 1000
                dur_min  = round(dur_sec / 60, 1)
                itype    = infer_type_from_slug(islug)
                has_any_items = True

                if itype == "video":
                    videos.append({"title": iname, "duration_sec": dur_sec, "duration_min": dur_min})
                elif itype == "reading":
                    readings.append({"title": iname, "duration_sec": dur_sec, "duration_min": dur_min})
                elif itype == "assignment":
                    assignments.append({"title": iname})
                elif itype == "discussion":
                    discussions.append({"title": iname})

        mod_data[title] = {
            "description":      desc,
            "videos":           videos,
            "readings":         readings,
            "assignments":      assignments,
            "discussions":      discussions,
            "other_items":      [],
            "video_count":      len(videos),
            "reading_count":    len(readings),
            "assignment_count": len(assignments),
            "discussion_count": len(discussions),
            "total_duration_min": total_dur_min,
            "total_duration_sec": tc_ms // 1000,
        }

    source = "api_with_items" if has_any_items else "api_no_items"
    return mod_data, source

# ── API: METADATA ─────────────────────────────────────────────────────
def fetch_metadata(session, slug):
    data = safe_get(session, BASE + "/onDemandCourses.v1", {
        "q": "slug", "slug": slug,
        "includes": "instructorIds,partnerIds",
    })
    if not data or not data.get("elements"): return {}
    el = data["elements"][0]
    return {
        "course_id":           el.get("id",""),
        "scraped_name":        el.get("name",""),
        "tagline":             el.get("tagline",""),
        "scraped_description": el.get("description","").strip(),
        "level":               el.get("level",""),
        "primary_languages":   el.get("primaryLanguages",[]),
        "workload":            el.get("workloadDescription",""),
        "_instructor_ids":     el.get("instructorIds",[]),
        "_partner_ids":        el.get("partnerIds",[]),
    }

def fetch_metadata_from_html(session, slug):
    """
    Fallback metadata extraction from __NEXT_DATA__ JSON embedded in the page HTML.
    Used when onDemandCourses.v1 API returns no elements (newer/3rd-party courses).
    Extracts: course_id, name, description, level, languages, instructor/partner IDs.
    """
    html = page_get(session, "https://www.coursera.org/learn/" + slug)
    if not html:
        return {}
    nd_match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.S
    )
    if not nd_match:
        return {}
    try:
        nd = json.loads(nd_match.group(1))
    except Exception:
        return {}

    # course_id: look for the xdp courseId or onDemandCourseId
    course_id = (
        deep_find(nd, ["courseId", "onDemandCourseId", "id"]) or ""
    )
    name = (
        deep_find(nd, ["courseName", "name", "title"]) or ""
    )
    description = (
        deep_find(nd, ["courseDescription", "description", "about"]) or ""
    )
    level = deep_find(nd, ["difficultyLevel", "level", "courseLevel"]) or ""
    if isinstance(level, str):
        level = level.upper()

    soup = BeautifulSoup(html, "html.parser")
    # Instructor IDs: Coursera embeds them as data attributes or in JSON
    instructor_ids = flatten_str_list(deep_find(nd, ["instructorIds", "instructors"]))
    partner_ids    = flatten_str_list(deep_find(nd, ["partnerIds", "partners"]))

    if not name:
        # Last resort: og:title or <title> tag
        og = soup.find("meta", property="og:title")
        name = og["content"].strip() if og and og.get("content") else ""
        if not name:
            t = soup.find("title")
            if t:
                name = re.sub(r"\s*[\|–-].*$", "", t.get_text(strip=True))

    if not description:
        og_desc = soup.find("meta", property="og:description")
        description = og_desc["content"].strip() if og_desc and og_desc.get("content") else ""

    log.info("HTML metadata fallback for %s: id=%s name=%s", slug, course_id, name[:40])
    return {
        "course_id":           str(course_id),
        "scraped_name":        str(name),
        "tagline":             "",
        "scraped_description": str(description).strip(),
        "level":               str(level),
        "primary_languages":   [],
        "workload":            "",
        "_instructor_ids":     instructor_ids,
        "_partner_ids":        partner_ids,
        "_meta_source":        "html_fallback",
    }

def fetch_enriched(session, course_id):
    if not course_id: return {}
    data = safe_get(session, BASE + "/courses.v1/" + course_id, {
        "fields": "name,slug,description,domainTypes,skills,learningObjectives,"
                  "workload,level,language,subtitleLanguages",
    })
    if not data or not data.get("elements"): return {}
    el = data["elements"][0]
    raw_skills = el.get("skills",[])
    skills = ([s.get("name") or s.get("skill","") for s in raw_skills]
              if raw_skills and isinstance(raw_skills[0], dict)
              else (raw_skills if isinstance(raw_skills,list) else []))
    objectives = [parse_cml(o) for o in el.get("learningObjectives",[]) if o]
    objectives = [o for o in objectives if o]
    domain_types = []
    for dt in el.get("domainTypes",[]):
        if isinstance(dt, dict):
            d = dt.get("domainType","") or dt.get("name","")
            s = dt.get("subdomainType","") or dt.get("subdomain","")
            if d: domain_types.append(d + ("/"+s if s else ""))
        elif isinstance(dt, str) and dt:
            domain_types.append(dt)
    return {
        "api_skills":          [s for s in skills if s],
        "learning_objectives": objectives,
        "domain_types":        domain_types,
        "workload_v2":         el.get("workload",""),
        "level_v2":            el.get("level",""),
        "language_v2":         el.get("language","") or el.get("primaryLanguage",""),
        "subtitle_languages":  el.get("subtitleLanguages",[]),
    }

def fetch_instructors(session, ids):
    if not ids: return []
    # Request avgRating, numRatings, numStudents explicitly
    data = safe_get(session, BASE + "/instructors.v1", {
        "ids":    ",".join(ids),
        "fields": "avgRating,numRatings,numStudents,numCourses,title,fullName,bio,photo,department",
    })
    if not data: return []
    out = []
    for i in data.get("elements",[]):
        name = (i.get("fullName") or
                (i.get("firstName","") + " " + i.get("lastName","")).strip() or
                i.get("shortName",""))
        out.append({
            "name":          name,
            "title":         i.get("title",""),
            "department":    i.get("department",""),
            "bio":           i.get("bio","").strip()[:400],
            "rating":        i.get("avgRating",""),
            "num_ratings":   i.get("numRatings",""),
            "learner_count": i.get("numStudents",""),
            "course_count":  i.get("numCourses",""),
        })
    return out

def fetch_partners(session, ids):
    if not ids: return []
    data = safe_get(session, BASE + "/partners.v1", {"ids": ",".join(ids)})
    if not data: return []
    return [{"name": p.get("name",""), "website": p.get("websiteUrl",""),
             "logo_url": p.get("squareLogo") or p.get("logo","")}
            for p in data.get("elements",[])]

# ── PAGE SCRAPER (PRIMARY source for items/skills/tools) ─────────────
def scrape_page(session, slug):
    """
    Parse the rendered HTML of the Coursera course page.
    This is the PRIMARY source for:
      - videos, readings, assignments, discussions (with titles & durations)
      - skills you'll gain
      - tools you'll learn
      - instructor rating + learner count
    """
    result = {
        "nd_skills":         [],
        "nd_tools":          [],
        "nd_objectives":     [],
        "html_modules":      [],
        "instructor_stats":  [],
        "weekly_hours":      "",
        "duration_weeks":    "",
        "flexible_deadline": False,
        "course_rating":     "",
        "num_ratings":       "",
        "page_scraped":      False,
    }

    html = page_get(session, "https://www.coursera.org/learn/" + slug)
    if not html:
        return result

    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(" ", strip=True)

    # ── Duration ──────────────────────────────────────────────────────
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\s*(?:a|per|/)\s*week", full_text, re.I)
    if m: result["weekly_hours"] = m.group(0).strip()
    m2 = re.search(r"(\d+)\s*weeks?", full_text, re.I)
    if m2: result["duration_weeks"] = m2.group(0).strip()
    result["flexible_deadline"] = bool(re.search(r"flexible\s+deadline", full_text, re.I))

    # ── Course rating from embedded JSON ──────────────────────────────
    # Page HTML contains a JSON blob with course stats
    # Look for avgProductRating, courseRating, or stars pattern
    rating_m = re.search(r'"avgProductRating"\s*:\s*"?([\d.]+)"?', html)
    if not rating_m:
        rating_m = re.search(r'"courseRating"\s*:\s*"?([\d.]+)"?', html)
    if not rating_m:
        # Visible rating like "4.9" near a star symbol in HTML
        rating_m = re.search(r'aria-label="[\d.]+ out of 5 stars".*?>([\d.]+)<', html)
    if rating_m:
        result["course_rating"] = float(rating_m.group(1))

    enroll_m = re.search(r'"contentSatisfactionRatingsCount"\s*:\s*(\d+)', html)
    if enroll_m:
        result["num_ratings"] = int(enroll_m.group(1))

    # ── Skills you'll gain ────────────────────────────────────────────
    # Debug confirmed: skills are in <ul data-testid="skills-section">
    # with each skill as an <a> tag inside nested spans.
    skills = []

    # Primary: exact selector confirmed by debug output
    skill_links = soup.select('[data-testid="skills-section"] a')
    if skill_links:
        seen = set()
        for a in skill_links:
            t = a.get_text(strip=True)
            if t and t not in seen and 2 < len(t) < 60:
                seen.add(t)
                skills.append(t)

    # Fallback: individual skill-tag spans
    if not skills:
        for span in soup.select('[data-testid^="skill-tag-"]'):
            t = span.get_text(strip=True)
            if t and 2 < len(t) < 60:
                skills.append(t)

    result["nd_skills"] = skills[:20]

    # ── Tools you'll learn ────────────────────────────────────────────
    # Same pattern as skills — look for data-testid="tools-section"
    # Only exists on Google/IBM/Meta/DeepLearning.AI certs, not 3rd party.
    tools = []

    tool_links = soup.select('[data-testid="tools-section"] a')
    if tool_links:
        seen = set()
        for a in tool_links:
            t = a.get_text(strip=True)
            if t and t not in seen and 2 < len(t) < 60:
                seen.add(t)
                tools.append(t)

    # Fallback: find "Tools you'll learn" heading strictly
    if not tools:
        for tag in soup.find_all(["h2","h3","h4"]):
            if re.search(r"^tools you.ll learn$", tag.get_text(strip=True), re.I):
                container = tag.find_parent(["section","div","article"]) or tag.parent
                if container:
                    seen = set()
                    for a in container.find_all("a"):
                        t = a.get_text(strip=True)
                        if t and t not in seen and 2 < len(t) < 60:
                            seen.add(t)
                            tools.append(t)
                break

    result["nd_tools"] = tools[:20]

    # ── Learning objectives ───────────────────────────────────────────
    objectives = []
    for sel in ["[data-testid='learning-objectives'] li",
                "[data-testid='outcomes-list'] li",
                "[class*='LearningObjective'] li",
                "[class*='learning-objective'] li"]:
        items = soup.select(sel)
        if items:
            objectives = [i.get_text(strip=True) for i in items if i.get_text(strip=True)]
            break
    if not objectives:
        for tag in soup.find_all(["h2","h3","h4"]):
            if re.search(r"what you.ll learn", tag.get_text(), re.I):
                container = tag.find_parent(["section","div","article"])
                if container:
                    lis = container.find_all("li")
                    objectives = [li.get_text(strip=True) for li in lis if li.get_text(strip=True)]
                    if objectives: break
    result["nd_objectives"] = objectives[:20]

    # ── Module items from HTML ────────────────────────────────────────
    # Coursera renders the full syllabus publicly in HTML.
    # Each module is an accordion. Items have icons indicating type.
    html_modules = _parse_modules_from_html(soup)
    result["html_modules"] = html_modules

    # ── Instructor stats ──────────────────────────────────────────────
    result["instructor_stats"] = _parse_instructor_stats(soup, full_text)
    result["page_instructor_rating"] = _parse_instructor_rating_from_text(full_text)

    # ── __NEXT_DATA__ fallback ────────────────────────────────────────
    nd_match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.S
    )
    if nd_match:
        try:
            nd = json.loads(nd_match.group(1))
            if not result["nd_skills"]:
                result["nd_skills"] = flatten_str_list(
                    deep_find(nd, ["skills","skillNames","skillsYouWillGain","skillGain"]))
            if not result["nd_tools"]:
                result["nd_tools"] = flatten_str_list(
                    deep_find(nd, ["tools","toolNames","toolsYouWillLearn"]))
            if not result["nd_objectives"]:
                result["nd_objectives"] = flatten_str_list(
                    deep_find(nd, ["learningObjectives","objectives","whatYouWillLearn"]))
        except Exception:
            pass

    result["page_scraped"] = True
    return result


def _parse_instructor_rating_from_text(text):
    """
    Coursera shows instructor stats like:
    "4.9 ★  52,556 Ratings  7,392,767 Learners  15 Courses"
    Parse all rating+learner blocks from page text.
    """
    results = []
    # Pattern: number ★ ... Ratings ... Learners
    blocks = re.findall(
        r"(\d+\.\d+)\s*(?:★|&#x2605;|\*)\s*([\d,]+)\s*Ratings?\s*([\d,]+)\s*Learners?(?:\s*([\d,]+)\s*Courses?)?",
        text, re.I
    )
    for b in blocks:
        results.append({
            "rating":      float(b[0]),
            "num_ratings": int(b[1].replace(",","")),
            "learners":    int(b[2].replace(",","")),
            "courses":     int(b[3].replace(",","")) if b[3] else "",
        })
    return results



def _parse_modules_from_html(soup):
    """
    Parse module + item structure from Coursera page HTML.
    Coursera renders syllabus as a list of modules, each with:
      - module heading (h3)
      - description paragraph
      - item list: videos, readings, assignments, discussions
    Each item row has: icon (indicates type), title, duration.
    """
    modules = []

    # Strategy 1: Look for the syllabus/modules section
    # Coursera uses data-testid or class names for the syllabus container
    syllabus = None
    for sel in [
        "[data-testid='syllabus-toggle-row']",
        "[data-testid='module-heading']",
        "[class*='SyllabusModule']",
        "[class*='syllabus-module']",
        "[id*='syllabus']",
        "section[aria-label*='yllabus']",
    ]:
        found = soup.select(sel)
        if found:
            # Get the common ancestor
            syllabus = found[0].find_parent(["section","div","article"])
            if syllabus:
                break

    # Strategy 2: Find by "Modules" or "Syllabus" heading
    if not syllabus:
        for tag in soup.find_all(["h2","h3"]):
            if re.search(r"^(syllabus|modules?|weeks?|course\s+content)$",
                         tag.get_text(strip=True), re.I):
                syllabus = tag.find_parent(["section","div","article"]) or tag.parent
                break

    if not syllabus:
        return []

    # Now parse modules within the syllabus section
    # Each module typically has:
    #   - A header row with title and summary "X videos, Y readings"
    #   - When expanded: individual item rows

    current_title = ""
    current_desc  = ""
    current_items = []

    for el in syllabus.find_all(["h2","h3","h4","p","li","div","span"], recursive=True):
        tag  = el.name
        text = el.get_text(strip=True)
        if not text or len(text) > 300:
            continue

        # Module heading: h3 or h4 with substantial text that's not an item title
        if tag in ("h3","h4") and 3 < len(text) < 100:
            # Looks like a module heading
            if current_title and current_items:
                modules.append(build_module(current_title, current_desc, current_items))
            elif current_title and not current_items:
                # Module with no parsed items yet — still save with empty items
                modules.append(build_module(current_title, current_desc, []))
            current_title = text
            current_desc  = ""
            current_items = []
            continue

        # Module description: p tag following a module heading
        if tag == "p" and current_title and not current_items and len(text) > 20:
            current_desc = text[:500]
            continue

        # Item row: li element
        if tag == "li" and current_title:
            # Detect type from surrounding HTML
            el_html   = str(el)
            raw_type  = _infer_type(el_html, text)
            dur_sec   = extract_duration_sec(text)
            # Clean title: remove duration suffix
            title_clean = re.sub(
                r"\s*[•·]\s*\d+\s*(?:minutes?|mins?|hours?|hrs?)\s*$",
                "", text, flags=re.I
            ).strip()
            title_clean = re.sub(
                r"\s+\d+\s*(?:minutes?|mins?|hours?|hrs?)\s*$",
                "", title_clean, flags=re.I
            ).strip()
            if title_clean and len(title_clean) > 2:
                current_items.append({
                    "title":           title_clean,
                    "type":            raw_type,
                    "normalised_type": normalise_type(raw_type),
                    "duration_sec":    dur_sec,
                })

    # Don't forget the last module
    if current_title:
        modules.append(build_module(current_title, current_desc, current_items))

    return modules


def _infer_type(html_str, text):
    """Infer item type from HTML attributes and text."""
    h = html_str.lower()
    t = text.lower()
    # Check HTML for type hints
    if any(x in h for x in ["video","play-circle","playcircle","lecture","film"]):
        return "video"
    if any(x in h for x in ["book","reading","supplement","article","document"]):
        return "reading"
    if any(x in h for x in ["quiz","assignment","graded","exam","peer","pencil","edit"]):
        return "assignment"
    if any(x in h for x in ["discussion","forum","chat","comment"]):
        return "discussion"
    # Text fallback
    if any(x in t for x in ["video", "watch", "preview"]):
        return "video"
    if any(x in t for x in ["reading", "article", "supplement"]):
        return "reading"
    if any(x in t for x in ["quiz", "assignment", "graded", "assessment", "exam"]):
        return "assignment"
    if any(x in t for x in ["discussion", "prompt", "forum"]):
        return "discussion"
    return "other"


def _parse_instructor_stats(soup, full_text):
    """
    Parse instructor rating and learner count from HTML.
    Coursera shows: "3.1 ★ (17 ratings)" and "232,495 learners"
    """
    stats = []
    # Find instructor cards
    for card in soup.select(
        "[data-testid='instructor-card'], [class*='InstructorCard'], "
        "[class*='instructor-info'], [class*='InstructorInfo']"
    ):
        card_text = card.get_text(" ", strip=True)
        name_tag  = card.find(["h3","h4","strong","b","a"])
        name      = name_tag.get_text(strip=True) if name_tag else ""

        rating_m  = re.search(r"(\d+\.\d+)\s*(?:★|stars?)", card_text, re.I)
        ratings_m = re.search(r"([\d,]+)\s*ratings?", card_text, re.I)
        learners_m= re.search(r"([\d,]+)\s*(?:learners?|students?)", card_text, re.I)
        courses_m = re.search(r"([\d,]+)\s*[Cc]ourses?", card_text, re.I)

        if name:
            stats.append({
                "name":        name,
                "rating":      float(rating_m.group(1))              if rating_m   else "",
                "num_ratings": int(ratings_m.group(1).replace(",",""))  if ratings_m  else "",
                "learners":    int(learners_m.group(1).replace(",","")) if learners_m else "",
                "courses":     int(courses_m.group(1).replace(",",""))  if courses_m  else "",
            })

    # Fallback: scan full page text for rating patterns
    if not stats:
        rating_matches = re.findall(
            r"(\d+\.\d+)\s*(?:★|stars?)\s*\(?([\d,]+)\s*ratings?\)?", full_text, re.I)
        for r, n in rating_matches[:3]:
            stats.append({
                "name": "", "rating": float(r),
                "num_ratings": int(n.replace(",","")),
                "learners": "", "courses": "",
            })
    return stats


# ── CORE: SCRAPE ONE COURSE ───────────────────────────────────────────
def scrape_course(course, cauth):
    slug = course.get("slug")
    if not slug:
        return {**course, "scrape_status": "skipped_no_slug"}

    # Don't start new work if CAUTH is already dead
    if _cauth_dead:
        return {**course, "scrape_status": "skipped_cauth_dead"}

    session = make_session(cauth)

    try:
        meta = fetch_metadata(session, slug)
        if not meta:
            # API returned no elements — try extracting from page HTML (__NEXT_DATA__)
            log.warning("onDemandCourses.v1 empty for %s — trying HTML fallback", slug)
            meta = fetch_metadata_from_html(session, slug)
            if not meta:
                return {**course, "scrape_status": "failed", "fail_reason": "metadata_failed"}
        course_id = meta.get("course_id","")

        em          = fetch_enriched(session, course_id)
        instructors = fetch_instructors(session, meta.pop("_instructor_ids",[]))
        partners    = fetch_partners(session, meta.pop("_partner_ids",[]))

        # Fetch modules+lessons+items from API
        mod_data, modules_source = fetch_syllabus_api(session, slug)

        # PRIMARY: scrape page HTML for skills, tools, objectives
        page = scrape_page(session, slug)

        # Build final module list directly from API data
        final_modules = list(mod_data.values())
        for m in final_modules:
            m.setdefault("other_items", [])

        # ── Instructor stats from page → merge into instructor list ───
        # Page rating blocks (parsed from text like "4.9★ 52,556 Ratings 7M Learners")
        page_rating_blocks = page.get("page_instructor_rating", [])
        page_stats         = page.get("instructor_stats", [])
        all_blocks         = page_rating_blocks or page_stats

        for i, inst in enumerate(instructors):
            # Try card-level match first (has name)
            matched = next((s for s in page_stats
                            if s.get("name") and inst.get("name") and
                            s["name"].lower() in inst["name"].lower()), None)
            # Fall back to positional rating block
            if not matched and i < len(all_blocks):
                matched = all_blocks[i]
            if matched:
                inst["rating"]        = matched.get("rating","")
                inst["num_ratings"]   = matched.get("num_ratings","")
                inst["learner_count"] = matched.get("learners","") or matched.get("learner_count","")
                inst["course_count"]  = matched.get("courses","")  or matched.get("course_count","")

        # ── Resolve fields ────────────────────────────────────────────
        if em.get("api_skills"):
            final_skills, skills_src = em["api_skills"], "api"
        elif page["nd_skills"]:
            final_skills, skills_src = page["nd_skills"], "html"
        else:
            final_skills, skills_src = [], "none"

        final_tools      = page["nd_tools"]
        final_objectives = (em.get("learning_objectives") or page["nd_objectives"] or [])
        final_level      = (em.get("level_v2") or meta.get("level") or course.get("level_xlsx",""))
        pl               = meta.get("primary_languages",[])
        final_language   = em.get("language_v2") or (pl[0] if pl else "")

        total_videos   = sum(m.get("video_count",0)      for m in final_modules)
        total_readings = sum(m.get("reading_count",0)    for m in final_modules)
        total_assmnts  = sum(m.get("assignment_count",0) for m in final_modules)
        total_discuss  = sum(m.get("discussion_count",0) for m in final_modules)
        total_dur_min  = sum(m.get("total_duration_min",0) for m in final_modules)

        return {
            **{k: v for k, v in course.items() if not k.startswith("_")},
            **meta,
            "final_skills":        final_skills,
            "skills_source":       skills_src,
            "final_tools":         final_tools,
            "learning_objectives": final_objectives,
            "domain_types":        em.get("domain_types",[]),
            "final_level":         final_level,
            "final_language":      final_language,
            "subtitle_languages":  em.get("subtitle_languages",[]),
            "workload":            em.get("workload_v2") or meta.get("workload",""),
            "weekly_hours":        page["weekly_hours"],
            "duration_weeks":      page["duration_weeks"],
            "flexible_deadline":   page["flexible_deadline"],
            "course_rating":       page.get("course_rating",""),
            "num_ratings":         page.get("num_ratings",""),
            "instructors":         instructors,
            "partners":            partners,
            "modules":             final_modules,
            "modules_source":      modules_source,
            "total_modules":       len(final_modules),
            "total_videos":        total_videos,
            "total_readings":      total_readings,
            "total_assignments":   total_assmnts,
            "total_discussions":   total_discuss,
            "total_duration_min":  total_dur_min,
            "page_scraped":        page["page_scraped"],
            "scrape_status":       "success",
        }

    except Exception as e:
        log.error("Error scraping %s: %s", slug, str(e))
        return {**course, "scrape_status": "failed", "fail_reason": str(e)[:200]}


# ── CHECKPOINT ────────────────────────────────────────────────────────
def load_checkpoint():
    if Path(CHECKPOINT_FILE).exists():
        with open(CHECKPOINT_FILE,"r",encoding="utf-8") as f:
            return json.load(f)
    return {"completed_slugs":[], "results":[]}

_chunk_suffix = ""   # set in main() when chunk mode active

def save_checkpoint(completed_slugs, results, lock):
    with lock:
        with open(CHECKPOINT_FILE,"w",encoding="utf-8") as f:
            cp_file = f"cs_checkpoint{_chunk_suffix}.json"
            json.dump({"completed_slugs": completed_slugs, "results": results},
                      f, ensure_ascii=False, default=str)
        log.info("Checkpoint saved — %d done", len(completed_slugs))

# ── SAVE OUTPUTS ──────────────────────────────────────────────────────
def save_outputs(results):
    with open(OUTPUT_JSON,"w",encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    log.info("JSON saved → %s", OUTPUT_JSON)

    flat_rows = []
    for r in results:
        row = {}
        for k, v in r.items():
            if k == "modules":
                row["module_titles"]       = " | ".join(m.get("title","") for m in v)
                row["module_descriptions"] = " || ".join(
                    m.get("description","") for m in v if m.get("description"))
                summaries = []
                for m in v:
                    parts = []
                    if m.get("video_count"):
                        dur = f" {m['total_duration_min']}min" if m.get("total_duration_min") else ""
                        parts.append(f"{m['video_count']} videos{dur}")
                    if m.get("reading_count"):    parts.append(f"{m['reading_count']} readings")
                    if m.get("assignment_count"): parts.append(f"{m['assignment_count']} assignments")
                    if m.get("discussion_count"): parts.append(f"{m['discussion_count']} discussions")
                    summaries.append(m.get("title","") + (f" ({', '.join(parts)})" if parts else ""))
                row["module_summaries"]        = " | ".join(summaries)
                row["all_video_titles"]        = " | ".join(
                    v2["title"] for m in v for v2 in m.get("videos",[]) if v2.get("title"))
                row["all_reading_titles"]      = " | ".join(
                    r2["title"] for m in v for r2 in m.get("readings",[]) if r2.get("title"))
                row["all_assignment_titles"]   = " | ".join(
                    a["title"] for m in v for a in m.get("assignments",[]) if a.get("title"))
                row["all_discussion_titles"]   = " | ".join(
                    d["title"] for m in v for d in m.get("discussions",[]) if d.get("title"))
            elif k == "instructors":
                row["instructor_names"]     = " | ".join(i.get("name","")         for i in v if i.get("name"))
                row["instructor_titles"]    = " | ".join(i.get("title","")        for i in v if i.get("title"))
                row["instructor_ratings"]   = " | ".join(str(i.get("rating",""))  for i in v if i.get("rating"))
                row["instructor_ratings_n"] = " | ".join(str(i.get("num_ratings","")) for i in v if i.get("num_ratings"))
                row["instructor_learners"]  = " | ".join(str(i.get("learner_count","")) for i in v if i.get("learner_count"))
                row["instructor_bios"]      = " | ".join(i.get("bio","")[:120]    for i in v if i.get("bio"))
            elif k == "partners":
                row["partner_names"]    = " | ".join(p.get("name","")    for p in v)
                row["partner_websites"] = " | ".join(p.get("website","") for p in v if p.get("website"))
            elif k == "learning_objectives":
                row[k] = " | ".join(str(o) for o in v if o)
            elif k == "final_skills":
                row[k] = " | ".join(str(s) for s in v if s)
            elif k == "final_tools":
                row[k] = " | ".join(str(t) for t in v if t)
            elif k == "domain_types":
                row[k] = " | ".join(str(d) for d in v if d)
            elif k == "subtitle_languages":
                row[k] = ", ".join(str(l) for l in v)
            elif isinstance(v, list):
                row[k] = ", ".join(str(x) for x in v)
            elif isinstance(v, dict):
                row[k] = json.dumps(v)
            else:
                row[k] = v
        flat_rows.append(row)

    df = pd.DataFrame(flat_rows)
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    log.info("CSV saved → %s  (%d rows, %d cols)", OUTPUT_CSV, len(df), len(df.columns))

# ── MAIN ──────────────────────────────────────────────────────────────
def main():
#    parser = argparse.ArgumentParser()
#    parser.add_argument("--excel",   required=True)
#    parser.add_argument("--cauth",   default="")
#    parser.add_argument("--workers", type=int,   default=5)
#    parser.add_argument("--delay",   type=float, default=1.5)
#    parser.add_argument("--limit",   type=int,   default=None)
#    parser.add_argument("--resume",  action="store_true")
#    parser.add_argument("--chunk-index", type=int, default=None,
#                        help="Which chunk to process (0-based)")
#    parser.add_argument("--chunk-total", type=int, default=None,
#                        help="Total number of chunks")
#    parser.add_argument("--test",    action="store_true",
#                        help="Test mode: 3 courses per sheet")
#    args = parser.parse_args()

    global CHECKPOINT_FILE, OUTPUT_JSON, OUTPUT_CSV
    if args.test:
        CHECKPOINT_FILE = "cs_test_checkpoint.json"
        OUTPUT_JSON     = "cs_test_dataset.json"
        OUTPUT_CSV      = "cs_test_dataset.csv"
        args.limit      = 3
        args.workers    = 2
        args.delay      = 2.0
        log.info("*** TEST MODE: 3 CS courses per sheet ***")

    log.info("="*60)
    log.info("Coursera CS Scraper v3")
    log.info("Excel=%s Workers=%d Delay=%.1f CAUTH=%s",
             args.excel, args.workers, args.delay, "SET" if args.cauth else "NOT SET")
    log.info("="*60)

    all_courses = load_cs_courses(args.excel, limit=args.limit)

    # ── Chunk mode: each job processes a slice of courses ──────────────
    chunk_suffix = ""
    if args.chunk_index is not None and args.chunk_total is not None:
        ci, ct = args.chunk_index, args.chunk_total
        chunk_size = (len(all_courses) + ct - 1) // ct
        start_index = ci * chunk_size
        end_index = (ci + 1) * chunk_size 
        if end_index > len(all_courses):
            end_index = len(all_courses) 
        print(chunk_size, start_index, end_index-1)
        all_courses = all_courses[start_index: end_index]  #(ci + 1) * chunk_size]
        chunk_suffix = f"_chunk{ci:02d}of{ct:02d}"
        log.info("Chunk %d/%d: processing %d courses", ci, ct, len(all_courses))
        global _chunk_suffix
        _chunk_suffix = chunk_suffix
    if not all_courses:
        log.error("No CS courses found."); sys.exit(1)

    results, completed_slugs = [], set()
    if args.resume and Path(CHECKPOINT_FILE).exists():
        cp = load_checkpoint()
        results         = cp.get("results",[])
        completed_slugs = set(cp.get("completed_slugs",[]))
        log.info("Resuming: %d already done", len(completed_slugs))

    pending = [c for c in all_courses if c.get("slug") not in completed_slugs]
    log.info("Pending: %d / %d", len(pending), len(all_courses))

    if not pending:
        save_outputs(results); return

    lock             = Lock()
    done_count       = [len(completed_slugs)]
    total            = len(all_courses)
    since_checkpoint = [0]
    start_time       = time.time()

    for c in pending:
        result = scrape_course(c, args.cauth)
        results.append(result)
        #slug = c.get("slug") or ""
        #if slug: completed_slugs.add(slug)
        done_count[0] += 1
        since_checkpoint[0] += 1
        elapsed    = time.time() - start_time
        per_course = elapsed / done_count[0] if done_count[0] else 0
        remaining  = (total - done_count[0]) * per_course
        log.info(
            "[%d/%d] %-40s | %s | skills=%d tools=%d obj=%d "
            "mods=%d vids=%d reads=%d assigns=%d | src=%s | ETA %dm",
            done_count[0], total,
            str(result.get("scraped_name") or result.get("xlsx_name",""))[:40],
            result.get("scrape_status","?"),
            len(result.get("final_skills",[])),
            len(result.get("final_tools",[])),
            len(result.get("learning_objectives",[])),
            result.get("total_modules",0),
            result.get("total_videos",0),
            result.get("total_readings",0),
            result.get("total_assignments",0),
            result.get("modules_source","?"),
            int(remaining/60),
        )
        if since_checkpoint[0] >= CHECKPOINT_EVERY:
            save_checkpoint(list(completed_slugs), results, lock)
            since_checkpoint[0] = 0

    log.info("="*60)
    save_checkpoint(list(completed_slugs), results, lock)
    save_outputs(results)

    success = [r for r in results if r.get("scrape_status") == "success"]
    failed  = [r for r in results if r.get("scrape_status") == "failed"]
    log.info("Total=%d | Success=%d | Failed=%d", len(results), len(success), len(failed))

    if success:
        checks = [
            ("Skills",      lambda r: bool(r.get("final_skills"))),
            ("Tools",       lambda r: bool(r.get("final_tools"))),
            ("Objectives",  lambda r: bool(r.get("learning_objectives"))),
            ("Modules",     lambda r: r.get("total_modules",0) > 0),
            ("Videos",      lambda r: r.get("total_videos",0) > 0),
            ("Readings",    lambda r: r.get("total_readings",0) > 0),
            ("Assignments", lambda r: r.get("total_assignments",0) > 0),
            ("Instructor",  lambda r: any(i.get("name") for i in r.get("instructors",[]))),
            ("Inst.Rating", lambda r: any(i.get("rating") for i in r.get("instructors",[]))),
        ]
        log.info("COVERAGE:")
        for label, fn in checks:
            n = sum(1 for r in success if fn(r))
            log.info("  %-14s %d/%d (%d%%)", label, n, len(success), round(n/len(success)*100))

    elapsed = time.time() - start_time
    log.info("Time: %dm %ds | Outputs: %s %s",
             int(elapsed//60), int(elapsed%60), OUTPUT_JSON, OUTPUT_CSV)

if __name__ == "__main__":
    main()