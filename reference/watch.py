#!/usr/bin/env python3
"""GPU deal watcher: flags GPUs (and whole PCs containing them) priced far below the typical
eBay asking price, across eBay, Craigslist, OfferUp and Reddit/deal RSS, and alerts via Telegram.

Usage: watch.py [--dry-run] [--test-telegram] [--only ebay,pc,craigslist,offerup,rss]
"""
import csv, html, json, math, os, random, re, statistics, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, "state")
CFG = json.load(open(os.path.join(DIR, "config.json")))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
DRY = "--dry-run" in sys.argv
ONLY = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else None
os.makedirs(STATE, exist_ok=True)


def enabled(source):
    return ONLY is None or source in ONLY


def log(msg):
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line)
    with open(os.path.join(STATE, "runs.log"), "a") as f:
        f.write(line + "\n")


def load(name, default):
    try:
        return json.load(open(os.path.join(STATE, name)))
    except (OSError, ValueError):
        return default


def save(name, obj):
    tmp = os.path.join(STATE, name + ".tmp")
    json.dump(obj, open(tmp, "w"), indent=1)
    os.replace(tmp, os.path.join(STATE, name))


def env():
    vals = {}
    for line in open(os.path.join(DIR, ".env")):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            vals[k] = v.strip().strip('"')
    return vals


