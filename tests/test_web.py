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
    preview.JOBS.clear()
    client.post("/preview", data={**FORM, "src_craigslist": ""})
    job_id = next(iter(preview.JOBS))  # the fake source can finish before the first render, so don't parse it
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


def test_gpu_import_creates_17_and_updates_by_name(client):
    create(client, name="RTX 3090", search_queries="rtx 3090")
    assert "Import the 16 GPU watches" in client.get("/").text
    r = client.post("/import/gpu", follow_redirects=False)
    assert r.headers["location"] == "/?imported=16+new,+1+updated"
    db = client.app.state.db
    assert db.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 17
    w = db.execute("SELECT * FROM watches WHERE name = 'RTX 3090'").fetchone()
    assert (w["typical_override"], w["override_enabled"], w["bundle_enabled"], w["bundle_ratio"]) == (750, 1, 1, 0.85)
    assert w["include_regex"] == r"(?<!\d)3090(?!\d)(?!\s*ti)"
    ex = json.loads(w["exclude_terms"])
    assert r"3090\ ti" in ex and r"\blaptop\b" in ex
    assert "Import the" not in client.get("/").text
    # re-import is idempotent
    client.post("/import/gpu")
    assert db.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 17


def test_imported_gpu_watch_matches_like_the_old_scanner(client):
    from dealwatch import gpu_import
    from dealwatch.models import Watch
    from dealwatch.scoring import Rules, classify
    client.post("/import/gpu")
    rules = Rules.from_settings()
    watches = {r["name"]: Watch.from_row(r) for r in client.app.state.db.execute("SELECT * FROM watches")}
    assert classify("NVIDIA GeForce RTX 3090 Founders Edition 24GB", watches["RTX 3090"], rules)
    assert not classify("RTX 3090 Ti 24GB", watches["RTX 3090"], rules)
    assert classify("RTX 3090 Ti 24GB", watches["RTX 3090 Ti"], rules)
    assert not classify("Dell Precision 7920 RTX 3090", watches["RTX 3090"], rules)
    assert not classify("RTX 4090 laptop", watches["RTX 4090"], rules)
    assert classify("Gaming PC i9-13900K 64GB DDR5 RTX 4090", watches["RTX 4090"], rules).is_bundle
    assert not classify("NVIDIA A100 80GB SXM4", watches["A100 80GB"], rules)
    assert classify("NVIDIA A100 80GB PCIe", watches["A100 80GB"], rules)


def test_recipients_flow(client, monkeypatch):
    db = client.app.state.db
    wid = create(client, **{"rcpt_1": "on"})
    assert "rcpt_1" in client.get("/watches/new").text and 'name="rcpt_1" checked' in client.get("/watches/new").text
    # add Alex to all watches, then remove Me from this watch
    client.post("/recipients", data={"name": "Alex", "chat_id": "777", "all_watches": "on"})
    alex = db.execute("SELECT id FROM recipients WHERE name = 'Alex'").fetchone()[0]
    assert {r[0] for r in db.execute("SELECT recipient_id FROM watch_recipients WHERE watch_id = ?", (wid,))} == {1, alex}
    assert "→ Me, Alex" in client.get("/").text
    client.post(f"/watches/{wid}", data={**FORM, f"rcpt_{alex}": "on"})
    assert [r[0] for r in db.execute("SELECT recipient_id FROM watch_recipients WHERE watch_id = ?", (wid,))] == [alex]
    edit = client.get(f"/watches/{wid}/edit").text
    assert 'name="rcpt_1" checked' not in edit and f'name="rcpt_{alex}" checked' in edit
    # rename, bad input, owner can't be deleted, Alex can
    client.post(f"/recipients/{alex}/rename", data={"name": "Alex K"})
    assert "Alex K" in client.get("/settings").text
    assert "people_error=1" in client.post("/recipients", data={"name": "Bob", "chat_id": "abc"}, follow_redirects=False).headers["location"]
    client.post("/recipients/1/delete")
    assert db.execute("SELECT COUNT(*) FROM recipients WHERE is_owner = 1").fetchone()[0] == 1
    client.post(f"/recipients/{alex}/delete")
    assert "Alerts go to nobody" in client.get("/").text


def test_recent_chats_offers_new_people_only(client, monkeypatch):
    client.post("/recipients", data={"name": "Alex", "chat_id": "777"})
    monkeypatch.setattr(client.app.state.notifier, "recent_chats",
                        lambda: ([{"chat_id": "777", "name": "Alex", "username": None},
                                  {"chat_id": "888", "name": "Sam Lee", "username": "samlee"}], ""))
    html = client.get("/telegram/recent").text
    assert 'value="888"' in html and 'value="777"' not in html and "@samlee" in html


def test_dashboard_deal_count_follows_current_filters_and_sorts_deals_first(client):
    quiet = create(client, name="Aaa quiet watch", search_queries="steelcase leap")
    wid = create(client)
    seed(client, wid)  # one DEAL-tier listing: "Herman Miller Aeron Size B graphite"
    page = client.get("/").text
    assert page.index("Herman Miller Aeron") < page.index("Aaa quiet watch")  # deals first despite the name
    assert '<dt>Deals now</dt><dd>1</dd>' in page
    client.post(f"/watches/{wid}/exclude", data={"word": "graphite"})
    assert '<dt>Deals now</dt><dd>1</dd>' not in client.get("/").text
