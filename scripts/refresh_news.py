"""Fetch configured RSS/Atom feeds. Never render publisher HTML."""
import concurrent.futures
import datetime as dt
import email.utils
import html
import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request
from pathlib import Path
import feedparser
import yaml

ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc

def public_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ValueError("Expected a public HTTPS URL")
    for result in socket.getaddrinfo(p.hostname, p.port or 443):
        if not ipaddress.ip_address(result[4][0]).is_global:
            raise ValueError("Non-public address")
    return url

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", str(value)))).strip()

def fetch(source, limit):
    req = urllib.request.Request(public_url(source["url"]), headers={"User-Agent": "KohuNews/1.0 (+https://www.kohu.pro/news/)", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    with urllib.request.build_opener(SafeRedirect()).open(req, timeout=25) as response:
        raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError("Feed exceeds size limit")
    parsed = feedparser.parse(raw)
    if not parsed.entries:
        raise ValueError("No RSS/Atom entries")
    rows = []
    for entry in parsed.entries:
        title = clean(entry.get("title", ""))[:300]
        link = entry.get("link", "")
        p = urllib.parse.urlsplit(link)
        if not title or p.scheme not in ("https", "http") or not p.hostname or p.username or p.password:
            continue
        labels = " ".join(clean(t.get("term", "")) for t in entry.get("tags", []))
        if re.search(r"\b(sponsored|sponsorship|deals|coupons|squid)\b", title + " " + labels, re.I):
            continue
        date = entry.get("published_parsed") or entry.get("updated_parsed")
        if not date:
            continue
        published = dt.datetime(*date[:6], tzinfo=UTC)
        if published > dt.datetime.now(UTC) + dt.timedelta(hours=1):
            continue
        link = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode([(k,v) for k,v in urllib.parse.parse_qsl(p.query) if not k.startswith("utm_")]), ""))
        rows.append({"title": title, "url": link, "published": published.isoformat(), "source": source["name"], "source_id": source["id"], "category": source["category"]})
    rows.sort(key=lambda r:r["published"], reverse=True)
    return rows[:limit]

def main():
    config = yaml.safe_load((ROOT / "_data/rss-feeds.yml").read_text())
    sources = [s for s in config["feeds"] if s.get("enabled", True)]
    ids = [s["id"] for s in sources]
    categories = {c["id"] for c in config["categories"]}
    if len(ids) != len(set(ids)) or any(s["category"] not in categories for s in sources):
        raise ValueError("Duplicate source ID or unknown category")
    limit = max(1, min(20, int(config["settings"]["max_items_per_source"])))
    total = max(1, min(200, int(config["settings"]["max_items_total"])))
    output = ROOT / "_data/news.json"
    old = json.loads(output.read_text()) if output.exists() else {}
    rows, statuses = [], []
    now = dt.datetime.now(UTC).isoformat()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {pool.submit(fetch, s, limit): s for s in sources}
        for job in concurrent.futures.as_completed(jobs):
            s = jobs[job]
            try:
                items = job.result()
                if not items:
                    raise ValueError("No eligible dated articles")
                rows.extend(items)
                statuses.append({"id":s["id"], "name":s["name"], "status":"ok", "last_success":now})
                print(s["id"] + ": " + str(len(items)) + " articles")
            except Exception as error:
                rows.extend(r for r in old.get("items",[]) if r["source_id"] == s["id"])
                previous = next((x for x in old.get("sources",[]) if x["id"] == s["id"]), {})
                statuses.append({"id":s["id"],"name":s["name"],"status":"unavailable","last_success":previous.get("last_success")})
                print("::warning::" + s["id"] + ": " + str(error)[:150])
    seen, unique = set(), []
    for row in sorted(rows, key=lambda r:r["published"], reverse=True):
        key = re.sub(r"\W+", "", row["title"].lower())
        if key in seen or row["url"] in seen:
            continue
        seen.update([key,row["url"]])
        unique.append(row)
    if not unique:
        raise RuntimeError("No articles available; retaining previous published site")
    output.write_text(json.dumps({"updated":now,"items":unique[:total],"sources":sorted(statuses,key=lambda s:s["name"])}, ensure_ascii=False, indent=2) + "\n")
if __name__ == "__main__":
    main()
