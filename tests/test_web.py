"""Web UI: every page renders, forms validate and save, not-relevant flow, settings, password, preview."""

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from dealwatch import preview
from dealwatch.sources import Listing, Source

FORM = {"name": "Herman Miller Aeron", "search_queries": "herman miller aeron", "exclude_terms": "size a",
        "src_ebay": "on", "src_craigslist": "on", "deal_pct": "40", "local_deal_pct": "50", "scam_pct": "70",
        "bundle_pct": "15", "enabled": "on", "override_enabled": "on", "longshot_enabled": "on", "quiet_longshots": "on"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULER", "off")
    monkeypatch.delenv("UI_PASSWORD", raising=False)
    from dealwatch.app import app
    with TestClient(app) as c:
        yield c


def create(client, **kw):
    r = client.post("/watches", data={**FORM, **kw}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_empty_pages_render(client):
    for path in ("/", "/watches/new", "/alerts", "/settings"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "Create your first watch" in client.get("/").text


def test_create_edit_and_dashboard(client):
    wid = create(client)
    db = client.app.state.db
    row = db.execute("SELECT * FROM watches WHERE id = ?", (wid,)).fetchone()
    assert (row["deal_ratio"], row["local_deal_ratio"], row["scam_ratio"]) == (0.6, 0.5, 0.3)
    assert json.loads(row["sources"]) == ["ebay", "craigslist"] and json.loads(row["exclude_terms"]) == ["size a"]
    assert "Herman Miller Aeron" in client.get("/").text
    assert "Won't alert until it has 6 eBay samples" in client.get("/").text
    edit = client.get(f"/watches/{wid}/edit").text
    assert 'value="40"' in edit and "size a" in edit
    client.post(f"/watches/{wid}", data={**FORM, "max_price": "$450", "deal_pct": "35"})
    row = db.execute("SELECT max_price, deal_ratio FROM watches WHERE id = ?", (wid,)).fetchone()
    assert (row[0], row[1]) == (450, 0.65)


def test_form_errors_keep_input(client):
    r = client.post("/watches", data={**FORM, "name": "", "exclude_terms": "(unclosed", "src_ebay": "", "src_craigslist": ""})
    assert r.status_code == 200
    assert "Give the watch a name." in r.text and "not a valid pattern" in r.text and "Pick at least one source." in r.text
    assert "(unclosed" in r.text


def test_toggle_and_delete(client):
    wid = create(client)
    client.post(f"/watches/{wid}/toggle")
    assert client.app.state.db.execute("SELECT enabled FROM watches").fetchone()[0] == 0
    client.post(f"/watches/{wid}/delete")
    assert client.get(f"/watches/{wid}").status_code == 404


def seed(client, wid):
    db = client.app.state.db
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for i, (title, price) in enumerate([("Herman Miller Aeron Size B graphite", 400), ("Herman Miller Aeron Size C mesh", 650),
                                        ("Herman Miller Aeron remastered", 700)]):
        db.execute("INSERT INTO listings (source, source_id, watch_id, title, url, price_total, location, first_seen,"
                   " last_seen, tier) VALUES ('craigslist', ?, ?, ?, ?, ?, '12 mi from Grand Rapids, MI', ?, ?, ?)",
                   (f"c{i}", wid, title, f"https://cl/{i}", price, now, now, "DEAL" if i == 0 else None))
    for i, p in enumerate([780, 800, 800, 810, 820, 850]):
        db.execute("INSERT INTO price_samples (watch_id, source_id, price, first_seen) VALUES (?, ?, ?, ?)", (wid, f"e{i}", p, now))
    db.execute("INSERT INTO market_history VALUES (?, '2026-10-21', 790, 6), (?, '2026-10-22', 805, 6)", (wid, wid))
    lid = db.execute("SELECT id FROM listings WHERE source_id = 'c0'").fetchone()[0]
    db.execute("INSERT INTO alerts (listing_id, watch_id, price_total, tier, ratio, typical_at_alert, sent_at, telegram_ok, created_at)"
               " VALUES (?, ?, 400, 'DEAL', 0.5, 805, ?, 1, ?)", (lid, wid, now, now))


def test_detail_alerts_and_chart(client):
    wid = create(client)
    seed(client, wid)
    page = client.get(f"/watches/{wid}").text
    assert "$805" in page and "50%" in page  # typical and % below for the $400 listing
    assert page.index("Size B graphite") < page.index("remastered")  # cheapest relative to typical first
    assert '<polyline class="line"' in page and "2026-10-21" in page
    alerts = client.get("/alerts").text
    assert "Size B graphite" in alerts and "DEAL" in alerts
    assert "No alerts match" in client.get(f"/alerts?tier=LONG+SHOT").text
    assert "Size B graphite" in client.get(f"/alerts?watch={wid}&tier=DEAL").text


def test_not_relevant_adds_exclude(client):
    wid = create(client)
    seed(client, wid)
    lid = client.app.state.db.execute("SELECT id FROM listings WHERE source_id = 'c1'").fetchone()[0]
    chips = client.get(f"/watches/{wid}/not-relevant/{lid}").text
    assert "mesh" in chips and "aeron" not in chips  # query words are never offered
    client.post(f"/watches/{wid}/exclude", data={"word": "mesh"})
    excl = json.loads(client.app.state.db.execute("SELECT exclude_terms FROM watches").fetchone()[0])
    assert excl == ["size a", r"\bmesh\b"]
    assert "Size C mesh" not in client.get(f"/watches/{wid}").text  # hidden immediately


def test_settings_save_validate_reset(client):
    s = client.get("/settings").text
    assert "for parts" in s and "not set" in s  # blacklist shown; no token in this test env
    form = {"home_label": "Grand Rapids, MI", "home_lat": "42.96", "home_lon": "-85.67", "radius_miles": "100",
            "scan_interval_min": "45", "min_seller_feedback": "25", "min_seller_positive_pct": "97",
            "telegram_enabled": "on", "hard_blacklist": "for parts\nbroken", "soft_flags": "untested",
            "rss_feeds": "swap | https://reddit.com/r/hardwareswap/new/.rss | swap"}
    assert client.post("/settings", data=form, follow_redirects=False).status_code == 303
    from dealwatch import settings
    st = settings.load(client.app.state.db)
    assert st["radius_miles"] == 100 and st["hard_blacklist"] == ["for parts", "broken"]
    assert st["rss_feeds"] == [{"name": "swap", "url": "https://reddit.com/r/hardwareswap/new/.rss", "used": True}]
    bad = client.post("/settings", data={**form, "home_lat": "north", "soft_flags": "(oops"})
    assert "Not saved" in bad.text and "not a number" in bad.text
    client.post("/settings/reset/hard_blacklist")
    assert len(settings.load(client.app.state.db)["hard_blacklist"]) > 2


def test_password(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULER", "off")
    monkeypatch.setenv("UI_PASSWORD", "hunter2")
    from dealwatch.app import app
    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/healthz").status_code == 200
        good = base64.b64encode(b"me:hunter2").decode()
        assert c.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200


class FakeEbay(Source):
    name, label, local = "ebay", "eBay", False

    def search(self, q, w):
        titles = [("Herman Miller Aeron Size B", 800), ("Herman Miller Aeron Size C", 820), ("Herman Miller Aeron", 790),
                  ("Herman Miller Aeron chair", 810), ("Herman Miller Aeron B", 805), ("Herman Miller Aeron C", 830),
                  ("Arm pads for Herman Miller Aeron", 30), ("Herman Miller Aeron size A", 300), ("Steelcase Leap", 400)]
        return [Listing("ebay", f"e{i}", t, f"https://e/{i}", p, location="Located in United States", seller_fb_count=500,
                        seller_fb_pct=100.0) for i, (t, p) in enumerate(titles)]


def test_preview_explains_and_estimates(client, monkeypatch):
    monkeypatch.setattr(preview, "registry", lambda: {"ebay": FakeEbay})
    r = client.post("/preview", data={**FORM, "src_craigslist": ""})
    job_id = r.text.split('hx-get="/preview/')[1].split('"')[0]
    for _ in range(50):
        html = client.get(f"/preview/{job_id}").text
        if "Excluded, and why" in html:
            break
        time.sleep(0.1)
    assert "<strong>6</strong> matching listings" in html
    assert "eBay asking median of 6 clean listings" in html
    assert "looks like a part or accessory" in html and "excluded: “size a”" in html and "missing: “herman”" in html
    assert client.app.state.db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0  # nothing stored


def test_preview_validation(client):
    assert "Add at least one search term." in client.post("/preview", data={**FORM, "search_queries": ""}).text
