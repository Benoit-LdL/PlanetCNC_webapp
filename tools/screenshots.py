"""Retake the README screenshots (media/screenshots/) from the demo dashboard.

Not needed on the Pi. On a PC with Google Chrome:

    pip install playwright
    python server.py --demo --port 8097 --log-dir none     # in one terminal
    python tools/screenshots.py                             # in another

Takes a phone (iPhone 14 size) and a desktop (1920x1080) set: the main view,
the 3D view on the phone, and the Console and Settings tabs.
"""
import argparse
import os

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
PI_FOLDER = "/home/mekanika/Desktop"  # typed into Settings > Files instead of this PC's folder (not saved)

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8097/")
ap.add_argument("--out", default=os.path.join(HERE, "..", "media", "screenshots"))
args = ap.parse_args()
os.makedirs(args.out, exist_ok=True)


def shot(page, name):
    page.screenshot(path=os.path.join(args.out, name + ".png"))


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")

    def open_page(**context):
        page = browser.new_context(**context).new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(7000)  # demo job running, 3D view drawn
        return page

    def open_tab(page, name, scroll=False):
        page.click(f'.ptab[data-ptab="{name}"]')
        if name == "settings":
            page.fill("#filesDirInput", PI_FOLDER)
        page.wait_for_timeout(1500)
        if scroll:  # phone: bring the tab panel into view
            page.evaluate("document.querySelector('.ptab.active').parentElement.closest('section,div')"
                          ".scrollIntoView({block: 'start'})")
            page.wait_for_timeout(500)

    phone = open_page(**dict(p.devices["iPhone 14"], device_scale_factor=2))
    shot(phone, "mobile")
    phone.evaluate("document.querySelector('.viewport').scrollIntoView({block: 'center'})")
    phone.wait_for_timeout(800)
    shot(phone, "mobile-3d")
    for tab in ("console", "settings"):
        open_tab(phone, tab, scroll=True)
        shot(phone, "mobile-" + tab)

    desk = open_page(viewport={"width": 1920, "height": 1080})
    shot(desk, "desktop")
    for tab in ("console", "settings"):
        open_tab(desk, tab)
        shot(desk, "desktop-" + tab)

    browser.close()
print("screenshots written to", os.path.abspath(args.out))