def telegram(text, quiet=False):
    if DRY:
        print(f"---- [dry-run telegram{' (quiet)' if quiet else ''}] ----\n{text}\n----------------------------")
        return
    e = env()
    data = urllib.parse.urlencode({"chat_id": e["TELEGRAM_CHAT_ID"], "text": text, "disable_web_page_preview": "true",
                                   "disable_notification": "true" if quiet else "false"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{e['TELEGRAM_BOT_TOKEN']}/sendMessage", data, timeout=20)


def fetch(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=25).read()


def money(s):
    m = re.search(r"\$\s?([\d,]+(?:\.\d\d)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def miles(lat, lon):
    h = CFG["home"]
    p1, p2 = math.radians(h["lat"]), math.radians(lat)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon - h["lon"]) / 2) ** 2
    return 3959 * 2 * math.asin(math.sqrt(a))


# ---------- classification ----------

def _rx(patterns):
    return re.compile("|".join(f"(?:{p})" for p in patterns))


HARD = _rx(CFG["hard_blacklist"])
SOFT = _rx(CFG["soft_flags"])
PC_SPEC = re.compile(CFG["pc_spec_regex"])
PC_PHRASE = re.compile(CFG["pc_phrase_regex"])
NOT_PC = re.compile(CFG["not_pc_regex"])
CARD = re.compile(CFG["card_regex"])
PC_WORD = re.compile(CFG["pc_word_regex"])
OTHER_GPU = re.compile(CFG["other_gpu_regex"])
COUNT_WORDS = [(re.compile(r"\b(8\s*x|x\s*8|eight)\s*$"), 8), (re.compile(r"\b(quad|4\s*x|x\s*4|four)\s*$"), 4),
               (re.compile(r"\b(dual|2\s*x|x\s*2|two|pair of)\s*$"), 2)]


def classify(title, body=""):
    """Returns None (ignore) or dict(model, count, is_pc, flags). Body text is used only to find the GPU."""
    t = title.lower()
    if HARD.search(t):
        return None
    model, pos = None, None
    # Only trust the description when the title is a computer that doesn't name some other GPU
    use_body = body and (PC_WORD.search(t) or PC_PHRASE.search(t) or PC_SPEC.search(t)) and not OTHER_GPU.search(t)
    for text in (t, body.lower() if use_body else ""):
        for m in CFG["models"]:
            mm = re.search(m["regex"], text)
            if mm and not any(x in text for x in m.get("exclude", [])):
                model, pos, src = m, mm.start(), text
                break
        if model:
            break
    if not model:
        return None
    count = 1
    before = re.sub(r"(nvidia|geforce|rtx|radeon|amd|quadro|tesla|\s)+$", "", src[max(0, pos - 30):pos])
    for rx, n in COUNT_WORDS:
        if rx.search(before):
            count = n
            break
    is_pc = bool(PC_PHRASE.search(t) or (PC_SPEC.search(t) and not CARD.search(t))) and not NOT_PC.search(t)
    if src is not t:  # GPU only mentioned in the body: almost always a whole system
        is_pc = True
    return {"model": model, "count": count, "is_pc": is_pc, "flags": sorted(set(SOFT.findall(t)) - {""})}


def tier(total, med, c, seller_ok=True, local=False):
    """Returns (tier, ratio, reasons) or None."""
    value = med * c["count"]
    ratio = total / value
    limit = CFG["pc_ratio"] if c["is_pc"] else CFG["local_deal_ratio"] if local else CFG["deal_ratio"]
    longshot_limit = CFG["pc_ratio"] if c["is_pc"] else CFG["deal_ratio"]
    if ratio > longshot_limit or ratio < CFG["longshot_floor"]:
        return None
    reasons = []
    if ratio > limit:
        reasons.append(f"only {round((1 - ratio) * 100)}% below eBay typical — may just be normal local pricing")
    if ratio < CFG["scam_ratio"]:
        reasons.append(f"price is {round((1 - ratio) * 100)}% below typical — scam/broken risk")
    if c["flags"]:
        reasons.append("title mentions: " + ", ".join(c["flags"]))
    if not seller_ok:
        reasons.append("seller has little or mixed feedback")
    if reasons:
        return "LONG SHOT", ratio, reasons
    return ("PC DEAL" if c["is_pc"] else "DEAL"), ratio, []


def median_for(medians, model):
    info = medians.get(model["name"]) or {}
    if info.get("src") == "fallback" or not info:
        return model["fallback_median"] if CFG["alert_on_fallback"] else None
    return info["median"]


def candidate(source, key, title, url, total, c, medians, seen, seller_ok=True, **extra):
    if key in seen:
        return None
    med = median_for(medians, c["model"])
    if med is None:
        return None
    t = tier(total, med, c, seller_ok, local=source != "eBay")
    if not t:
        return None
    return dict(extra, source=source, key=key, title=title, url=url, total=total, cls=c, median=med,
                tier=t[0], ratio=t[1], reasons=t[2])


# ---------- eBay (Playwright) ----------

EXTRACT_JS = """() => [...document.querySelectorAll('li.s-card')].map(c => {
  const q = s => c.querySelector(s);
  const rows = [...c.querySelectorAll('.s-card__attribute-row')].map(r => r.innerText.trim());
  const sec = q('.su-card-container__attributes__secondary');
  return {
    id: c.getAttribute('data-listingid'),
    title: (q('.s-card__title .su-styled-text') || {}).innerText || '',
    cond: [...c.querySelectorAll('.s-card__subtitle')].map(e => e.innerText.trim()).join(' · '),
    prices: [...c.querySelectorAll('.s-card__price')].map(e => e.innerText.trim()),
    rows, seller: sec ? sec.innerText.replace(/\\n/g, ' ') : '',
  };
})"""


def new_context(pw):
    browser = pw.chromium.launch(channel="chromium", args=["--disable-blink-features=AutomationControlled"])
    h = CFG["home"]
    ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1366, "height": 900},
                              geolocation={"latitude": h["lat"], "longitude": h["lon"]}, permissions=["geolocation"])
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    ctx.route(re.compile(r"\.(png|jpe?g|webp|gif|woff2?|mp4)(\?|$)"), lambda r: r.abort())
    return browser, ctx


