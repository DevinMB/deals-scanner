"""FastAPI app: web UI (server-rendered, HTMX for live bits) and /healthz."""

import base64
import html
import json
import logging
import os
import re
import secrets
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, gpu_import, preview, recipients, settings as settings_mod, telegram
from .models import Watch
from .runner import SOURCE_ORDER, iso, market
from .scheduler import Scheduler
from .scoring import DEFAULTS, Rules, classify, effective_typical
from .sources import registry

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
SOURCE_LABELS = {n: s.label for n, s in registry().items()}
STOPWORDS = set("a an and the for with of in on to new used like great condition excellent good works working "
                "size sale only w obo free shipping local pickup".split())


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    app.state.cfg = cfg
    app.state.db = conn
    app.state.notifier = telegram.from_config(cfg, lambda: db.connect(cfg.db_path))
    app.state.scheduler = None
    if os.environ.get("SCHEDULER", "on") != "off":
        app.state.scheduler = Scheduler(cfg, app.state.notifier)
        app.state.scheduler.start()
    yield
    if app.state.scheduler:
        app.state.scheduler.shutdown()
    conn.close()


app = FastAPI(title="Deal Watch", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.middleware("http")
async def shared_password(request: Request, call_next):
    """Optional HTTP Basic auth with UI_PASSWORD (any username). /healthz stays open for the healthcheck."""
    pw = os.environ.get("UI_PASSWORD")
    if pw and request.url.path != "/healthz":
        try:
            _, _, given = base64.b64decode(request.headers.get("authorization", "")[6:]).decode().partition(":")
        except Exception:
            given = ""
        if not secrets.compare_digest(given.encode(), pw.encode()):
            return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="Deal Watch"'})
    return await call_next(request)


# ---------- template helpers ----------

def _parse(ts: str | None) -> datetime | None:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if ts else None


def ago(ts: str | None) -> str:
    t = _parse(ts)
    if not t:
        return "never"
    s = (datetime.now(timezone.utc) - t).total_seconds()
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s // 60:.0f} min ago"
    if s < 86400 * 2:
        return f"{s // 3600:.0f} h ago"
    return f"{s // 86400:.0f} days ago"


def money(v) -> str:
    return "—" if v is None else f"${v:,.0f}"


def pct_below(ratio) -> str:
    return "" if ratio is None else f"{round((1 - ratio) * 100)}%"


templates.env.filters.update(ago=ago, money=money, pct_below=pct_below, fromjson=json.loads)
templates.env.globals.update(SOURCE_LABELS=SOURCE_LABELS)


def page(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, ctx)


def conn(request: Request):
    return request.app.state.db


def rules_for(request: Request) -> tuple[dict, Rules]:
    s = settings_mod.load(conn(request))
    return s, Rules.from_settings(s)


def get_watch(request: Request, wid: int) -> Watch:
    row = conn(request).execute("SELECT * FROM watches WHERE id = ?", (wid,)).fetchone()
    if not row:
        raise HTTPException(404, "No such watch")
    return Watch.from_row(row)


def health_badges(s: dict) -> list[dict]:
    out = []
    for n in SOURCE_ORDER:
        h = s.get("health", {}).get(n)
        if not h:
            continue
        state = "failing" if h["streak"] >= 3 else "flaky" if h["streak"] else "ok"
        out.append({"name": n, "label": SOURCE_LABELS.get(n, n), "state": state, **h})
    return out


# ---------- health ----------

@app.get("/healthz")
def healthz(request: Request):
    try:
        conn(request).execute("SELECT 1").fetchone()
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=503)
    return {"status": "ok"}


# ---------- dashboard ----------

