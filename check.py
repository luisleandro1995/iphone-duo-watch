import json
import os
import random
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

PART = os.environ.get("PART", "MK1F4LL/A")
ZIP = os.environ.get("ZIP", "10001")
TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")
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


FULFILLMENT_URL = f"/shop/fulfillment-messages?fae=true&pl=true&mts.0=regular&parts.0={PART}&location={ZIP}"
CHROME_APP = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE_DIR = Path(__file__).with_name("chrome-profile")


class AppleBlocked(RuntimeError):
    pass


def real_chrome_ua():
    try:
        version = subprocess.run([CHROME_APP, "--version"], capture_output=True, text=True, timeout=30).stdout
        major = version.split()[-1].split(".")[0]
    except Exception:
        major = "130"
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def open_session(p):
    """Launch the installed Google Chrome with a persistent profile, so cookies carry over like a real visitor."""
    headless = os.environ.get("HEADED") != "1"
    use_real_chrome = sys.platform == "darwin" or os.environ.get("BROWSER_CHANNEL") == "chrome"
    args = ["--disable-blink-features=AutomationControlled"] + (["--headless=new"] if headless else [])
    opts = dict(
        headless=headless,
        args=args,
        user_agent=real_chrome_ua() if sys.platform == "darwin" else UA,
        locale="en-US",
        viewport={"width": 1440, "height": 900},
    )
    if use_real_chrome:
        opts["channel"] = "chrome"
    ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), **opts)
    ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    load_product_page(page)
    return ctx, page


def load_product_page(page):
    page.goto(PRODUCT_URL, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(4000)


def query_stores(page):
    res = page.evaluate(FETCH_JS, FULFILLMENT_URL)
    if res["status"] != 200:
        raise AppleBlocked(f"Apple returned HTTP {res['status']}")
    return parse_stores(res["body"])


def fetch_stores():
    with sync_playwright() as p:
        ctx, page = open_session(p)
        try:
            return query_stores(page)
        finally:
            ctx.close()


def parse_stores(body):
    stores = json.loads(body)["body"]["content"]["pickupMessage"]["stores"]
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
            f"near {ZIP}, you'll see a pop-up like this listing the stores.",
        )
        return

    if "--test-email" in sys.argv:
        send_email(
            "Test: iPhone Duo pickup watcher is set up",
            f"<p>This is a test. You'll get an email like this when the iPhone Duo ({PART}) "
            f"is available for in-store pickup today near {ZIP}.</p>",
        )
        return

    if "--loop" in sys.argv:
        run_loop()
        return

    stores = fetch_stores()
    for s in stores:
        print(f"{s['store']:<28} {s['pickupDisplay']:<12} {s['quote']}")
    alert_on_new_hits(stores)
    print("Done.")


def run_loop():
    interval = float(os.environ.get("INTERVAL_SECONDS", "30"))
    reload_every = 15 * 60
    backoff = 0
    with sync_playwright() as p:
        while True:
            try:
                ctx, page = open_session(p)
                last_reload = time.time()
                while True:
                    if time.time() - last_reload > reload_every:
                        load_product_page(page)
                        last_reload = time.time()
                    try:
                        stores = query_stores(page)
                    except AppleBlocked as e:
                        backoff = min(max(backoff * 2, 120), 900)
                        log(f"{e}; pausing {backoff // 60:.0f} min, then reloading the page")
                        time.sleep(backoff)
                        load_product_page(page)
                        last_reload = time.time()
                        continue
                    backoff = 0
                    available = [s for s in stores if s["pickupDisplay"] == "available"]
                    log(f"ok: {len(available)}/{len(stores)} stores show pickup "
                        + (", ".join(f"{s['store']} ({s['quote']})" for s in available) or ""))
                    alert_on_new_hits(stores)
                    time.sleep(interval * random.uniform(0.8, 1.2))
            except Exception as e:
                log(f"browser error: {str(e).splitlines()[0][:200]}; restarting Chrome in 60s")
                try:
                    ctx.close()
                except Exception:
                    pass
                time.sleep(60)


def log(msg):
    print(f"{datetime.now(ZoneInfo(TIMEZONE)):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def alert_on_new_hits(stores):
    hits = {s["store"] for s in rule_based_hits(stores)}

    today = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    already = set(state.get(today, []))
    new_hits = sorted(hits - already)
    if not new_hits:
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