class Ebay:
    def __init__(self, ctx):
        self.page = ctx.new_page()
        self.page.goto("https://www.ebay.com/", timeout=45000)
        self.page.wait_for_timeout(2500)
        self.total_listings = 0

    def search(self, query):
        # Buy It Now, newly listed first (eBay blocks bots from sold-listing searches)
        params = {"_nkw": query, "_ipg": "120", "LH_BIN": "1", "_sop": "10",
                  "LH_PrefLoc": "1" if CFG["us_only"] else "2"}
        url = "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode(params)
        cards = []
        for attempt in range(2):
            self.page.goto(url, timeout=45000)
            self.page.wait_for_timeout(2500 + random.randint(0, 1500))
            cards = self.page.evaluate(EXTRACT_JS)
            if cards:
                break
            self.page.goto("https://www.ebay.com/", timeout=45000)
            self.page.wait_for_timeout(4000)
        out = []
        for c in cards:
            if not c["id"] or not c["title"] or len(c["prices"]) != 1:  # skip price ranges / multi-variation
                continue
            price = money(c["prices"][0])
            if price is None or re.search(r"parts|not working", c["cond"].lower()):
                continue
            ship = 0.0
            for r in c["rows"]:
                if "delivery" in r.lower() or "shipping" in r.lower():
                    ship = money(r) or 0.0
            fb = re.search(r"([\d.]+)% positive \(([\d.]+)([KM]?)\)", c["seller"])
            out.append({
                "id": c["id"], "title": c["title"], "cond": c["cond"], "total": round(price + ship, 2),
                "url": f"https://www.ebay.com/itm/{c['id']}",
                "seller": re.sub(r"^\w{3}-\d\d \d\d:\d\d\s+", "", c["seller"].strip()),
                "fb_pct": float(fb.group(1)) if fb else None,
                "fb_count": float(fb.group(2)) * {"": 1, "K": 1e3, "M": 1e6}[fb.group(3)] if fb else 0,
                "location": next((r for r in c["rows"] if r.startswith("Located in")), ""),
                "best_offer": any("best offer" in r.lower() for r in c["rows"]),
            })
        self.total_listings += len(out)
        return out


def update_market(prices, model, listings):
    """Rolling pool of clean single-card asking prices per model; returns (median, n, src)."""
    now = time.time()
    pool = {k: v for k, v in prices.get(model["name"], {}).items() if now - v[1] < CFG["market_window_days"] * 86400}
    for it in listings:
        c = classify(it["title"])
        if (c and c["model"] is model and not c["is_pc"] and not c["flags"] and c["count"] == 1
                and "united states" in it["location"].lower()):
            pool[it["id"]] = [it["total"], pool.get(it["id"], [0, now])[1]]
    prices[model["name"]] = pool
    vals = sorted(v[0] for v in pool.values())
    if len(vals) < CFG["min_market_samples"]:
        return model["fallback_median"], len(vals), "fallback"
    k = len(vals) // 10
    return round(statistics.median(vals[k:len(vals) - k] if k else vals)), len(vals), "ebay-asking"


def ebay_candidates(listings, medians, seen, want_model=None):
    hits = []
    for it in listings:
        c = classify(it["title"])
        if not c or (want_model and c["model"] is not want_model):
            continue
        if CFG["us_only"] and it["location"] and "United States" not in it["location"]:
            continue
        seller_ok = it["fb_count"] >= CFG["min_seller_feedback"] and (it["fb_pct"] or 0) >= CFG["min_seller_positive_pct"]
        h = candidate("eBay", f"ebay:{it['id']}:{it['total']}", it["title"], it["url"], it["total"], c, medians, seen,
                      seller_ok, cond=it["cond"], seller=it["seller"], best_offer=it["best_offer"])
        if h:
            hits.append(h)
    return hits


UNAVAILABLE = re.compile(r"out of stock|this listing (has|was) ended|no longer available|listing was ended|bidding has ended")


def verify_ebay(eb, hits, details):
    """Search results keep showing sold-out listings at their old price, so open each hit before alerting.
    Unavailable listings are rechecked every 2 hours in case they come back in stock."""
    ok = []
    for h in hits:
        key = "oos:" + h["url"]
        if time.time() - details.get(key, 0) < 2 * 3600:
            continue
        try:
            eb.page.goto(h["url"], timeout=45000)
            eb.page.wait_for_timeout(2500)
            text = eb.page.inner_text("body")[:20000].lower()
        except Exception as ex:
            log(f"ebay verify failed {h['url']}: {ex}")
            continue
        if UNAVAILABLE.search(text):
            log(f"skipping unavailable listing {h['url']}")
            details[key] = time.time()
            continue
        ok.append(h)
        time.sleep(random.uniform(1, 3))
    return ok


def check_ebay(eb, medians, prices, seen):
    hits = []
    for m in CFG["models"]:
        try:
            listings = eb.search(m["query"])
        except Exception as ex:
            log(f"ebay search failed {m['name']}: {ex}")
            continue
        med, n, src = update_market(prices, m, listings)
        medians[m["name"]] = {"median": med, "n": n, "src": src, "ts": time.time()}
        if not listings:
            log(f"ebay returned 0 listings for {m['name']} (possible block)")
        hits += ebay_candidates(listings, medians, seen, want_model=m)
        time.sleep(random.uniform(2, 5))
    return hits