@app.get("/")
def dashboard(request: Request):
    c = conn(request)
    s, rules = rules_for(request)
    day_ago = iso(datetime.now(timezone.utc) - timedelta(hours=24))
    cards = []
    for row in c.execute("SELECT * FROM watches ORDER BY enabled DESC, name COLLATE NOCASE"):
        w = Watch.from_row(row)
        med, n, newest = market(c, w.id, rules)
        last = c.execute("SELECT MAX(finished_at) FROM runs WHERE watch_id = ? AND error IS NULL AND listings_found > 0",
                         (w.id,)).fetchone()[0]
        new24 = c.execute("SELECT COUNT(*) FROM listings WHERE watch_id = ? AND first_seen >= ?",
                          (w.id, day_ago)).fetchone()[0]
        # Same rule as the watch page: re-check against the current filters, and drop what Ollama called a part
        deals = sum(1 for t, b in c.execute("SELECT title, body FROM listings WHERE watch_id = ? AND tier IS NOT NULL "
                                            "AND last_seen >= ? AND available = 1 AND COALESCE(llm_verdict, 'item') = 'item'",
                                            (w.id, day_ago)) if classify(t, w, rules, b or ""))
        problems = [f"{SOURCE_LABELS.get(r[0], r[0])}: {r[1][:80]}" for r in c.execute(
            "SELECT source, error FROM runs WHERE id IN (SELECT MAX(id) FROM runs WHERE watch_id = ? GROUP BY source) "
            "AND error IS NOT NULL", (w.id,)) if w.enabled and r[0] in w.sources]
        alert = c.execute("SELECT a.tier, a.price_total, a.sent_at, l.title FROM alerts a JOIN listings l ON l.id = a.listing_id"
                          " WHERE a.watch_id = ? AND a.telegram_ok = 1 ORDER BY a.sent_at DESC LIMIT 1", (w.id,)).fetchone()
        people = recipients.for_watch(c, w.id)
        cards.append({"w": w, "problems": problems, "people": [r["name"] for r in people],
                      "only_owner": len(people) == 1 and people[0]["is_owner"], "learned": med, "samples": n, "newest": newest, "typical": effective_typical(med, w),
                      "override_used": w.override_enabled and w.typical_override is not None,
                      "last_scan": last, "new24": new24, "deals": deals, "alert": alert})
    # Live deals first, then recent alerts, then the rest; paused watches last
    cards.sort(key=lambda cd: (not cd["w"].enabled, -cd["deals"], -(_parse(cd["alert"]["sent_at"]).timestamp()
               if cd["alert"] else 0), cd["w"].name.lower()))
    return page(request, "dashboard.html", cards=cards, badges=health_badges(s), last_run=s.get("last_run"),
                scanning=_scanning(request), imported=request.query_params.get("imported"))


@app.post("/import/gpu")
def import_gpu(request: Request):
    created, updated = gpu_import.run(conn(request))
    return RedirectResponse(f"/?imported={created}+new,+{updated}+updated", 303)


def _scanning(request: Request) -> bool:
    sch = request.app.state.scheduler
    return bool(sch and sch.busy)


@app.post("/scan", response_class=HTMLResponse)
def scan_now(request: Request):
    sch = request.app.state.scheduler
    if not sch:
        return '<span class="err">The scheduler is off (SCHEDULER=off).</span>'
    sch.scan_now()
    return scan_status(request)


@app.get("/scan/status", response_class=HTMLResponse)
def scan_status(request: Request):
    s = settings_mod.load(conn(request))
    return templates.get_template("_scan_status.html").render(scanning=_scanning(request), last_run=s.get("last_run"))


@app.post("/watches/{wid}/toggle")
def toggle_watch(request: Request, wid: int):
    conn(request).execute("UPDATE watches SET enabled = 1 - enabled WHERE id = ?", (wid,))
    return RedirectResponse(request.headers.get("referer") or "/", 303)


# ---------- watch form ----------

FORM_DEFAULTS = Watch(id=None, name="", search_queries=[])


