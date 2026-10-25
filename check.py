import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

PART = os.environ.get("PART", "MK1F4LL/A")
ZIP = os.environ.get("ZIP", "94103")
PRODUCT_URL = "https://www.apple.com/shop/buy-iphone/iphone-duo"
TO_EMAIL = os.environ.get("TO_EMAIL", "luisunion11@hotmail.com")
STATE_FILE = Path(__file__).with_name("state.json")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Apple returns a 404 page for this endpoint unless the request comes from a real browser session.
FETCH_JS = """async (url) => {
  const r = await fetch(url, {headers: {Accept: 'application/json'}});
  return {status: r.status, body: await r.text()};
}"""


def fetch_stores():
    url = f"/shop/fulfillment-messages?fae=true&pl=true&mts.0=regular&parts.0={PART}&location={ZIP}"
    with sync_playwright() as p:
        headless = os.environ.get("HEADED") != "1"
        browser = p.chromium.launch(
            channel=os.environ.get("BROWSER_CHANNEL") or None,
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"] + (["--headless=new"] if headless else []),
        )
        ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1440, "height": 900})
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = ctx.new_page()
        page.goto(PRODUCT_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        res = page.evaluate(FETCH_JS, url)
        browser.close()
    if res["status"] != 200:
        raise RuntimeError(f"Apple returned HTTP {res['status']}: {res['body'][:300]}")
    stores = json.loads(res["body"])["body"]["content"]["pickupMessage"]["stores"]
    out = []
    for s in stores:
        pa = s.get("partsAvailability", {}).get(PART, {})
        regular = pa.get("messageTypes", {}).get("regular", {})
        out.append({
            "store": s.get("storeName"),
            "city": s.get("city"),
            "miles": s.get("storedistance"),
            "pickupDisplay": pa.get("pickupDisplay"),
            "quote": pa.get("pickupSearchQuote"),
            "storeQuote": regular.get("storePickupQuote"),
            "product": regular.get("storePickupProductTitle"),
        })
    return out


def rule_based_hits(stores):
    return [
        s for s in stores
        if s["pickupDisplay"] == "available" and "today" in f"{s['quote']} {s['storeQuote']}".lower()
    ]


def popup(title, message):
    if sys.platform != "darwin":
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], check=False)
    subprocess.Popen([
        "osascript", "-e",
        f'display dialog "{esc(message)}" with title "{esc(title)}" buttons {{"OK"}} '
        f'default button "OK" with icon caution',
    ])


def send_email(subject, html):
    if not os.environ.get("RESEND_API_KEY"):
        print("RESEND_API_KEY not set, skipping email.")
        return
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps({
            "from": os.environ.get("FROM_EMAIL", "iPhone Duo Watch <onboarding@resend.dev>"),
            "to": [TO_EMAIL],
            "subject": subject,
            "html": html,
        }).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
            "Content-Type": "application/json",
            "User-Agent": "iphone-duo-watch",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print("Email sent:", r.read().decode())


def main():
    if "--test-popup" in sys.argv:
        popup(
            "iPhone Duo Watch (TEST)",
            "This is a test. When the iPhone Duo 256GB Night Sky is available for pickup today "
            "near 94103, you'll see a pop-up like this listing the stores.",
        )
        return

    if "--test-email" in sys.argv:
        send_email(
            "Test: iPhone Duo pickup watcher is set up",
            f"<p>This is a test. You'll get an email like this when the iPhone Duo ({PART}) "
            f"is available for in-store pickup today near {ZIP}.</p>",
        )
        return

    stores = fetch_stores()
    for s in stores:
        print(f"{s['store']:<28} {s['pickupDisplay']:<12} {s['quote']}")

    hits = {s["store"] for s in rule_based_hits(stores)}

    today = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    already = set(state.get(today, []))
    new_hits = sorted(hits - already)
    if not new_hits:
        print("No new pickup-today stock.")
        return

    rows = "".join(
        f"<li><b>{s['store']}</b> ({s['city']}, {s['miles']} mi): {s['storeQuote'] or s['quote']}</li>"
        for s in stores if s["store"] in new_hits
    )
    popup(
        "iPhone Duo available for pickup TODAY",
        "\n".join(
            f"{s['store']} ({s['city']}, {s['miles']} mi): {s['storeQuote'] or s['quote']}"
            for s in stores if s["store"] in new_hits
        ) + f"\n\nReserve at {PRODUCT_URL}",
    )
    send_email(
        f"iPhone Duo available for pickup TODAY: {', '.join(new_hits)}",
        f"<p>{stores[0]['product'] or PART} is available for in-store pickup today:</p><ul>{rows}</ul>"
        f"<p><a href='{PRODUCT_URL}'>Reserve it on apple.com</a></p>",
    )
    STATE_FILE.write_text(json.dumps({today: sorted(already | hits)}, indent=2))


if __name__ == "__main__":
    main()