def check_ebay_pcs(eb, medians, seen):
    hits = []
    for q in CFG["ebay_pc_queries"]:
        try:
            listings = eb.search(q)
        except Exception as ex:
            log(f"ebay pc search failed {q}: {ex}")
            continue
        hits += [h for h in ebay_candidates(listings, medians, seen) if h["cls"]["is_pc"]]
        time.sleep(random.uniform(2, 5))
    return hits


# ---------- Craigslist ----------

def craigslist_body(url, cache):
    if url in cache:
        return cache[url]
    try:
        page = fetch(url).decode("utf-8", "replace")
        m = re.search(r'<section id="postingbody">(.*?)</section>', page, re.S)
        body = html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))) if m else ""
    except Exception as ex:
        log(f"craigslist detail failed {url}: {ex}")
        body = ""
    cache[url] = body[:3000]
    return cache[url]


def check_craigslist(medians, seen, details):
    hits, fetched = [], 0
    cl = CFG["craigslist"]
    h = CFG["home"]
    for q in cl["queries"]:
        params = {"batch": "1-0-360-0-0", "cc": "US", "lang": "en", "searchPath": "sss", "query": q,
                  "lat": h["lat"], "lon": h["lon"], "search_distance": cl["radius_miles"]}
        try:
            d = json.loads(fetch("https://sapi.craigslist.org/web/v8/postings/search/full?" + urllib.parse.urlencode(params)))["data"]
        except Exception as ex:
            log(f"craigslist search failed: {ex}")
            continue
        if not d.get("items") or not isinstance(d.get("decode"), dict):  # decode is 0 when nothing matched
            continue
        base = d["decode"]["minPostingId"]
        for it in d["items"]:
            price, title = it[3], it[-1]
            if not isinstance(title, str) or price is None or price <= 1:
                continue
            parts = dict((x[0], x[1:]) for x in it if isinstance(x, list) and x and isinstance(x[0], int))
            if 13 not in parts or 6 not in parts:
                continue
            url = f"https://www.craigslist.org/view/d/{parts[6][0]}/{parts[13][0]}"
            try:
                _, lat, lon = it[4].split("~")
                dist = miles(float(lat), float(lon))
            except (ValueError, AttributeError):
                dist = None
            if dist is not None and dist > CFG["max_local_miles"]:
                continue
            key = f"cl:{base + it[0]}:{price}"
            if key in seen:
                continue
            c = classify(title)
            if not c and not HARD.search(title.lower()) and fetched < CFG["detail_fetch_cap"]:
                fetched += url not in details
                c = classify(title, craigslist_body(url, details))
            if not c:
                continue
            hh = candidate("Craigslist", key, title, url, float(price), c, medians, seen,
                           location=f"{dist:.0f} mi from {h['label']}" if dist is not None else "")
            if hh:
                hits.append(hh)
        time.sleep(3)
    return hits


# ---------- OfferUp (Playwright; results are local to the machine's location) ----------

def offerup_body(ctx, url, cache):
    """OfferUp 403s plain HTTP on item pages, so load them in the browser."""
    if url in cache:
        return cache[url]
    page = ctx.new_page()
    try:
        page.goto(url, timeout=45000)
        page.wait_for_timeout(2500)
        m = re.search(r'"description":"((?:[^"\\]|\\.)*)"', page.content())
        body = json.loads(f'"{m.group(1)}"') if m else ""
    except Exception as ex:
        log(f"offerup detail failed {url}: {ex}")
        body = ""
    finally:
        page.close()
    cache[url] = body[:3000]
    return cache[url]