def watch_from_form(form) -> tuple[dict, list[str]]:
    """Form fields -> DB columns. Ratios are shown as '% below typical' and stored as price/typical."""
    errors = []

    def lines(key):
        return [x.strip() for x in form.get(key, "").splitlines() if x.strip()]

    def num(key, cast=float, lo=None, hi=None):
        v = form.get(key, "").strip().replace("$", "").replace(",", "")
        if not v:
            return None
        try:
            x = cast(v)
        except ValueError:
            errors.append(f"{key.replace('_', ' ')}: not a number")
            return None
        if (lo is not None and x < lo) or (hi is not None and x > hi):
            errors.append(f"{key.replace('_', ' ')}: must be between {lo} and {hi}")
        return x

    def below(key, default):
        p = num(key, float, 0, 100)
        return round(1 - (p if p is not None else default) / 100, 4)

    d = {
        "name": form.get("name", "").strip(),
        "search_queries": lines("search_queries"),
        "include_regex": form.get("include_regex", "").strip() or None,
        "exclude_terms": lines("exclude_terms"),
        "sources": [n for n in SOURCE_ORDER if form.get(f"src_{n}")],
        "enabled": int(bool(form.get("enabled"))),
        "deal_ratio": below("deal_pct", 40),
        "local_deal_ratio": below("local_deal_pct", 50),
        "scam_ratio": below("scam_pct", 70),
        "max_price": num("max_price", float, 0),
        "typical_override": num("typical_override", float, 0),
        "override_enabled": int(bool(form.get("override_enabled"))),
        "longshot_enabled": int(bool(form.get("longshot_enabled"))),
        "quiet_longshots": int(bool(form.get("quiet_longshots"))),
        "radius_miles": num("radius_miles", int, 1, 500),
        "bundle_enabled": int(bool(form.get("bundle_enabled"))),
        "bundle_regex": form.get("bundle_regex", "").strip() or None,
        "bundle_ratio": below("bundle_pct", 15),
    }
    if not d["name"]:
        errors.append("Give the watch a name.")
    if not d["search_queries"]:
        errors.append("Add at least one search term.")
    if not d["sources"]:
        errors.append("Pick at least one source.")
    if d["scam_ratio"] >= d["deal_ratio"]:
        errors.append("The scam-risk level must be further below typical than the deal level.")
    for label, pats in (("include pattern", [d["include_regex"]] if d["include_regex"] else []),
                        ("bundle pattern", [d["bundle_regex"]] if d["bundle_regex"] else []),
                        ("exclude term", d["exclude_terms"])):
        for p in pats:
            try:
                re.compile(p)
            except re.error as ex:
                errors.append(f"{label} “{p}” is not a valid pattern: {ex}")
    return d, errors


def _as_watch(d: dict, wid=None) -> Watch:
    return Watch(id=wid, **{k: (bool(v) if k in ("enabled", "override_enabled", "longshot_enabled", "quiet_longshots",
                                                  "bundle_enabled") else v) for k, v in d.items()})


def form_page(request: Request, w: Watch, errors: list, is_new: bool, chosen: set[int]):
    return page(request, "watch_form.html", w=w, errors=errors, is_new=is_new, people=recipients.all(conn(request)),
                chosen=chosen, ollama=bool(settings_mod.load(conn(request)).get("ollama_url")))


def chosen_recipients(form) -> set[int]:
    return {int(k[5:]) for k in form if k.startswith("rcpt_") and k[5:].isdigit()}


@app.get("/watches/new")
def new_watch(request: Request):
    return form_page(request, FORM_DEFAULTS, [], True, {recipients.owner_id(conn(request))})


@app.get("/watches/{wid}/edit")
def edit_watch(request: Request, wid: int):
    chosen = {r["id"] for r in recipients.for_watch(conn(request), wid)}
    return form_page(request, get_watch(request, wid), [], False, chosen)


