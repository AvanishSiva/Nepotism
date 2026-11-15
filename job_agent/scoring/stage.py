"""Score stage: hard filters → fact extraction (cached) → deterministic score → decision.

Commits after every job, so it can be interrupted (Ctrl-C) and resumed.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time

from job_agent.config import Config
from job_agent.normalize import norm_company, now_utc
from job_agent.scoring import extract as ex
from job_agent.scoring.filters import HardFilters
from job_agent.scoring.score import SkillMatcher, compute, explain, pick_project

log = logging.getLogger(__name__)


def _save(conn, job_id, facts_json, score, decision, reasons, explanation, low_conf, repost):
    conn.execute(
        """INSERT INTO job_scores(job_id, facts_json, score, decision, reasons_json, explanation,
                                  low_confidence, possible_repost, scored_at)
           VALUES(?,?,?,?,?,?,?,?,datetime('now'))
           ON CONFLICT(job_id) DO UPDATE SET facts_json=excluded.facts_json, score=excluded.score,
               decision=excluded.decision, reasons_json=excluded.reasons_json,
               explanation=excluded.explanation, low_confidence=excluded.low_confidence,
               possible_repost=excluded.possible_repost, scored_at=excluded.scored_at""",
        (job_id, facts_json, score, decision, json.dumps(reasons), explanation, int(low_conf), int(repost)),
    )
    conn.commit()


def run(conn: sqlite3.Connection, cfg: Config, *, job_ids: list[int] | None = None,
        reextract: bool = False, limit: int | None = None, use_claude: bool = True) -> dict:
    hf = HardFilters(cfg)
    matcher = SkillMatcher.from_config(cfg.profile, cfg.skills.get("synonyms") or {},
                                       cfg.skills.get("ignore") or [])
    agencies = {norm_company(a) for a in cfg.filters.get("agencies") or []}
    flag_patterns = [re.compile(p, re.I) for p in cfg.filters.get("jd_flag_patterns") or []]
    model = cfg.llm.get("ollama_model", "qwen2.5:14b")
    claude_bin = cfg.llm.get("claude_cli", "claude") if use_claude else None
    cfg_engine = cfg.llm.get("extract_engine", "ollama")
    claude_model = cfg.llm.get("claude_model")
    limit_hit = False
    thresholds = cfg.scoring.get("thresholds") or {}
    weights = cfg.scoring.get("weights")
    user_years = float(cfg.profile.get("years_experience") or 0)
    now = now_utc()

    q = "SELECT * FROM jobs"
    params: list = []
    if job_ids:
        q += f" WHERE id IN ({','.join('?' * len(job_ids))})"
        params = job_ids
    jobs = conn.execute(q + " ORDER BY id", params).fetchall()

    stats = {"jobs": len(jobs), "hard_skipped": 0, "extracted": 0, "cached": 0, "title_only": 0,
             "decisions": {}, "errors": [], "ollama_calls": 0, "claude_calls": 0}
    extracted_this_run = 0
    for job in jobs:
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (job["company_id"],)).fetchone() \
            if job["company_id"] else None
        cname = company["name"] if company else "?"
        prev = conn.execute("SELECT facts_json FROM job_scores WHERE job_id = ?", (job["id"],)).fetchone()
        prev_facts = json.loads(prev["facts_json"]) if prev and prev["facts_json"] else None

        hard = hf.check(conn, job, company)
        if hard:
            stats["hard_skipped"] += 1
            stats["decisions"]["skip"] = stats["decisions"].get("skip", 0) + 1
            _save(conn, job["id"], json.dumps(prev_facts) if prev_facts else None, 0.0, "skip",
                  hard, None, False, False)
            continue

        low_conf = not job["jd_text"]
        try:
            if low_conf:
                facts = ex.facts_from_title(job["title"])
                stats["title_only"] += 1
                engine = "title"
            else:
                h = ex.jd_hash(job["title"], job["jd_text"])
                if not reextract and prev_facts and prev_facts.get("_jd_hash") == h:
                    facts = ex.Facts.model_validate({k: v for k, v in prev_facts.items() if not k.startswith("_")})
                    engine = prev_facts.get("_engine", "cache")
                    stats["cached"] += 1
                else:
                    if limit is not None and extracted_this_run >= limit:
                        continue
                    t0 = time.monotonic()
                    facts, engine_used = ex.extract(
                        job["title"], cname, job["jd_text"], model=model, claude_binary=claude_bin,
                        engine="ollama" if limit_hit else cfg_engine, claude_model=claude_model, stats=stats)
                    limit_hit = limit_hit or bool(stats.get("claude_limit_hit"))
                    engine = engine_used
                    extracted_this_run += 1
                    stats["extracted"] += 1
                    log.info("extracted #%d %s | %s via %s in %.0fs", job["id"], cname, job["title"][:50],
                             engine, time.monotonic() - t0)
        except Exception as e:
            log.error("job #%d %s | %s: extraction failed: %s", job["id"], cname, job["title"], e)
            stats["errors"].append(f"#{job['id']}: {e}")
            continue

        res = compute(facts, matcher=matcher, user_years=user_years,
                      role_types=cfg.preferences.role_types, weights=weights, thresholds=thresholds,
                      jd_text=job["jd_text"], posted_at=job["posted_at"] or job["alert_date"],
                      first_seen_at=job["first_seen_at"], last_seen_at=job["last_seen_at"], now=now,
                      low_confidence=low_conf, flag_patterns=flag_patterns,
                      boost_keywords=cfg.skills.get("boost_keywords") or [],
                      is_agency=norm_company(cname) in agencies)
        explanation = None
        if res.score >= thresholds.get("apply_direct", 6) and not res.hard_skip:
            proj = pick_project(cfg.profile.get("projects") or [], facts.must_have + facts.nice_to_have, matcher)
            explanation = "\n".join(explain(res, facts, proj))

        facts_doc = facts.model_dump()
        if not low_conf:
            facts_doc["_jd_hash"] = ex.jd_hash(job["title"], job["jd_text"])
        facts_doc["_engine"] = engine
        facts_doc["_components"] = res.components
        _save(conn, job["id"], json.dumps(facts_doc), res.score, res.decision, res.reasons,
              explanation, res.low_confidence, res.possible_repost)
        stats["decisions"][res.decision] = stats["decisions"].get(res.decision, 0) + 1
    return stats