def check_offerup(ctx, medians, seen, details):
    hits, fetched = [], 0
    page = ctx.new_page()
    for q in CFG["offerup"]["queries"]:
        try:
            page.goto("https://offerup.com/search?" + urllib.parse.urlencode({"q": q}), timeout=45000)
            page.wait_for_timeout(4000)
            for _ in range(3):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1200)
            items = page.eval_on_selector_all(
                "a[href*='/item/detail/']", "els => els.map(e => [e.href, e.innerText.split('\\n').map(s => s.trim()).filter(Boolean)])")
        except Exception as ex:
            log(f"offerup search failed {q}: {ex}")
            continue
        if not items:
            log(f"offerup returned 0 items for {q}")
        for href, lines in items:
            if len(lines) < 2:
                continue
            price_line = next((l for l in lines if l.startswith("$")), None)
            price = money(price_line)
            if not price or price <= 1:
                continue
            title = next(l for l in lines if l != price_line)
            loc = lines[-1] if lines[-1] not in (title, price_line) else ""
            url = href.split("?")[0]
            key = f"ou:{url.rsplit('/', 1)[-1]}:{price}"
            if key in seen:
                continue
            c = classify(title)
            if (not c and not HARD.search(title.lower()) and (PC_PHRASE.search(title.lower()) or PC_SPEC.search(title.lower()))
                    and fetched < CFG["offerup"]["detail_cap"]):
                fetched += url not in details
                c = classify(title, offerup_body(ctx, url, details))
            if not c:
                continue
            hh = candidate("OfferUp", key, title, url, price, c, medians, seen, location=loc)
            if hh:
                hits.append(hh)
        time.sleep(random.uniform(2, 4))
    page.close()
    return hits


# ---------- RSS (Reddit, Slickdeals, etc.) ----------

def rss_items(raw):
    root = ET.fromstring(raw)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for e in root.findall(".//a:entry", ns):  # Atom (Reddit)
        link = e.find("a:link", ns)
        yield (e.findtext("a:id", "", ns), e.findtext("a:title", "", ns),
               link.get("href") if link is not None else "", e.findtext("a:content", "", ns))
    for e in root.iter("item"):  # RSS 2.0
        yield (e.findtext("guid") or e.findtext("link", ""), e.findtext("title", ""), e.findtext("link", ""), e.findtext("description", ""))


def prices_near(text, model):
    """Prices mentioned within ~120 chars after a mention of the model."""
    out = []
    for mm in re.finditer(model["regex"], text):
        window = text[mm.start(): mm.start() + 120]
        out += [p for p in (money(x) for x in re.findall(r"\$\s?[\d,]{3,6}(?:\.\d\d)?", window)) if p]
    return out


def check_rss(medians, seen):
    hits = []
    for feed in CFG["rss_feeds"]:
        try:
            raw = fetch(feed["url"])
        except Exception as ex:
            log(f"rss failed {feed['name']}: {ex}")
            time.sleep(15)
            continue
        for gid, title, link, body in rss_items(raw):
            tl = html.unescape(title).lower()
            if feed["used"]:  # r/hardwareswap style: only look at what they HAVE
                if "[h]" not in tl:
                    continue
                tl = tl.split("[h]", 1)[1].split("[w]", 1)[0]
            text = html.unescape(re.sub(r"<[^>]+>", " ", body or "")).lower()
            c = classify(tl)
            if not c:
                continue
            med = median_for(medians, c["model"])
            if med is None:
                continue
            floor, ceil = CFG["longshot_floor"] * med, (CFG["pc_ratio"] if c["is_pc"] else CFG["deal_ratio"]) * med * c["count"]
            cands = [p for p in (prices_near(tl + " " + text, c["model"]) or [money(title)]) if p and floor <= p <= ceil]
            if not cands:
                continue
            h = candidate(feed["name"], f"rss:{gid}", html.unescape(title), link, min(cands), c, medians, seen,
                          note="price parsed from post text — verify")
            if h:
                hits.append(h)
        time.sleep(15)  # Reddit rate-limits unauthenticated RSS hard
    return hits


# ---------- alerts ----------