@app.post("/watches")
async def create_watch(request: Request):
    form = await request.form()
    d, errors = watch_from_form(form)
    if errors:
        return form_page(request, _as_watch(d), errors, True, chosen_recipients(form))
    cols = list(d)
    vals = [json.dumps(v) if isinstance(v, list) else v for v in d.values()]
    wid = conn(request).execute(f"INSERT INTO watches ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                vals).lastrowid
    recipients.set_for_watch(conn(request), wid, sorted(chosen_recipients(form)))
    return RedirectResponse(f"/watches/{wid}", 303)


@app.post("/watches/{wid}")
async def update_watch(request: Request, wid: int):
    get_watch(request, wid)
    form = await request.form()
    d, errors = watch_from_form(form)
    if errors:
        return form_page(request, _as_watch(d, wid), errors, False, chosen_recipients(form))
    conn(request).execute(f"UPDATE watches SET {', '.join(f'{k} = ?' for k in d)} WHERE id = ?",
                          [json.dumps(v) if isinstance(v, list) else v for v in d.values()] + [wid])
    recipients.set_for_watch(conn(request), wid, sorted(chosen_recipients(form)))
    return RedirectResponse(f"/watches/{wid}", 303)


@app.post("/watches/{wid}/delete")
def delete_watch(request: Request, wid: int):
    c = conn(request)
    c.execute("DELETE FROM alerts WHERE watch_id = ?", (wid,))
    c.execute("DELETE FROM watches WHERE id = ?", (wid,))
    return RedirectResponse("/", 303)


# ---------- suggest ----------

@app.post("/suggest", response_class=HTMLResponse)
async def suggest(request: Request):
    from . import llm
    item = (await request.form()).get("name", "").strip()
    if not item:
        return '<p class="err">Type what you want to buy in Name first, e.g. “Fujifilm X100V”.</p>'
    s = settings_mod.load(conn(request))
    out, err = llm.suggest(s.get("ollama_url"), s["ollama_model"], item)
    return templates.get_template("_suggest.html").render(s=out, err=err, item=item, model=s["ollama_model"])


# ---------- preview ----------

@app.post("/preview", response_class=HTMLResponse)
async def start_preview(request: Request):
    d, errors = watch_from_form(await request.form())
    errors = [e for e in errors if not e.startswith("Give the watch")]  # a name isn't needed to preview
    if errors:
        return templates.get_template("_preview.html").render(job=None, errors=errors)
    d["name"] = d["name"] or d["search_queries"][0]
    job = preview.start(request.app.state.cfg, _as_watch(d))
    return templates.get_template("_preview.html").render(job=job, errors=[])


@app.get("/preview/{job_id}", response_class=HTMLResponse)
def preview_status(job_id: str):
    job = preview.JOBS.get(job_id)
    if not job:
        return '<p class="err">This preview has expired. Run it again.</p>'
    return templates.get_template("_preview.html").render(job=job, errors=[])


# ---------- watch detail ----------

LISTINGS_SHOWN = 40


@app.get("/watches/{wid}")
def watch_detail(request: Request, wid: int, all: int = 0):
    c = conn(request)
    w = get_watch(request, wid)
    s, rules = rules_for(request)
    med, n, newest = market(c, wid, rules)
    typical = effective_typical(med, w)
    since = iso(datetime.now(timezone.utc) - timedelta(hours=48))
    rows = c.execute("SELECT * FROM listings WHERE watch_id = ? AND last_seen >= ? AND available = 1", (wid, since)).fetchall()
    # Re-check against the current filters, so an exclude added a moment ago takes effect immediately
    listings = []
    for r in rows:
        m = classify(r["title"], w, rules, r["body"] or "")
        if not m:
            continue
        ratio = r["price_total"] / (typical * m.count) if typical and r["price_total"] else None
        listings.append({**dict(r), "count": m.count, "live_ratio": ratio, "reasons": json.loads(r["reasons"])})
    listings.sort(key=lambda l: (l["tier"] is None, l["live_ratio"] is None, l["live_ratio"] or 0))
    total = len(listings)
    if not all:
        listings = listings[:LISTINGS_SHOWN]
    history = [dict(r) for r in c.execute("SELECT day, median, samples FROM market_history WHERE watch_id = ? "
                                          "ORDER BY day DESC LIMIT 120", (wid,))][::-1]
    alerts = c.execute("SELECT a.*, l.title, l.url, l.source FROM alerts a JOIN listings l ON l.id = a.listing_id "
                       "WHERE a.watch_id = ? ORDER BY a.id DESC LIMIT 50", (wid,)).fetchall()
    runs = c.execute("SELECT source, finished_at, listings_found, error FROM runs WHERE id IN "
                     "(SELECT MAX(id) FROM runs WHERE watch_id = ? GROUP BY source)", (wid,)).fetchall()
    return page(request, "watch_detail.html", w=w, people=recipients.for_watch(c, wid), learned=med, samples=n, newest=newest, typical=typical,
                override_used=w.override_enabled and w.typical_override is not None, listings=listings, total=total,
                history=history, chart=_chart(history), alerts=alerts, runs={r["source"]: r for r in runs})


def _chart(history: list[dict]) -> dict | None:
    """Server-side geometry for the median line; hover handled by static/chart.js."""
    if len(history) < 2:
        return None
    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 640, 180, 56, 12, 12, 28
    vals = [h["median"] for h in history]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or max(hi * 0.1, 1)
    lo, hi = lo - span * 0.15, hi + span * 0.15
    xs = [PAD_L + i * (W - PAD_L - PAD_R) / (len(history) - 1) for i in range(len(history))]
    ys = [PAD_T + (hi - v) * (H - PAD_T - PAD_B) / (hi - lo) for v in vals]
    ticks = [lo + (hi - lo) * f for f in (0.15, 0.5, 0.85)]
    return {"w": W, "h": H, "pad_l": PAD_L, "pad_r": PAD_R, "base": H - PAD_B,
            "points": " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys)),
            "ticks": [(PAD_T + (hi - t) * (H - PAD_T - PAD_B) / (hi - lo), money(t)) for t in ticks],
            "data": json.dumps([{"x": round(x, 1), "y": round(y, 1), "day": h["day"], "median": money(h["median"]),
                                 "samples": h["samples"]} for x, y, h in zip(xs, ys, history)]),
            "first": history[0]["day"], "last": history[-1]["day"]}


@app.get("/watches/{wid}/not-relevant/{lid}", response_class=HTMLResponse)
def not_relevant(request: Request, wid: int, lid: int):
    """Offer this listing's words, most distinctive first, to add to the watch's excludes."""
    c = conn(request)
    w = get_watch(request, wid)
    row = c.execute("SELECT title FROM listings WHERE id = ? AND watch_id = ?", (lid, wid)).fetchone()
    if not row:
        return ""
    others = Counter()
    for (t,) in c.execute("SELECT title FROM listings WHERE watch_id = ? AND id != ?", (wid, lid)):
        others.update(set(_words(t)))
    known = set(_words(w.name + " " + " ".join(w.search_queries))) | STOPWORDS
    words = [x for x in dict.fromkeys(_words(row["title"])) if x not in known and len(x) > 1]
    words.sort(key=lambda x: others[x])
    return templates.get_template("_not_relevant.html").render(w=w, lid=lid, words=words[:8])


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9+\-]*", text.lower())


