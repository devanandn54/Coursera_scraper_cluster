#!/usr/bin/env python3
"""
Raw API debugger — run this on the cluster to see exactly what 
each Coursera endpoint returns for known courses.
Usage: python3 debug_api.py --cauth "YOUR_CAUTH_HERE"
"""
import argparse, json, sys, time
import requests

def make_session(cauth):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://www.coursera.org/",
    })
    if cauth:
        s.cookies.set("CAUTH", cauth, domain="www.coursera.org", path="/")
        s.cookies.set("CAUTH", cauth, domain=".coursera.org", path="/")
    return s

def get(session, url, params=None):
    try:
        r = session.get(url, params=params or {}, timeout=20)
        print(f"  Status: {r.status_code}")
        if r.status_code == 200:
            return r.json()
        else:
            print(f"  Body: {r.text[:300]}")
            return None
    except Exception as e:
        print(f"  ERROR: {e}")
        return None

def dump(label, obj, max_keys=10):
    if obj is None:
        print(f"  {label}: None")
        return
    if isinstance(obj, list):
        print(f"  {label} [{len(obj)} items]: {json.dumps(obj[:3], default=str)[:300]}")
    elif isinstance(obj, dict):
        keys = list(obj.keys())[:max_keys]
        print(f"  {label} keys: {keys}")
        for k in keys[:5]:
            v = obj[k]
            print(f"    [{k}]: {json.dumps(v, default=str)[:150]}")
    else:
        print(f"  {label}: {str(obj)[:200]}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cauth", default="")
    parser.add_argument("--slug", default="machine-learning")
    args = parser.parse_args()

    session = make_session(args.cauth)
    BASE = "https://www.coursera.org/api"
    slug = args.slug

    print("="*70)
    print(f"DEBUG: {slug}")
    print(f"CAUTH set: {bool(args.cauth)}")
    print("="*70)

    # ── 1. onDemandCourses.v1 ─────────────────────────────────────────
    print("\n[1] onDemandCourses.v1")
    d = get(session, f"{BASE}/onDemandCourses.v1", {
        "q": "slug", "slug": slug,
        "includes": "instructorIds,partnerIds",
        "fields": "name,slug,description,level,primaryLanguages,workloadDescription,"
                  "skills,domainTypes,tagline"
    })
    if d and d.get("elements"):
        el = d["elements"][0]
        course_id = el.get("id","")
        print(f"  course_id: {course_id}")
        print(f"  name: {el.get('name','')}")
        print(f"  skills field: {el.get('skills','MISSING')}")
        print(f"  domainTypes: {el.get('domainTypes','MISSING')}")
        print(f"  instructorIds: {el.get('instructorIds','MISSING')}")
        print(f"  level: {el.get('level','')}")
    else:
        print("  FAILED or no elements")
        course_id = ""
    time.sleep(1)

    # ── 2. courses.v1 enriched ────────────────────────────────────────
    if course_id:
        print(f"\n[2] courses.v1/{course_id}")
        d2 = get(session, f"{BASE}/courses.v1/{course_id}", {
            "fields": "name,skills,domainTypes,learningObjectives,workload,level,"
                      "language,subtitleLanguages",
        })
        if d2 and d2.get("elements"):
            el2 = d2["elements"][0]
            print(f"  skills: {el2.get('skills','MISSING')}")
            print(f"  domainTypes: {str(el2.get('domainTypes','MISSING'))[:200]}")
            print(f"  learningObjectives count: {len(el2.get('learningObjectives',[]))}")
            print(f"  learningObjectives[0]: {str(el2.get('learningObjectives',[''])[0])[:200]}")
        time.sleep(1)

    # ── 3. instructors.v1 — test multiple field formats ──────────────
    print("\n[3] instructors.v1 — testing field formats")
    # First get instructor IDs from step 1
    if d and d.get("elements"):
        inst_ids = d["elements"][0].get("instructorIds", [])
        print(f"  Instructor IDs: {inst_ids}")
        if inst_ids:
            ids_str = ",".join(inst_ids[:2])
            # Try format A: fields as comma list
            print("  Format A: fields=avgRating,numRatings,numStudents")
            d3a = get(session, f"{BASE}/instructors.v1", {
                "ids": ids_str,
                "fields": "avgRating,numRatings,numStudents,numCourses,fullName,title",
            })
            if d3a and d3a.get("elements"):
                for inst in d3a["elements"]:
                    print(f"    {inst.get('fullName','?')}: avgRating={inst.get('avgRating','MISSING')} "
                          f"numRatings={inst.get('numRatings','MISSING')} "
                          f"numStudents={inst.get('numStudents','MISSING')}")
                    print(f"    All keys: {list(inst.keys())}")
            time.sleep(1)
    
    # ── 4. onDemandCourseMaterials.v2 — raw dump ─────────────────────
    print("\n[4] onDemandCourseMaterials.v2 — RAW (no extra fields param)")
    d4 = get(session, f"{BASE}/onDemandCourseMaterials.v2", {
        "q": "slug",
        "slug": slug,
        "includes": "modules,lessons,items",
        "showLockedItems": "true",
    })
    if d4:
        linked = d4.get("linked", {})
        print(f"  Top-level keys: {list(d4.keys())}")
        print(f"  linked keys: {list(linked.keys())}")
        
        # Check all possible module keys
        for k in linked.keys():
            if "module" in k.lower():
                mods = linked[k]
                print(f"\n  MODULE KEY: {k} ({len(mods)} modules)")
                if mods:
                    m0 = mods[0]
                    print(f"  Module[0] keys: {list(m0.keys())}")
                    print(f"  Module[0] name: {m0.get('name','')}")
                    print(f"  Module[0] timeCommitment: {m0.get('timeCommitment','MISSING')}")
                    print(f"  Module[0] elements: {str(m0.get('elements','MISSING'))[:200]}")
                    print(f"  Module[0] fullData: {json.dumps(m0, default=str)[:500]}")

        # Check lesson keys
        for k in linked.keys():
            if "lesson" in k.lower():
                lessons = linked[k]
                print(f"\n  LESSON KEY: {k} ({len(lessons)} lessons)")
                if lessons:
                    l0 = lessons[0]
                    print(f"  Lesson[0] keys: {list(l0.keys())}")
                    print(f"  Lesson[0]: {json.dumps(l0, default=str)[:400]}")

        # Check item keys
        for k in linked.keys():
            if "item" in k.lower():
                items = linked[k]
                print(f"\n  ITEM KEY: {k} ({len(items)} items)")
                if items:
                    i0 = items[0]
                    print(f"  Item[0] keys: {list(i0.keys())}")
                    print(f"  Item[0]: {json.dumps(i0, default=str)[:400]}")
                    if len(items) > 1:
                        print(f"  Item[1]: {json.dumps(items[1], default=str)[:400]}")
    time.sleep(1)

    # ── 5. Check page HTML for skills ─────────────────────────────────
    print(f"\n[5] Page HTML — check what's actually in the skills section")
    try:
        r = session.get(f"https://www.coursera.org/learn/{slug}", timeout=20)
        print(f"  Page status: {r.status_code}")
        html = r.text
        print(f"  Page size: {len(html)} chars")
        
        # Check for skills indicators
        import re
        skill_matches = re.findall(r"skills.{0,200}you.ll gain", html[:50000], re.I)
        print(f"  'Skills you'll gain' occurrences: {len(skill_matches)}")
        
        # Check for __NEXT_DATA__
        nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if nd:
            nd_size = len(nd.group(1))
            print(f"  __NEXT_DATA__ size: {nd_size} chars")
            try:
                nd_json = json.loads(nd.group(1))
                # Deep search for skills
                nd_str = json.dumps(nd_json)
                skill_in_nd = "skillsYouWillGain" in nd_str or "skillGain" in nd_str
                print(f"  skills in __NEXT_DATA__: {skill_in_nd}")
                # Check for syllabus
                syl = "syllabus" in nd_str.lower() or "modules" in nd_str.lower()
                print(f"  syllabus/modules in __NEXT_DATA__: {syl}")
            except:
                print("  Could not parse __NEXT_DATA__ as JSON")
        else:
            print("  No __NEXT_DATA__ found")
            
        # Check if page has dynamic content markers
        is_react = '<div id="rendered-content">' in html or 'window.__INITIAL_STATE__' in html
        print(f"  React/dynamic page: {is_react}")
        
        # Find actual skills section in HTML
        idx = html.find("Skills you") 
        if idx > 0:
            print(f"  Skills section HTML snippet: {html[idx:idx+500]}")
            
    except Exception as e:
        print(f"  Page error: {e}")

    print("\n" + "="*70)
    print("DEBUG COMPLETE")
    print("="*70)

