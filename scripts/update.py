#!/usr/bin/env python3
"""Refresh Nevada 84th Session BDR watchlist from NELIS and LegiScan.

Sources must BOTH succeed before any data is written. Never drops tracked items.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BASE = "https://www.leg.state.nv.us/App/NELIS/REL/84th2027"
NELIS = BASE + "/HomeBdr/GetBdrsForNavSubPanel"
API = "https://api.legiscan.com/"
UA = "BurkeConstruction-Nevada2027BDRTracker/1.0 (https://github.com/halliemcclintock/nevada-2027-bdr-tracker; weekly legislative research)"
LOG = logging.getLogger("update")
BDR_RE = re.compile(r"\b(?:BDR\s*)?(\d{1,2})\s*[-–]\s*(\d{1,5})\b", re.I)
BILL_RE = re.compile(r"\b(AB|SB|AJR|SJR|ACR|SCR)\s*[- ]?\s*(\d{1,5})\b", re.I)
NUMBER_RE = re.compile(r"\b(?:BDR\s*)?(\d{1,5})\b", re.I)


def load(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def request(session: requests.Session, url, *, params=None):
    response = session.get(url, params=params, timeout=35)
    response.raise_for_status()
    return response


def norm_bdr(text):
    match = BDR_RE.search(str(text or ""))
    return f"{int(match.group(1))}-{int(match.group(2))}" if match else None


def norm_bill(text):
    match = BILL_RE.search(str(text or ""))
    return (match.group(1).upper() + str(int(match.group(2)))) if match else None


def matches(item, watchlist):
    checked = " ".join(str(item.get(k) or "") for k in ("title", "summary", "reference", "watch_for"))
    result = []
    for term in watchlist:
        for phrase in term["patterns"].split(";"):
            phrase = phrase.strip()
            if phrase and re.search(r"\b" + re.escape(phrase), checked, flags=re.I):
                result.append(term)
                break
    return result


def parse_nelis(content, content_type=""):
    """NELIS nav endpoint may return markup, or a JSON collection of rows."""
    body = content.strip()
    rows = []
    if "json" in content_type.lower() or body.startswith(("[", "{")):
        payload = json.loads(body)
        def descend(node):
            if isinstance(node, list):
                for entry in node:
                    yield from descend(entry)
            elif isinstance(node, dict):
                keys = {str(k).lower(): k for k in node}
                num = next((node[keys[k]] for k in ("bdrnumber", "bdr_number", "bdrno", "bdr") if k in keys), None)
                summary = next((node[keys[k]] for k in ("summary", "description", "title", "subject") if k in keys), None)
                if num and summary:
                    bdr = norm_bdr(num) or (str(num).strip() if str(num).strip().isdigit() else None)
                    if bdr:
                        bill = next((node[keys[k]] for k in ("billnumber", "bill_number", "bill") if k in keys), "")
                        requester = next((node[keys[k]] for k in ("requester", "requestedby", "sponsor") if k in keys), "")
                        rows.append({"bdr": bdr, "summary": str(summary).strip(), "requester": str(requester or "").strip(), "bill": norm_bill(bill)})
                for value in node.values():
                    if isinstance(value, (list, dict)):
                        yield from descend(value)
        list(descend(payload))
    else:
        soup = BeautifulSoup(body, "html.parser")
        for tr in soup.select("tr"):
            cells = [el.get_text(" ", strip=True) for el in tr.find_all(["td", "th"], recursive=False)]
            if len(cells) < 2:
                continue
            joined = " ".join(cells)
            bdr = norm_bdr(cells[0])
            if not bdr:
                # Some 2027 draft requests have only a sequential number.
                candidate = re.search(r"^\s*(?:BDR\s*)?(\d{1,5})\s*$", cells[0], re.I)
                if candidate:
                    bdr = str(int(candidate.group(1)))
            if not bdr:
                continue
            bill = norm_bill(joined)
            middle = [x for x in cells[1:] if x and x.lower() not in ("view", "details")]
            summary = max(middle, key=len, default="")
            requester = next((x for x in middle if x != summary), "")
            if summary:
                rows.append({"bdr": bdr, "summary": summary, "requester": requester, "bill": bill})
    by_bdr = {r["bdr"]: r for r in rows if r["summary"]}
    return list(by_bdr.values())


def fetch_nelis(session):
    collected = {}
    empty = 0
    # A generous fixed range allows future filings; stop after three empty ranges.
    for start in range(0, 5000, 100):
        if start:
            time.sleep(1)
        response = request(session, NELIS, params={
            "docTypeId": 2, "startNumber": start, "endNumber": start + 99,
            "orderByNumber": "True"
        })
        batch = parse_nelis(response.text, response.headers.get("content-type", ""))
        for bdr in batch:
            collected[bdr["bdr"]] = bdr
        empty = empty + 1 if not batch else 0
        if empty >= 3 and start >= 299:
            break
    if not collected:
        raise RuntimeError("NELIS endpoint returned no parseable BDR records; check its live response schema before trusting updates")
    LOG.info("NELIS: %d unique BDR rows", len(collected))
    return collected


def legiscan(session, key, op, **params):
    response = request(session, API, params={"key": key, "op": op, **params})
    obj = response.json()
    if str(obj.get("status")) != "OK":
        raise RuntimeError(f"LegiScan {op}: {obj.get('alert') or obj.get('status') or 'unknown error'}")
    return obj


def find_session(session, key):
    sessions = legiscan(session, key, "getSessionList", state="NV").get("sessions", [])
    # Avoid choosing Nevada's previous session or a special session by year alone.
    candidates = [x for x in sessions if int(x.get("year_start", 0)) == 2027
                  and not bool(int(x.get("special") or 0))
                  and ("84" in str(x.get("session_name", "") + " " + x.get("session_title", ""))
                       or "regular" in str(x.get("session_name", "") + " " + x.get("session_title", "")).lower())]
    if not candidates:
        raise RuntimeError("Nevada 2027 84th regular session not yet in LegiScan session list")
    return int(candidates[0]["session_id"])


def fetch_legiscan(session, key, cache):
    sid = find_session(session, key)
    master = legiscan(session, key, "getMasterList", id=sid).get("masterlist", {})
    bills = {}
    new_cache = {}
    for entry in master.values():
        if not isinstance(entry, dict) or not entry.get("bill_id"):
            continue
        ident = str(entry["bill_id"])
        change_hash = str(entry.get("change_hash", ""))
        prior = cache.get(ident, {})
        if change_hash and prior.get("hash") == change_hash and prior.get("detail"):
            detail = prior["detail"]
        else:
            detail = legiscan(session, key, "getBill", id=ident).get("bill")
            if not isinstance(detail, dict):
                raise RuntimeError(f"LegiScan getBill {ident}: missing bill detail")
            time.sleep(0.1)
        new_cache[ident] = {"hash": change_hash, "detail": detail}
        number = norm_bill(detail.get("bill_number") or entry.get("number") or detail.get("title"))
        if number:
            bills[number] = detail
    if not isinstance(master, dict) or not master:
        raise RuntimeError("LegiScan master list is empty; refusing to update local data")
    LOG.info("LegiScan: %d bill details from session %s", len(bills), sid)
    return bills, new_cache


def nelis_link(detail):
    for key in ("state_link", "url"):
        link = str(detail.get(key) or "")
        if link.startswith("https://www.leg.state.nv.us/") and "/84th2027/" in link:
            return link
    # NELIS numeric bill identifiers cannot be inferred from LegiScan's bill_id.
    return None


def merge(items, bdrs, bills, watchlist, today):
    output = [dict(x) for x in items]
    changed = []
    existing_bdrs = {}
    existing_bills = {}
    # A 2025 veto or citation is *not* a 2027 bill.
    for obj in output:
        ref = str(obj.get("reference", ""))
        for m in re.finditer(r"\bBDR\s*(\d{1,2}\s*[-–]\s*\d+|\d+)\b", ref, re.I):
            raw = m.group(1)
            bdr = norm_bdr(raw) or str(int(raw))
            existing_bdrs.setdefault(bdr, obj)
        for match in BILL_RE.finditer(ref):
            prefix = ref[:match.start()]
            if "2025" in prefix[-12:]:
                continue
            existing_bills.setdefault(match.group(1).upper()+str(int(match.group(2))), obj)
    next_rank = max((int(o.get("rank", 0)) for o in output), default=0)
    used_ids = {str(o["id"]) for o in output}
    def add_new(key, summary, reference, stage, link, source):
        nonlocal next_rank
        candidate = {"title": summary, "summary": summary, "reference": reference, "watch_for": ""}
        if not matches(candidate, watchlist):
            return None
        next_rank += 1
        uid = f"A{next_rank:04d}"
        while uid in used_ids:
            next_rank += 1
            uid = f"A{next_rank:04d}"
        used_ids.add(uid)
        entry = {"id": uid, "rank": next_rank, "title": summary,
                 "reference": reference, "stage": stage, "section": "Added by weekly update",
                 "summary": summary, "watch_for": "", "link": link or "",
                 "link_label": "NELIS record" if link else "Source pending",
                 "date_added": today, "source": source}
        output.append(entry)
        changed.append(f"added {key}")
        return entry
    for bdr_id, bdr in bdrs.items():
        billnum = bdr.get("bill")
        detail = bills.get(billnum) if billnum else None
        if not detail:
            detail = next((v for v in bills.values() if norm_bdr(v.get("title")) == bdr_id), None)
            if detail:
                billnum = norm_bill(detail.get("bill_number"))
        item = existing_bdrs.get(bdr_id) or (existing_bills.get(billnum) if billnum else None)
        link = nelis_link(detail or {})
        stage = ("Introduced" if detail and detail.get("status") and
                 str(detail.get("status")) not in ("0", "") else "Prefiled bill") if billnum else "Draft request"
        if item:
            if billnum:
                old_ref = item.get("reference", "")
                if not re.search(r"\b" + re.escape(billnum) + r"\b", old_ref, re.I):
                    item["reference"] = (old_ref + " · " + billnum).strip(" ·")
                    changed.append(f"{item['id']}: linked {billnum}")
                if item.get("stage") != stage:
                    item["stage"] = stage
                    changed.append(f"{item['id']}: stage → {stage}")
                if link and item.get("link") != link:
                    item["link"], item["link_label"] = link, f"{billnum} on NELIS"
                    changed.append(f"{item['id']}: NELIS link")
                if not item.get("summary"):
                    item["summary"] = bdr["summary"]
                    changed.append(f"{item['id']}: filled summary")
                existing_bills[billnum] = item
            continue
        if billnum and billnum in existing_bills:
            continue
        record = add_new(bdr_id, bdr["summary"], f"BDR {bdr_id}" + (f" · {billnum}" if billnum else ""),stage,
                         link or BASE+"/Bdrs/List", "NELIS 84th Session BDR list")
        if record:
            existing_bdrs[bdr_id] = record
            if billnum:
                existing_bills[billnum] = record
    for number, detail in bills.items():
        if number in existing_bills:
            continue
        bdr_id = norm_bdr(detail.get("title", ""))
        if bdr_id and bdr_id in existing_bdrs:
            item = existing_bdrs[bdr_id]
            item["reference"] += " · " + number
            item["stage"] = "Introduced" if detail.get("status") else "Prefiled bill"
            link = nelis_link(detail)
            if link:
                item["link"], item["link_label"] = link, f"{number} on NELIS"
            changed.append(f"{item['id']}: linked {number}")
            existing_bills[number] = item
            continue
        summary = str(detail.get("description") or "").strip()
        if not summary:
            continue
        stage = "Introduced" if detail.get("status") else "Prefiled bill"
        record = add_new(number, summary, (f"BDR {bdr_id} · " if bdr_id else "") + number, stage,
                         nelis_link(detail) or BASE+"/Bdrs/List", "LegiScan Nevada 2027 session")
        if record:
            existing_bills[number] = record
    return output, changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    key = os.environ.get("LEGISCAN_API_KEY")
    if not key:
        raise RuntimeError("LEGISCAN_API_KEY secret is missing")
    items = load(DATA / "bdrs.json")
    watchlist = load(DATA / "watchlist.json")
    cache = load(DATA / "legiscan_cache.json", {})
    if not isinstance(items, list) or not isinstance(watchlist, list):
        raise RuntimeError("Missing or invalid existing watchlist data")
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"})
    # Fetch both sources before modifying files. A failure aborts visibly.
    bdrs = fetch_nelis(session)
    bills, refreshed_cache = fetch_legiscan(session, key, cache)
    today = dt.date.today().isoformat()
    updated, changes = merge(items, bdrs, bills, watchlist, today)
    if args.dry_run:
        LOG.info("DRY RUN: %d changes: %s", len(changes), changes)
        return
    if updated != items:
        save(DATA / "bdrs.json", updated)
    if refreshed_cache != cache:
        save(DATA / "legiscan_cache.json", refreshed_cache)
    meta = {"last_updated": today}
    if meta != load(DATA / "meta.json", {}):
        save(DATA / "meta.json", meta)
    if changes:
        with (DATA / "changelog.md").open("a", encoding="utf-8") as f:
            f.write(f"- {today}: " + "; ".join(changes) + "\n")
    LOG.info("Completed: %d changed entries, last_updated=%s", len(changes), today)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("Weekly update failed; source data remains unchanged")
        sys.exit(1)
