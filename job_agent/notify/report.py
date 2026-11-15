"""Static, self-contained daily HTML report: reports/YYYY-MM-DD.html (spec §10)."""

from __future__ import annotations

import html
from pathlib import Path

from job_agent.db import PROJECT_ROOT
from job_agent.notify.digest import Digest, DigestJob

REPORTS = PROJECT_ROOT / "reports"

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--muted:#6b6b70;--line:#e4e4e0;--accent:#2f6fde;
--good:#1f8a4c;--warn:#b26a00;--bad:#b3261e;--chip:#eef1f6}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121214;--card:#1c1c1f;--fg:#ececee;
--muted:#9a9aa2;--line:#2c2c31;--accent:#6ea0ff;--good:#4cc27e;--warn:#e0a03a;--bad:#ff6b60;--chip:#26262c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 12px;color:var(--muted);
text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--muted);margin:0 0 16px}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 8px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px}
.stat b{font-size:20px;display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 10px}
.top{display:flex;gap:12px;align-items:flex-start;justify-content:space-between}
.title{font-weight:600}.co{color:var(--muted);font-size:14px}
.score{font-weight:700;font-size:18px;min-width:44px;text-align:right}
.s-hi{color:var(--good)}.s-mid{color:var(--accent)}
ul{margin:8px 0 0;padding-left:18px}li{margin:2px 0}
.warn{color:var(--warn)}.tag{display:inline-block;background:var(--chip);border-radius:6px;padding:1px 7px;
font-size:12px;margin:6px 6px 0 0;color:var(--muted)}
.new{background:var(--accent);color:#fff}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.links a{margin-right:14px;font-size:14px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;
overflow:hidden}td{padding:6px 12px;border-top:1px solid var(--line);font-size:14px}
tr:first-child td{border-top:0}td.n{text-align:right;width:60px;color:var(--muted)}
details summary{cursor:pointer;color:var(--muted)}
.note{white-space:pre-wrap;background:var(--chip);border-radius:8px;padding:10px 12px;margin:8px 0 0;font-size:14px}
button.copy{margin-top:8px;font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);
color:var(--fg);border-radius:6px;padding:3px 10px;cursor:pointer}
"""

_COPY_JS = """<script>document.addEventListener('click',e=>{const b=e.target.closest('button.copy');if(!b)return;
const t=document.getElementById(b.dataset.for).textContent;navigator.clipboard.writeText(t).then(()=>{
b.textContent='Copied';setTimeout(()=>b.textContent='Copy',1500)}).catch(()=>{});});</script>"""


def _outreach(items: list[dict]) -> str:
    cards = []
    for o in items:
        if o["lane"] == "A":
            what = "Follow-up: you send it" if o["followup"] else "Referral ask: you send it"
        else:
            what = {"queued": "Connection note (queued for the sender)", "sent": "Connection request pending",
                    "accepted": "Accepted"}.get(o["status"], o["status"])
        tags = "".join(f'<span class="tag">{_e(t)}</span>' for t in o["tags"] + ([o["degree"]] if o["degree"] else []))
        low = '<span class="tag warn">local model: check wording</span>' if o["low_quality"] else ""
        copy = (f'<button class="copy" data-for="o{o["id"]}">Copy</button>' if o["lane"] == "A" else "")
        cards.append(f"""<div class="card"><div class="title">{_e(o['name'])} <span class="co">· {_e(o['company'])}
· #{o['id']} [{o['lane']}]</span></div><div class="co">{_e(what)}{f' · {_e(o["note"])}' if o['note'] else ''}</div>
<div>{tags}{low}</div><div class="note" id="o{o['id']}">{_e(o['text'])}</div>{copy}
<div class="links" style="margin-top:8px"><a href="{_e(o['url'])}" target="_blank" rel="noopener">Profile ↗</a></div></div>""")
    return "<h2>Outreach</h2>" + "".join(cards) if cards else ""



def _e(s) -> str:
    return html.escape(str(s or ""))


def _card(j: DigestJob) -> str:
    cls = "s-hi" if j.score >= 8 else "s-mid"
    lines = "".join(f"<li>{_e(x)}</li>" for x in j.explanation)
    lines += "".join(f'<li class="warn">⚠ {_e(x)}</li>' for x in j.reasons)
    tags = []
    if j.is_new:
        tags.append('<span class="tag new">new</span>')
    if j.low_confidence:
        tags.append('<span class="tag">no JD: open manually</span>')
    f = j.facts or {}
    if f.get("years_required") is not None:
        tags.append(f'<span class="tag">{_e(f["years_required"])}+ yrs</span>')
    if f.get("seniority") and not j.low_confidence:
        tags.append(f'<span class="tag">{_e(f["seniority"])}</span>')
    if f.get("domain"):
        tags.append(f'<span class="tag">{_e(f["domain"])}</span>')
    if j.posted:
        tags.append(f'<span class="tag">posted {_e(j.posted)}</span>')
    links = []
    if j.url:
        links.append(f'<a href="{_e(j.url)}" target="_blank" rel="noopener">Apply / view ↗</a>')
    if j.linkedin_url and j.linkedin_url != j.url:
        links.append(f'<a href="{_e(j.linkedin_url)}" target="_blank" rel="noopener">LinkedIn ↗</a>')
    return f"""<div class="card"><div class="top"><div>
<div class="title">{_e(j.title)}</div><div class="co">{_e(j.company)} · {_e(j.location)} · #{j.id}</div>
</div><div class="score {cls}">{j.score:g}</div></div>
{f'<ul>{lines}</ul>' if lines else ''}<div>{''.join(tags)}</div>
<div class="links" style="margin-top:8px">{''.join(links)}</div></div>"""


def render(d: Digest) -> str:
    with_jd = [j for j in d.apply if not j.low_confidence]
    no_jd = [j for j in d.apply if j.low_confidence]
    skipped = "".join(f"<tr><td>{_e(k)}</td><td class='n'>{v}</td></tr>" for k, v in d.skipped_reasons.items())
    sections = []
    if d.referral:
        sections.append("<h2>Referral first</h2>" + "".join(_card(j) for j in d.referral))
    if with_jd:
        sections.append("<h2>Apply directly</h2>" + "".join(_card(j) for j in with_jd))
    if no_jd:
        sections.append("<h2>From LinkedIn alerts, no JD</h2>" + "".join(_card(j) for j in no_jd))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Job Agent {d.date:%Y-%m-%d}</title>
<style>{CSS}</style></head><body><main>
<h1>Job Agent: {d.date:%A %d %B %Y}</h1>
<p class="sub">Generated {d.date:%H:%M}. Scores 0–10: ≥ 8 referral first, 6–8 apply directly.</p>
<div class="stats">
<div class="stat"><b>{d.new_count}</b>new</div>
<div class="stat"><b>{d.counts.get('referral_first', 0)}</b>referral first</div>
<div class="stat"><b>{d.counts.get('apply_direct', 0)}</b>apply directly</div>
<div class="stat"><b>{d.counts.get('skip', 0)}</b>skipped</div></div>
{''.join(sections) or '<p>No jobs above the threshold today.</p>'}
{_outreach(d.outreach)}
<h2>Skipped</h2><details><summary>{d.counts.get('skip', 0)} jobs, by reason</summary>
<table>{skipped}</table></details>
<h2>Status</h2><p class="sub">{_e(d.linkedin_status)}</p>
</main>{_COPY_JS}</body></html>"""


def write(d: Digest) -> Path:
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{d.date:%Y-%m-%d}.html"
    path.write_text(render(d), encoding="utf-8")
    return path