def alert(h):
    c, m = h["cls"], h["cls"]["model"]
    pct = round((1 - h["ratio"]) * 100)
    gpu = f"{c['count']}x {m['name']}" if c["count"] > 1 else m["name"]
    value = h["median"] * c["count"]
    if h["tier"] == "PC DEAL":
        head = f"🖥️🚨 PC DEAL: whole computer with {gpu} ({m['vram'] * c['count']}GB VRAM)"
        sub = f"${h['total']:,.0f} for the PC vs ~${value:,.0f} for the GPU alone on eBay"
    elif h["tier"] == "DEAL":
        head = f"{'🚨🚨' if h['ratio'] <= 0.5 else '🚨'} GPU DEAL: {gpu} ({m['vram'] * c['count']}GB) — {pct}% below market"
        sub = f"${h['total']:,.0f} all-in vs ~${value:,.0f} typical eBay price"
    else:
        what = f"PC with {gpu}" if c["is_pc"] else gpu
        head = f"🎲 LONG SHOT: {what} ({m['vram'] * c['count']}GB)"
        sub = f"${h['total']:,.0f} vs ~${value:,.0f} typical for the GPU{'s' if c['count'] > 1 else ''}"
    lines = [head, sub, h["title"][:200], f"Source: {h['source']}" + (f" · {h['location']}" if h.get("location") else "")]
    if h.get("cond"):
        lines.append(f"Condition: {h['cond']}" + (" · Best Offer" if h.get("best_offer") else ""))
    if h.get("seller"):
        lines.append(f"Seller: {h['seller']}")
    if h["reasons"]:
        lines.append("Why it's a long shot: " + "; ".join(h["reasons"]))
    if h.get("note"):
        lines.append(f"({h['note']})")
    if c["is_pc"]:
        lines.append("Confirm the GPU in photos/specs; sellers sometimes list what the PC 'supports'.")
    lines += [h["url"], "Check photos, seller history & returns before buying. Nothing was purchased."]
    telegram("\n".join(lines), quiet=h["tier"] == "LONG SHOT" and CFG["longshot_quiet"])


# ---------- main ----------

def main():
    if "--test-telegram" in sys.argv:
        telegram("✅ GPU deal watch test message — Telegram alerts are working.")
        return
    seen = set(load("seen.json", []))
    medians = load("medians.json", {})
    prices = load("prices.json", {})
    details = load("details.json", {})
    health = load("health.json", {"ebay_fail_streak": 0})
    hits = []
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, ctx = new_context(pw)
        try:
            if enabled("ebay") or enabled("pc"):
                ok = False
                try:
                    eb = Ebay(ctx)
                    found = []
                    if enabled("ebay"):
                        found += check_ebay(eb, medians, prices, seen)
                    if enabled("pc"):
                        found += check_ebay_pcs(eb, medians, seen)
                    ok = eb.total_listings > 0
                    hits += verify_ebay(eb, list({h["key"]: h for h in found}.values()), details)
                except Exception as ex:
                    log(f"ebay session error: {ex}")
                save("prices.json", prices)
                save("medians.json", medians)
                health["ebay_fail_streak"] = 0 if ok else health["ebay_fail_streak"] + 1
                if health["ebay_fail_streak"] == 3:
                    try:
                        telegram("⚠️ GPU deal watch: eBay has returned nothing for 3 runs in a row (likely blocked). "
                                 "Check state/runs.log on herdr-lab.")
                    except Exception as ex:
                        log(f"telegram failed: {ex}")
                save("health.json", health)
            if enabled("offerup"):
                try:
                    hits += check_offerup(ctx, medians, seen, details)
                except Exception as ex:
                    log(f"offerup error: {ex}")
        finally:
            browser.close()
    if enabled("craigslist"):
        hits += check_craigslist(medians, seen, details)
    if enabled("rss"):
        hits += check_rss(medians, seen)

    # trim the detail cache so it doesn't grow forever
    save("details.json", dict(list(details.items())[-2000:]))

    order = {"PC DEAL": 0, "DEAL": 0, "LONG SHOT": 1}
    unique = {h["key"]: h for h in hits}.values()
    csv_path = os.path.join(STATE, "deals-dryrun.csv" if DRY else "deals.csv")
    new_file = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time", "tier", "source", "model", "count", "is_pc", "total", "median", "pct_below", "title", "url"])
        for h in sorted(unique, key=lambda h: (order[h["tier"]], h["ratio"])):
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), h["tier"], h["source"],
                        h["cls"]["model"]["name"], h["cls"]["count"], h["cls"]["is_pc"], h["total"], h["median"],
                        round((1 - h["ratio"]) * 100), h["title"], h["url"]])
            try:
                alert(h)
                seen.add(h["key"])
            except Exception as ex:
                log(f"telegram failed: {ex}")
    if not DRY:
        save("seen.json", sorted(seen))
    counts = {t: sum(1 for h in unique if h["tier"] == t) for t in order}
    log(f"run ok: {counts}")


if __name__ == "__main__":
    main()
