"""Attach to the user's real Chrome over CDP and read LinkedIn pages safely (spec §7, §9).

Discovery is read-only: navigate, wait, read text + profile links, and (on profiles) a short
human-like scroll so lazy sections such as Education load. No clicks or typing here; the
only write actions live in outreach/sender.py. Never launches a browser and never runs
headless; never closes the user's browser.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

import httpx

from job_agent.safety import Safety

log = logging.getLogger(__name__)
CDP_URL = "http://127.0.0.1:9222"

# Runs in the page: collect profile links with the text of their card and section heading.
_EXTRACT_JS = r"""
() => {
  const root = document.querySelector('main') || document.body;
  const out = [], seen = new Set();
  for (const a of root.querySelectorAll('a[href*="/in/"]')) {
    const m = a.href.match(/linkedin\.com\/in\/([^\/?#]+)/);
    if (!m) continue;
    const slug = decodeURIComponent(m[1]).toLowerCase();
    if (seen.has(slug)) continue;
    seen.add(slug);
    let el = a, card = a;
    for (let i = 0; i < 8 && el.parentElement; i++) {
      el = el.parentElement;
      const t = el.innerText || '';
      if (t.length > 700) break;
      card = el;
      if (el.tagName === 'LI') break;
    }
    const sec = a.closest('section');
    const h = sec ? ((sec.querySelector('h1,h2,h3') || {}).innerText || '') : '';
    out.push({slug, text: (card.innerText || '').slice(0, 700), section: h.slice(0, 120)});
  }
  const companies = [];
  for (const a of root.querySelectorAll('a[href*="/company/"]')) {
    const m = a.href.match(/linkedin\.com\/company\/([^\/?#]+)/);
    if (m) companies.push({slug: decodeURIComponent(m[1]).toLowerCase(), text: (a.innerText || '').slice(0, 120)});
  }
  return {cards: out, companies};
}
"""


@dataclass
class PageData:
    url: str
    final_url: str
    text: str
    cards: list[dict] = field(default_factory=list)
    companies: list[dict] = field(default_factory=list)


def chrome_available() -> bool:
    try:
        return httpx.get(f"{CDP_URL}/json/version", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


class LinkedInBrowser:
    """with LinkedInBrowser(safety) as b: data = b.view(url)"""

    def __init__(self, safety: Safety):
        self.safety = safety
        self._pw = self._browser = self._page = None
        self.views = 0

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(CDP_URL)
        if not self._browser.contexts:
            raise RuntimeError("Chrome has no open window/context")
        self._page = self._browser.contexts[0].new_page()   # our own tab; user's tabs untouched
        return self

    def __exit__(self, *exc):
        try:
            if self._page:
                self._page.close()
        finally:
            if self._pw:
                self._pw.stop()          # disconnects; the user's Chrome keeps running
        return False

    @property
    def page(self):
        return self._page

    def scroll_a_little(self, steps: tuple[int, int] = (3, 5)) -> None:
        """Wheel down a few screens with pauses (lazy sections load), then back to the top."""
        page = self._page
        total = 0
        for _ in range(random.randint(*steps)):
            dy = random.randint(350, 700)
            page.mouse.wheel(0, dy)
            total += dy
            time.sleep(random.uniform(0.7, 1.6))
        time.sleep(random.uniform(1.0, 2.0))
        page.mouse.wheel(0, -total - 400)
        time.sleep(random.uniform(0.8, 1.5))

    def body_text(self) -> str:
        try:
            return self._page.inner_text("body", timeout=5000)
        except Exception:
            return ""

    def view(self, url: str) -> PageData:
        self.safety.before_view()
        log.info("LinkedIn view %d: %s", self.views + 1, url)
        page = self._page
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass                          # LinkedIn keeps long-polling; idle may never come
        self.safety.settle_sleep()
        final = page.url
        try:
            text = page.inner_text("main", timeout=5000)
        except Exception:
            text = page.inner_text("body", timeout=5000)
        self.views += 1
        self.safety.after_view(final, text)            # counts the view; may raise SafetyStop
        data = page.evaluate(_EXTRACT_JS)
        return PageData(url=url, final_url=final, text=text, cards=data.get("cards", []),
                        companies=data.get("companies", []))


# Runs on a profile page: the top-card name, every button/link label in the top card,
# and the Education section text. Read-only.
_PROFILE_JS = r"""
() => {
  const main = document.querySelector('main') || document.body;
  // Name: the tab title "Pranav Jain | LinkedIn" (optionally prefixed "(3) ") is stable;
  // the h1 is a fallback (LinkedIn's newer profile layout has no h1 in <main>).
  const title = (document.title || '').replace(/^\(\d+\+?\)\s*/, '').split(' | ')[0].trim();
  const h1 = main.querySelector('h1');
  const name = title && !/linkedin/i.test(title) ? title : (h1 ? h1.innerText.trim() : '');
  // Top card = the first <section> in main; fall back to main.
  const top = (h1 && h1.closest('section')) || main.querySelector('section') || main;
  const labels = [];
  for (const b of top.querySelectorAll('button, a[role="button"], a[href*="/preload/custom-invite"]')) {
    const l = (b.getAttribute('aria-label') || b.innerText || '').trim().replace(/\s+/g, ' ');
    if (l && l.length < 120) labels.push(l);
    if (labels.length >= 12) break;
  }
  let education = '';
  const headings = [];
  const anchor = document.getElementById('education');          // <div id="education"> inside the section
  const eduSec = anchor && anchor.closest('section');
  if (eduSec) education = (eduSec.innerText || '').slice(0, 1500);
  for (const sec of main.querySelectorAll('section')) {
    const h = ((sec.querySelector('h2, h3') || {}).innerText || '').trim().split('\n')[0];
    if (h) headings.push(h.slice(0, 40));
    if (!education && /^education/i.test(h)) education = (sec.innerText || '').slice(0, 1500);
  }
  const dm = (top.innerText || '').match(/[·•]\s*(1st|2nd|3rd\+?)/);
  return {name, labels, education, degree: dm ? dm[1] : null, headings: headings.slice(0, 15)};
}
"""


def inspect_profile(browser: "LinkedInBrowser", url: str, *, scroll: bool = True) -> dict:
    """View a profile (counts as a page view, runs the kill switch), scroll a little so the
    Education section loads, and read the top card."""
    data = browser.view(url)
    if scroll:
        try:
            browser.scroll_a_little()
        except Exception as e:                       # scrolling is best-effort
            log.debug("scroll failed: %s", e)
    info = browser._page.evaluate(_PROFILE_JS)
    info["final_url"] = data.final_url
    return info
