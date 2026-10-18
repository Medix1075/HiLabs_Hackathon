import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline"))
from playwright.sync_api import sync_playwright
from ptd_dir.healthnet import SITE, _search
reqs=[]
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=os.environ.get("CHROME_PATH") or None)
    page=b.new_page()
    def on_req(r):
        if "pces" in r.url or "external-api" in r.url:
            reqs.append({"url":r.url,"method":r.method,"headers":r.headers,"post":r.post_data})
    page.on("request", on_req)
    page.goto(SITE, wait_until="networkidle", timeout=90000)
    try: page.click("text=Accept", timeout=3000)
    except Exception: pass
    box=page.get_by_label("Address, City, County or Zip Code"); box.fill("Los Angeles, CA"); page.wait_for_timeout(2500)
    box.press("ArrowDown"); box.press("Enter"); page.wait_for_timeout(1500)
    page.click('button:has-text("Continue")'); page.wait_for_load_state("networkidle"); page.wait_for_timeout(2500)
    r=page.locator('md-radio-button[value="2026"]')
    if r.count(): r.first.click(); page.wait_for_timeout(800)
    sel=page.locator("select").first
    opts=sel.evaluate("s => [...s.options].map(o => [o.value, o.text.trim()])")
    print(json.dumps(opts, indent=0))
    m=[o for o in opts if "PPO - Large" in o[1]]
    sel.select_option(value=m[0][0]); page.wait_for_timeout(800)
    page.click('button:has-text("Continue")'); page.wait_for_load_state("networkidle"); page.wait_for_timeout(2500)
    _search(page, "Family Medicine")
    b.close()
json.dump(reqs, open("reqs.json","w"), indent=1)
print(len(reqs))
for r in reqs: print(r["method"], r["url"][:150])