@app.post("/watches/{wid}/exclude")
async def add_exclude(request: Request, wid: int):
    w = get_watch(request, wid)
    word = (await request.form()).get("word", "").strip().lower()
    if word:
        pattern = rf"\b{re.escape(word)}\b"
        if pattern not in w.exclude_terms:
            conn(request).execute("UPDATE watches SET exclude_terms = ? WHERE id = ?",
                                  (json.dumps(w.exclude_terms + [pattern]), wid))
    return RedirectResponse(f"/watches/{wid}", 303)


# ---------- alerts ----------

@app.get("/alerts")
def alerts_page(request: Request, watch: int | None = None, tier: str | None = None):
    c = conn(request)
    q = ("SELECT a.*, l.title, l.url, l.source, l.location, w.name AS watch_name FROM alerts a "
         "JOIN listings l ON l.id = a.listing_id JOIN watches w ON w.id = a.watch_id WHERE 1 = 1")
    args = []
    if watch:
        q += " AND a.watch_id = ?"
        args.append(watch)
    if tier:
        q += " AND a.tier = ?"
        args.append(tier)
    rows = c.execute(q + " ORDER BY a.id DESC LIMIT 300", args).fetchall()
    watches = c.execute("SELECT id, name FROM watches ORDER BY name COLLATE NOCASE").fetchall()
    return page(request, "alerts.html", alerts=rows, watches=watches, sel_watch=watch, sel_tier=tier,
                tiers=["DEAL", "BUNDLE DEAL", "LONG SHOT"])


# ---------- settings ----------

def _mask(v: str | None) -> str:
    return "not set" if not v else f"set (ends …{v[-3:]})" if len(v) > 10 else "set"