def main_debug(cauth, slug):
    session = make_session(cauth)
    BASE = "https://www.coursera.org/api"

    print("="*70)
    print(f"DEBUG: {slug}  |  CAUTH set: {bool(cauth)}")
    print("="*70)

    d = get(session, f"{BASE}/onDemandCourses.v1", {
        "q":"slug","slug":slug,
        "includes":"instructorIds",
        "fields":"name,skills,domainTypes,tagline,instructorIds,level"
    })
    course_id = ""
    inst_ids = []
    if d and d.get("elements"):
        el = d["elements"][0]
        course_id = el.get("id","")
        inst_ids = el.get("instructorIds",[])
        print(f"course_id: {course_id}")
        print(f"skills field: {el.get('skills','MISSING')}")

    if inst_ids:
        import time
        time.sleep(1)
        d3 = get(session, f"{BASE}/instructors.v1", {"ids": ",".join(inst_ids[:2])})
        if d3 and d3.get("elements"):
            for inst in d3["elements"]:
                print(f"Instructor keys: {list(inst.keys())}")

    import time
    time.sleep(1)
    print("\n--- ITEM SLUGS ---")
    d4 = get(session, f"{BASE}/onDemandCourseMaterials.v2", {
        "q":"slug","slug":slug,"includes":"modules,lessons,items","showLockedItems":"true"
    })
    if d4:
        linked = d4.get("linked",{})
        items  = linked.get("onDemandCourseMaterialItems.v2") or []
        from collections import Counter
        prefixes = Counter(i.get("slug","").split("-")[0] for i in items)
        for p,c in prefixes.most_common(20):
            print(f"  prefix '{p}': {c} items")
        print("\nSample slugs:")
        for item in items[:25]:
            print(f"  {item.get('slug',''):<55} {item.get('name','')[:40]}")

    time.sleep(1)
    print("\n--- PAGE HTML: instructor rating area ---")
    try:
        r = session.get(f"https://www.coursera.org/learn/{slug}", timeout=20)
        html = r.text
        import re
        # Find instructor rating context
        for pattern in ["Ratings", "Learners", "avgRating", "instructorRating"]:
            idx = html.find(pattern)
            if idx > 0:
                print(f"  '{pattern}' found at {idx}: ...{html[max(0,idx-100):idx+200]}...")
                break
    except Exception as e:
        print(f"  Page error: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--cauth", default="")
    parser.add_argument("--slug", default="machine-learning")
    args = parser.parse_args()
    main_debug(args.cauth, args.slug)
