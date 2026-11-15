"""LinkedIn job-alert email → JobPosting list.

Parsing order (stop at the first that accounts for every job link in the email):
  1. text/plain part   (LinkedIn's plain-text alerts are very regular)
  2. text/html part    (card layout: title link + "Company · Location")
  3. Ollama            (only for job IDs that really appear in the email's links)
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from job_agent.llm import ollama
from job_agent.normalize import infer_remote_type, norm_company
from job_agent.sources.base import JobPosting
from job_agent.sources.gmail_api import EmailMsg

log = logging.getLogger(__name__)

JOB_LINK = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?\s\"']*?-)?(\d{6,})", re.I)

# Card/footer lines that are never title, company or location.
_NOISE = re.compile(
    r"^(view job:?|apply with resume.*|easy apply|actively recruiting|promoted|"
    r"be an early applicant|this company is actively hiring|new|top applicant|"
    r"\d+\s+(connections?|applicants?|(school |company )?alum(ni|nus|na)?)\b.*|"
    r".*\bworks? here\b.*|fast growing( company)?|"
    r"(see|view) all jobs.*|manage (your )?(job )?alerts?.*|unsubscribe.*|help|"
    r"(\d+[+]? )?new jobs?\b.*|your job alert.*|jobs? similar to.*|salary.*|"
    r"your (other )?saved jobs.*|you have connections? at.*|ask them about the job.*|"
    r"(top )?jobs? (picked|recommended) for you.*|apply now.*|"
    r"€|\$|£)$",
    re.I,
)
_SALARY = re.compile(r"^[€$£]\s?\d|^\d[\d,.]*[kK]?\s?[-–]\s?[€$£]?\d")
_VIEW_JOB = re.compile(r"^\s*view job:", re.I | re.M)


def job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def job_ids_in(msg: EmailMsg) -> list[str]:
    """IDs of the job *cards* in the email.

    Plain-text alerts mark every card with a 'View job:' line; other job links
    (e.g. the seed job in "Jobs similar to X <link>") are not cards. Without a
    text part, fall back to every job link in the HTML.
    """
    seen: dict[str, None] = {}
    if _VIEW_JOB.search(msg.text):
        for line in msg.text.splitlines():
            if _VIEW_JOB.match(line) and (m := JOB_LINK.search(line)):
                seen.setdefault(m.group(1), None)
    else:
        for m in JOB_LINK.finditer(msg.text + "\n" + msg.html):
            seen.setdefault(m.group(1), None)
    return list(seen)


def _clean(line: str) -> str:
    return re.sub(r"\s+", " ", line.replace("‌", "").replace("\xa0", " ")).strip(" \t-–|")


def _meaningful(line: str) -> bool:
    return bool(line) and not _NOISE.match(line) and not _SALARY.match(line) and "http" not in line


def _make(job_id: str, title: str, company: str, location: str | None, msg: EmailMsg) -> JobPosting:
    return JobPosting(
        source="gmail_linkedin", company=company.strip(), title=title.strip(),
        location=(location or "").strip() or None,
        remote_type=infer_remote_type(location),
        linkedin_job_id=job_id, linkedin_job_url=job_url(job_id),
        alert_date=msg.date, posted_at=None,
    )


# -- 1. plain text ---------------------------------------------------------

def parse_text(msg: EmailMsg) -> list[JobPosting]:
    """LinkedIn plain-text card:

        Title
        Company
        Location
        [Fast growing]            <- badges, optionally after a blank line
        [3 company alumni]
        View job: https://www.linkedin.com/comm/jobs/view/<id>/...

    The card is the first blank-line-separated run (since the last separator)
    with >= 2 meaningful lines; title/company/location are its first 3 lines.
    That way unknown badges below the card and header text above the first
    card (all noise/links) don't shift the fields.
    """
    out: dict[str, JobPosting] = {}
    runs: list[list[str]] = [[]]
    ids = set(job_ids_in(msg))
    for raw in msg.text.splitlines():
        line = _clean(raw)
        m = JOB_LINK.search(raw)
        if m and m.group(1) in ids:
            jid = m.group(1)
            card = next((r for r in runs if len(r) >= 2), None)
            if jid not in out and card:
                title, company = card[0], card[1]
                location = card[2] if len(card) >= 3 else None
                if " · " in company and location is None:
                    company, location = company.split(" · ", 1)
                out[jid] = _make(jid, title, company, location, msg)
            runs = [[]]
            continue
        if re.fullmatch(r"-{5,}", line):
            runs = [[]]
        elif not line:
            if runs[-1]:
                runs.append([])
        elif _meaningful(line):
            runs[-1].append(line)
    return list(out.values())


# -- 2. HTML ---------------------------------------------------------------

def parse_html(msg: EmailMsg) -> list[JobPosting]:
    if not msg.html:
        return []
    soup = BeautifulSoup(msg.html, "html.parser")
    anchors: dict[str, list] = {}
    for a in soup.find_all("a", href=True):
        m = JOB_LINK.search(a["href"])
        if m:
            anchors.setdefault(m.group(1), []).append(a)

    out = []
    for jid, links in anchors.items():
        # Smallest ancestor of the first link that contains no other job's link = the card.
        card = links[0]
        node = links[0]
        while node.parent is not None:
            parent = node.parent
            ids = {m.group(1) for a in parent.find_all("a", href=True) if (m := JOB_LINK.search(a["href"]))}
            if ids - {jid}:
                break
            card = node = parent
        strings = [s for s in (_clean(x) for x in card.stripped_strings) if _meaningful(s)]
        link_texts = [t for t in (_clean(a.get_text(" ")) for a in links) if _meaningful(t)]
        title = link_texts[0] if link_texts else (strings[0] if strings else "")
        rest = [s for s in strings if s != title]
        company = location = None
        dot = next((s for s in rest if " · " in s), None)
        if dot:
            company, location = (p.strip() for p in dot.split(" · ", 1))
        elif len(rest) >= 2:
            company, location = rest[0], rest[1]
        elif rest:
            company = rest[0]
        if title and company and f" {company} · " in title:
            title = title.split(f" {company} · ")[0].strip()
        if title and company:
            out.append(_make(jid, title, company, location, msg))
    return out


# -- 3. LLM fallback -------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {"jobs": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"}, "title": {"type": "string"},
            "company": {"type": "string"}, "location": {"type": "string"},
        },
        "required": ["job_id", "title", "company", "location"],
    }}},
    "required": ["jobs"],
}


def parse_llm(msg: EmailMsg, model: str) -> list[JobPosting]:
    ids = set(job_ids_in(msg))
    if not ids:
        return []
    body = msg.text or BeautifulSoup(msg.html, "html.parser").get_text("\n")
    prompt = (
        "This is a LinkedIn job-alert email. List every job in it. job_id is the number in "
        "its linkedin.com/jobs/view/<number> link. Use empty string for unknown location.\n\n"
        + ollama.wrap_data(body[:15000])
    )
    data = ollama.chat(prompt, _SCHEMA, model=model)
    out = []
    for j in data.get("jobs", []):
        jid = str(j.get("job_id", "")).strip()
        if jid in ids and j.get("title") and j.get("company"):   # drop anything invented
            out.append(_make(jid, j["title"], j["company"], j.get("location"), msg))
    return out


def merge_text_html(text_jobs: list[JobPosting], html_jobs: list[JobPosting]) -> list[JobPosting]:
    """Text gives clean title/company; HTML gives location with work type
    ("Dublin (Hybrid)"). If they disagree on the company, the text parser
    misread the card (e.g. a section heading) and the HTML card wins."""
    by_id = {j.linkedin_job_id: j for j in html_jobs}
    out = []
    for t in text_jobs:
        h = by_id.get(t.linkedin_job_id)
        if h is None:
            out.append(t)
        elif norm_company(h.company) != norm_company(t.company):
            out.append(h)
        else:
            if h.location:
                t.location, t.remote_type = h.location, h.remote_type
            out.append(t)
    return out


def parse_alert(msg: EmailMsg, ollama_model: str | None = None) -> tuple[list[JobPosting], str]:
    """Returns (jobs, method). method ∈ text+html|text|html|llm|none."""
    expected = len(job_ids_in(msg))
    if expected == 0:
        return [], "none"
    try:
        tj, hj = parse_text(msg), parse_html(msg)
    except Exception as e:
        log.warning("text/html parse failed on %s: %s", msg.id, e)
        tj = hj = []
    if len(tj) == expected and hj:
        return merge_text_html(tj, hj), "text+html"
    best: tuple[list[JobPosting], str] = ([], "none")
    for name, fn in (("text", parse_text), ("html", parse_html)):
        try:
            jobs = fn(msg)
        except Exception as e:   # a layout change shouldn't crash the run
            log.warning("%s parser failed on %s: %s", name, msg.id, e)
            continue
        if len(jobs) == expected:
            return jobs, name
        if len(jobs) > len(best[0]):
            best = (jobs, name)
    if ollama_model and len(best[0]) < expected:
        try:
            jobs = parse_llm(msg, ollama_model)
            if len(jobs) > len(best[0]):
                best = (jobs, "llm")
        except Exception as e:
            log.warning("LLM parse failed on %s: %s", msg.id, e)
    if len(best[0]) < expected:
        log.warning("email %s: parsed %d of %d job links (%s)", msg.id, len(best[0]), expected, best[1])
    return best
