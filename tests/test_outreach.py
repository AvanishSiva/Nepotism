import pytest

from job_agent.config import Config
from job_agent.outreach import drafts, sender
from job_agent.referrals.linkedin_people import Person

CFG = Config.model_validate({"profile": {
    "name": "Sivaavanish Kanagasabapathi", "short_name": "Siva", "headline": "Software Engineer, 3 years",
    "education": [{"degree": "MSc AI", "school": "National College of Ireland", "years": "2025"}],
    "past_employers": [{"company": "Zoho Corporation (ManageEngine)", "role": "MTS", "years": "2023",
                        "highlights": ["Built a Java load-balancing framework, cutting latency by 30%"]}],
    "projects": [{"name": "Job Tracker", "one_liner": "Serverless agent", "skills": []}],
    "unverified_achievements": ["Paper at a conference"], "other_work": [{"company": "Hospital"}]}})


def ctx(tags, lane="B", headline="Software Engineer at Stripe"):
    p = Person("Pranav Jain", headline, "https://www.linkedin.com/in/pj/", "2nd", 26, set(tags))
    return drafts.Context(p, "Stripe", ["Software Engineer, Supportability Decisions"], [63], None, lane,
                          drafts.shared_link(p, CFG, "Stripe", "Software Engineer"), drafts.proof_point(CFG, None))


def test_shared_link_uses_verified_tags_only():
    assert "studied at National College of Ireland" in ctx({"alum"}).shared_link
    assert "studied" not in ctx({"maybe_alum", "engineer"}).shared_link      # unverified → generic
    assert "Zoho" in ctx({"ex_colleague"}).shared_link
    assert "Zoho" not in ctx({"maybe_ex_colleague"}).shared_link


def test_facts_exclude_unverified_and_other_work():
    f = drafts.facts_block(CFG)
    assert "conference" not in f and "Hospital" not in f and "30%" in f


GOOD = ("Hi Pranav, I'm a software engineer in Dublin and came across your profile while looking at Stripe. "
        "I'm applying for the Software Engineer, Supportability Decisions role; at Zoho I built a Java "
        "load-balancing framework that cut latency by 30%. Would love to connect. Siva")


def test_validate_good_note():
    assert len(GOOD) <= 300
    assert drafts.validate(GOOD, ctx({"engineer"}), drafts.facts_block(CFG)) == []


@pytest.mark.parametrize("note,err", [
    (GOOD + " " + "x" * 60, "too long"),
    (GOOD.replace("Would love", "Could you refer me? Would love"), "referral"),
    (GOOD.replace("30%", "45%"), "numbers not in your facts"),
    (GOOD.replace("Pranav", "there"), "first name"),
    (GOOD.replace("Stripe", "the company"), "does not mention"),
    (GOOD + " 🚀", "emoji"),
])
def test_validate_rejects(note, err):
    assert any(err in e for e in drafts.validate(note, ctx({"engineer"}), drafts.facts_block(CFG)))


def test_template_fallback_is_valid():
    c = ctx({"engineer"})
    note = drafts.template(c, CFG)
    assert drafts.validate(note, c, drafts.facts_block(CFG)) == []


@pytest.mark.parametrize("labels,degree,state", [
    (["Connect", "Message", "More"], "2nd", "connect"),
    (["Invite Pranav Jain to connect"], "2nd", "connect"),
    (["Follow", "Message", "More actions"], "2nd", "connect_in_more"),
    (["Message", "More"], "1st", "connected"),
    (["Pending, click to withdraw invitation sent to Pranav Jain"], "2nd", "pending"),
    (["Message"], "2nd", "unknown"),           # Premium InMail: not proof of connection
    (["Follow"], "3rd", "follow_only"),
])
def test_connection_state(labels, degree, state):
    assert sender.connection_state(labels, "Pranav Jain", degree) == state


@pytest.mark.parametrize("target,page,ok", [
    ("Pranav Jain", "Pranav Jain", True), ("Pranav Jain", "Pranav Jain (He/Him)", True),
    ("Mícheál McMaugh", "Micheal McMaugh", True), ("Pranav Jain", "Priya Varma", False), ("Pranav Jain", "", False),
])
def test_name_verification(target, page, ok):
    assert sender.name_matches(target, page) is ok


def test_education_confirms_alum():
    edu = "Education\nNational College of Ireland\nMaster of Science - MSc, Data Analytics\n2021 - 2022"
    assert sender.education_confirms(edu, ["National College of Ireland"]) == "National College of Ireland"
    assert sender.education_confirms("Education\nUniversity of Limerick", ["National College of Ireland"]) is None
