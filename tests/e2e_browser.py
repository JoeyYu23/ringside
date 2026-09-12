"""Browser end-to-end with screenshots (Playwright): accounts -> pre-call brief -> scripted call -> live cues -> debrief ->
insights / prospect seat / overlay. Measures transcript->cue DOM latency in the page. Needs a running server."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("COACH_BASE", "http://127.0.0.1:8765")
SHOTS = Path(os.environ.get("COACH_SHOTS", ".cache/shots"))
SHOTS.mkdir(parents=True, exist_ok=True)
OBSERVER = """() => { window.__lat = []; let lastTx = 0;
  new MutationObserver(() => { const n = document.querySelectorAll('#transcript p:not(.partial)').length; if (n !== window.__ntx) { window.__ntx = n; lastTx = performance.now(); } }).observe(document.getElementById('transcript'), {childList: true});
  new MutationObserver(() => { if (lastTx) { window.__lat.push(performance.now() - lastTx); lastTx = 0; } }).observe(document.getElementById('cue'), {childList: true, subtree: true}); }"""


def main() -> int:
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1280, "height": 860})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(BASE + "/"); page.wait_for_selector("tr.pick"); page.wait_for_timeout(600)
        page.screenshot(path=str(SHOTS / "01-accounts.png"))
        page.click('tr.pick[data-id="brooklyn-auto"]'); page.click('#modes .chip[data-id="scripted"]'); page.click("#go")
        page.wait_for_url("**/call/*"); cid = page.url.rsplit("/", 1)[-1]
        page.wait_for_selector("#pre:not([hidden])"); page.wait_for_timeout(400)
        page.screenshot(path=str(SHOTS / "02-precall-brief.png"))
        page.click("#startScripted"); page.wait_for_selector("#live:not([hidden])"); page.evaluate(OBSERVER)
        page.wait_for_function("document.querySelector('#cue .line')?.textContent.includes('call back then')", timeout=30000)
        page.wait_for_timeout(250); page.screenshot(path=str(SHOTS / "03-live-gatekeeper.png"))
        page.wait_for_function("document.querySelector('#cue .line')?.textContent.includes('Keep them')", timeout=40000)
        page.wait_for_timeout(250); page.screenshot(path=str(SHOTS / "04-live-objection.png"))
        page.wait_for_function("document.querySelector('#cue .line')?.textContent.includes('Stop selling')", timeout=40000)
        page.wait_for_timeout(250); page.screenshot(path=str(SHOTS / "05-live-soft-yes.png"))
        lat = page.evaluate("window.__lat")
        page.wait_for_selector("#debrief:not([hidden])", timeout=60000); page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / "06-debrief-rules.png"), full_page=True)
        try:
            page.wait_for_function("document.querySelector('#debrief .kicker')?.textContent.includes('LLM (')", timeout=90000)
            page.wait_for_timeout(300); page.screenshot(path=str(SHOTS / "07-debrief-llm.png"), full_page=True)
            llm = True
        except Exception:  # noqa: BLE001
            llm = False
        page.goto(BASE + "/insights"); page.wait_for_selector("#brokers tr"); page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "08-insights.png"), full_page=True)
        page.goto(BASE + f"/prospect/{cid}"); page.wait_for_selector("#try p"); page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "09-prospect-seat.png"))
        ov = b.new_page(viewport={"width": 720, "height": 260}); ov.goto(BASE + f"/overlay/{cid}"); ov.wait_for_timeout(800)
        ov.screenshot(path=str(SHOTS / "10-overlay.png"))
        # second call to the same account: memory shows on the accounts page
        page.goto(BASE + "/"); page.wait_for_selector("tr.pick"); page.wait_for_timeout(400)
        mem = page.inner_text('tr.pick[data-id="brooklyn-auto"] .mem')
        page.screenshot(path=str(SHOTS / "11-accounts-after.png"))
        b.close()
    print(f"call {cid} | transcript->cue DOM latency (ms): {[round(x, 1) for x in lat]} | max {max(lat) if lat else 0:.1f}")
    print(f"LLM debrief rendered: {llm} | memory line now: {mem[:110]}")
    print(f"console errors: {errors or 'none'} | screenshots: {sorted(f.name for f in SHOTS.glob('*.png'))}")
    return 0 if lat and max(lat) < 100 and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
