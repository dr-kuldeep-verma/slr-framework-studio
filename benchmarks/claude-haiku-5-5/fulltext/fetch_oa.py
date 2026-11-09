"""Fetch open-access copies of the records in wilson_sought.csv.

Only locations that OpenAlex lists as open access are tried. Nothing is
logged in, no credentials are sent, and a site that refuses is recorded as
refused, not retried another way. Every attempt is written to oa_log.csv.
"""
import csv, json, sys, time, urllib.request, urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "pdfs"; OUT.mkdir(exist_ok=True)
UA = "slr-framework-studio-benchmark/0.1 (open-access retrieval for a published evaluation)"

def get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")

def openalex(oid):
    wid = oid.rstrip("/").split("/")[-1].upper()
    data, _ = get(f"https://api.openalex.org/works/{wid}")
    return json.loads(data)

rows = list(csv.DictReader(open(HERE / "wilson_sought.csv", encoding="utf-8")))
log = []
for i, r in enumerate(rows, 1):
    rid = r["record_id"]; dest = OUT / f"{rid}.pdf"
    entry = dict(record_id=rid, label=r["label"], title=r["title"][:150], oa_status="", tried="", result="", landing="")
    if dest.exists():
        entry["result"] = "already have"; log.append(entry); continue
    try:
        w = openalex(r["openalex_id"])
    except Exception as e:
        entry["result"] = f"openalex error: {e}"; log.append(entry); continue
    oa = w.get("open_access") or {}
    entry["oa_status"] = oa.get("oa_status", "")
    urls = []
    pmcid = ((w.get("ids") or {}).get("pmcid") or "").rstrip("/").split("/")[-1]
    if pmcid:
        urls.append(f"https://europepmc.org/articles/{pmcid}?pdf=render")
    for loc in w.get("locations") or []:
        if loc.get("is_oa") and loc.get("pdf_url"):
            urls.append(loc["pdf_url"])
        if loc.get("is_oa") and loc.get("landing_page_url") and not entry["landing"]:
            entry["landing"] = loc["landing_page_url"]
    if not oa.get("is_oa") and not pmcid:
        entry["result"] = "not open access"; log.append(entry); print(i, rid, "not OA"); continue
    for u in dict.fromkeys(urls):
        entry["tried"] += u + " "
        try:
            data, ctype = get(u)
            if data[:5] == b"%PDF-":
                dest.write_bytes(data); entry["result"] = "downloaded"; break
            entry["result"] = f"not a PDF ({ctype[:30]})"
        except urllib.error.HTTPError as e:
            entry["result"] = f"refused HTTP {e.code}"
        except Exception as e:
            entry["result"] = f"error: {type(e).__name__}"
        time.sleep(1)
    if not urls:
        entry["result"] = "open access, but no PDF link; open the landing page yourself"
    log.append(entry)
    print(i, rid, entry["oa_status"], entry["result"])
    time.sleep(0.3)

with open(HERE / "oa_log.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(log[0])); w.writeheader(); w.writerows(log)
got = [e for e in log if e["result"] in ("downloaded", "already have")]
print(f"\nPDFs: {len(got)} of {len(rows)}; relevant: {sum(e['label']=='1' for e in got)} of {sum(r['label']=='1' for r in rows)}")
print("Details in oa_log.csv")