@app.get("/settings")
def settings_page(request: Request, saved: int = 0, people_error: int = 0):
    s, _ = rules_for(request)
    cfg = request.app.state.cfg
    return page(request, "settings.html", s=s, errors=[], saved=saved, badges=health_badges(s), people_error=people_error,
                people=recipients.all(conn(request)), watch_count=conn(request).execute("SELECT COUNT(*) FROM watches").fetchone()[0],
                gpu_missing=_gpu_missing(request),
                token=_mask(cfg.telegram_bot_token), chat=_mask(cfg.telegram_chat_id), dry_run=cfg.dry_run,
                base_url=cfg.base_url, feeds_text=_feeds_text(s["rss_feeds"]))


def _gpu_missing(request: Request) -> list[str]:
    names = {r[0].lower() for r in conn(request).execute("SELECT name FROM watches")}
    return [m["name"] for m in gpu_import.GPU["models"] if m["name"].lower() not in names]


def _feeds_text(feeds: list[dict]) -> str:
    return "\n".join(f"{f['name']} | {f['url']}" + (" | swap" if f.get("used") else "") for f in feeds)


@app.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    c = conn(request)
    errors, new = [], {}

    def num(key, cast=float, lo=None, hi=None):
        try:
            x = cast(form.get(key, "").strip())
        except ValueError:
            errors.append(f"{key.replace('_', ' ')}: not a number")
            return None
        if (lo is not None and x < lo) or (hi is not None and x > hi):
            errors.append(f"{key.replace('_', ' ')}: must be between {lo} and {hi}")
        return x

    lat, lon = num("home_lat", float, -90, 90), num("home_lon", float, -180, 180)
    new["home"] = {"label": form.get("home_label", "").strip() or "Home", "lat": lat, "lon": lon}
    new["radius_miles"] = num("radius_miles", int, 1, 500)
    new["scan_interval_min"] = num("scan_interval_min", int, 10, 1440)
    new["min_seller_feedback"] = num("min_seller_feedback", int, 0)
    new["min_seller_positive_pct"] = num("min_seller_positive_pct", float, 0, 100)
    new["telegram_enabled"] = bool(form.get("telegram_enabled"))
    new["llm_check_enabled"] = bool(form.get("llm_check_enabled"))
    new["ollama_url"] = form.get("ollama_url", "").strip().rstrip("/")
    new["ollama_model"] = form.get("ollama_model", "").strip() or DEFAULTS["ollama_model"]
    if new["ollama_url"] and not new["ollama_url"].startswith("http"):
        errors.append("Ollama URL should look like http://192.168.1.197:11434")
    for key in ("hard_blacklist", "soft_flags"):
        pats = [x.strip() for x in form.get(key, "").splitlines() if x.strip()]
        for p in pats:
            try:
                re.compile(p)
            except re.error as ex:
                errors.append(f"“{p}” is not a valid pattern: {ex}")
        new[key] = pats
    feeds = []
    for line in form.get("rss_feeds", "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if not parts[0]:
            continue
        if len(parts) < 2 or not parts[1].startswith("http"):
            errors.append(f"Feed line needs “name | url”: {line.strip()}")
            continue
        feeds.append({"name": parts[0], "url": parts[1], "used": len(parts) > 2 and parts[2].lower() == "swap"})
    new["rss_feeds"] = feeds
    if errors:
        s = {**settings_mod.load(c), **{k: v for k, v in new.items() if v is not None}}
        cfg = request.app.state.cfg
        return page(request, "settings.html", s=s, errors=errors, saved=0, badges=health_badges(s),
                    people=recipients.all(c), watch_count=c.execute("SELECT COUNT(*) FROM watches").fetchone()[0],
                    gpu_missing=_gpu_missing(request),
                    token=_mask(cfg.telegram_bot_token), chat=_mask(cfg.telegram_chat_id), dry_run=cfg.dry_run,
                    base_url=cfg.base_url, feeds_text=form.get("rss_feeds", ""))
    old_interval = settings_mod.load(c)["scan_interval_min"]
    for k, v in new.items():
        settings_mod.put(c, k, v)
    sch = request.app.state.scheduler
    if sch and new["scan_interval_min"] != old_interval:
        sch.reschedule(new["scan_interval_min"])
    return RedirectResponse("/settings?saved=1", 303)


@app.post("/settings/reset/{key}")
def reset_setting(request: Request, key: str):
    if key in ("hard_blacklist", "soft_flags", "rss_feeds"):
        conn(request).execute("DELETE FROM settings WHERE key = ?", (key,))
    return RedirectResponse("/settings?saved=1", 303)


# ---------- recipients ----------

@app.post("/recipients")
async def add_recipient(request: Request):
    form = await request.form()
    name, chat = form.get("name", "").strip(), form.get("chat_id", "").strip()
    if not name or not re.fullmatch(r"-?\d{3,20}", chat):
        return RedirectResponse("/settings?people_error=1#people", 303)
    c = conn(request)
    rid = c.execute("INSERT INTO recipients (name, chat_id) VALUES (?, ?)", (name, chat)).lastrowid
    if form.get("all_watches"):
        c.execute("INSERT OR IGNORE INTO watch_recipients (watch_id, recipient_id) SELECT id, ? FROM watches", (rid,))
    return RedirectResponse("/settings?saved=1#people", 303)


@app.post("/recipients/{rid}/rename")
async def rename_recipient(request: Request, rid: int):
    name = (await request.form()).get("name", "").strip()
    if name:
        conn(request).execute("UPDATE recipients SET name = ? WHERE id = ?", (name, rid))
    return RedirectResponse("/settings?saved=1#people", 303)


@app.post("/recipients/{rid}/delete")
def delete_recipient(request: Request, rid: int):
    conn(request).execute("DELETE FROM recipients WHERE id = ? AND is_owner = 0", (rid,))
    return RedirectResponse("/settings?saved=1#people", 303)


@app.post("/recipients/{rid}/test", response_class=HTMLResponse)
def test_recipient(request: Request, rid: int):
    r = conn(request).execute("SELECT * FROM recipients WHERE id = ?", (rid,)).fetchone()
    if not r:
        return ""
    n = request.app.state.notifier
    ok, reason = n.test(n.chat_for(r))
    if ok:
        return '<span class="ok">Sent.</span>'
    hint = " They need to open your bot and press Start first." if "chat not found" in reason or "blocked" in reason else ""
    return f'<span class="err">Not sent: {html.escape(reason)}.{hint}</span>'


@app.get("/telegram/recent", response_class=HTMLResponse)
def recent_chats(request: Request):
    chats, err = request.app.state.notifier.recent_chats()
    known = {r["chat_id"] for r in recipients.all(conn(request))} | {request.app.state.cfg.telegram_chat_id}
    return templates.get_template("_recent_chats.html").render(
        chats=[c for c in chats if c["chat_id"] not in known], err=err, any=bool(chats))


@app.post("/ollama/test", response_class=HTMLResponse)
async def ollama_test(request: Request):
    from . import llm
    form = await request.form()
    url, model = form.get("ollama_url", "").strip(), form.get("ollama_model", "").strip() or DEFAULTS["ollama_model"]
    if not url:
        return '<span class="err">Enter the Ollama URL first.</span>'
    item = llm.check(url, model, "Herman Miller Aeron", "Herman Miller Aeron Chair Size B Fully Loaded", timeout=90)
    part = llm.check(url, model, "Herman Miller Aeron", "OEM Herman Miller Aeron Seat Pan Frame Size B", timeout=90)
    if item is None or part is None:
        return f'<span class="err">Couldn\'t get an answer from {html.escape(model)} at {html.escape(url)}. See the logs.</span>'
    good = item[0] == "item" and part[0] != "item"
    return (f'<span class="{"ok" if good else "err"}">{"Working" if good else "Answered, but got it wrong"}: '
            f'chair → {item[0]}, seat frame → {part[0]}.</span>')


@app.post("/telegram/test", response_class=HTMLResponse)
def telegram_test(request: Request):
    ok, reason = request.app.state.notifier.test()
    if ok:
        return '<span class="ok">Sent. Check Telegram.</span>'
    return f'<span class="err">Not sent: {html.escape(reason)}</span>'
