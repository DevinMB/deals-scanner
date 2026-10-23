"""Run loop tests with fake sources: storing, learning prices, scoring, dedup, retries, health."""

import json

import pytest

from dealwatch import db, settings
from dealwatch.runner import Notifier, run_scan
from dealwatch.sources import Listing, Source


class FakeEbay(Source):
    name, label, local = "ebay", "eBay", False
    listings: list = []
    unavailable: set = set()
    fail = False

    def search(self, query, watch):
        if FakeEbay.fail:
            raise ConnectionError("network down")
        return list(FakeEbay.listings)

    def is_available(self, listing):
        return listing.source_id not in FakeEbay.unavailable


class FakeCraigslist(Source):
    name, label = "craigslist", "Craigslist"
    listings: list = []

    def search(self, query, watch):
        return list(FakeCraigslist.listings)


class RecordingNotifier(Notifier):
    def __init__(self, ok=True):
        self.ok, self.alerts, self.health_msgs = ok, [], []

    def alert(self, a):
        self.alerts.append(a)
        return self.ok

    def health(self, text):
        self.health_msgs.append(text)
        return True


REG = {"ebay": FakeEbay, "craigslist": FakeCraigslist}


def ebay(i, price, title="Herman Miller Aeron Size B", fb=500, pct=100.0, loc="Located in United States"):
    return Listing("ebay", f"e{i}", title, f"https://www.ebay.com/itm/e{i}", price, location=loc, seller="s",
                   seller_fb_count=fb, seller_fb_pct=pct)


def cl(i, price, title="Herman Miller Aeron chair"):
    return Listing("craigslist", f"c{i}", title, f"https://cl/{i}", price, location="20 mi from Grand Rapids, MI")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    FakeEbay.listings, FakeEbay.unavailable, FakeEbay.fail, FakeCraigslist.listings = [], set(), False, []
    return c


def add_watch(conn, **kw):
    cols = {"name": "Aeron", "search_queries": json.dumps(["herman miller aeron"]),
            "sources": json.dumps(["ebay", "craigslist"]), **kw}
    return conn.execute(f"INSERT INTO watches ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        list(cols.values())).lastrowid


def scan(conn, tmp_path, notifier):
    return run_scan(conn, tmp_path / "scan.lock", notifier, sources=REG, sleep=False)


def market_pool():
    return [ebay(i, p) for i, p in enumerate([780, 800, 800, 810, 820, 850, 900])]


def test_learns_median_and_alerts_local_deal_once(conn, tmp_path):
    add_watch(conn)
    FakeEbay.listings = market_pool()
    FakeCraigslist.listings = [cl(1, 380)]
    n = RecordingNotifier()
    r = scan(conn, tmp_path, n)
    assert conn.execute("SELECT COUNT(*) FROM price_samples").fetchone()[0] == 7
    assert [a["tier"] for a in n.alerts] == ["DEAL"] and n.alerts[0]["typical"] == 810
    assert n.alerts[0]["typical_src"] == "eBay asking, 7 samples"
    assert (r.alerts_new, r.alerts_sent) == (1, 1)
    row = conn.execute("SELECT tier, sent_at, telegram_ok FROM alerts").fetchone()
    assert row["tier"] == "DEAL" and row["telegram_ok"] == 1 and row["sent_at"]
    # same listing, same price: no repeat
    n2 = RecordingNotifier()
    scan(conn, tmp_path, n2)
    assert n2.alerts == []
    # price drop: alerts again
    FakeCraigslist.listings = [cl(1, 350)]
    n3 = RecordingNotifier()
    scan(conn, tmp_path, n3)
    assert [a["price_total"] for a in n3.alerts] == [350]


def test_too_few_samples_and_no_override_never_alerts(conn, tmp_path):
    add_watch(conn)
    FakeEbay.listings = market_pool()[:5]
    FakeCraigslist.listings = [cl(1, 100)]
    n = RecordingNotifier()
    scan(conn, tmp_path, n)
    assert n.alerts == [] and conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    # but the listing is still stored for the UI
    assert conn.execute("SELECT tier FROM listings WHERE source = 'craigslist'").fetchone()["tier"] is None


def test_override_wins_over_learned(conn, tmp_path):
    add_watch(conn, typical_override=600)
    FakeEbay.listings = market_pool()
    FakeCraigslist.listings = [cl(1, 380)]  # 0.47 of 810 but 0.63 of 600
    n = RecordingNotifier()
    scan(conn, tmp_path, n)
    assert n.alerts == []


def test_forced_deal_with_max_price_alerts_once(conn, tmp_path):
    add_watch(conn, max_price=1000)
    FakeEbay.listings = [ebay(1, 900)]
    n = RecordingNotifier()
    scan(conn, tmp_path, n)
    assert [(a["tier"], a["quiet"]) for a in n.alerts] == [("DEAL", False)]
    n2 = RecordingNotifier()
    scan(conn, tmp_path, n2)
    assert n2.alerts == []


def test_long_shot_is_quiet_with_reasons(conn, tmp_path):
    add_watch(conn, typical_override=800)
    FakeEbay.listings = [ebay(1, 400, fb=3)]
    n = RecordingNotifier()
    scan(conn, tmp_path, n)
    a = n.alerts[0]
    assert a["tier"] == "LONG SHOT" and a["quiet"] and a["reasons"] == ["seller has little or mixed feedback"]


def test_failed_send_retries_next_run(conn, tmp_path):
    add_watch(conn, max_price=1000)
    FakeEbay.listings = [ebay(1, 900)]
    scan(conn, tmp_path, RecordingNotifier(ok=False))
    assert conn.execute("SELECT telegram_ok FROM alerts").fetchone()[0] == 0
    n = RecordingNotifier()
    r = scan(conn, tmp_path, n)
    assert len(n.alerts) == 1 and (r.alerts_new, r.alerts_sent) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


def test_unavailable_ebay_listing_is_skipped(conn, tmp_path):
    add_watch(conn, max_price=1000)
    FakeEbay.listings = [ebay(1, 900)]
    FakeEbay.unavailable = {"e1"}
    n = RecordingNotifier()
    scan(conn, tmp_path, n)
    assert n.alerts == [] and conn.execute("SELECT available FROM listings").fetchone()[0] == 0


def test_non_us_ebay_listing_ignored(conn, tmp_path):
    add_watch(conn, max_price=1000)
    FakeEbay.listings = [ebay(1, 900, loc="Located in China")]
    n = RecordingNotifier()
    r = scan(conn, tmp_path, n)
    assert n.alerts == [] and r.matches == 0


def test_health_warns_once_after_three_empty_runs(conn, tmp_path):
    add_watch(conn)
    FakeEbay.fail = True
    n = RecordingNotifier()
    for _ in range(4):
        scan(conn, tmp_path, n)
    # one warning per failing source, not one per run (the fake Craigslist returns nothing too)
    assert len(n.health_msgs) == 2
    assert "eBay" in n.health_msgs[0] and "network down" in n.health_msgs[0]
    assert "Craigslist" in n.health_msgs[1] and "returned no listings" in n.health_msgs[1]
    h = settings.load(conn)["health"]
    assert h["ebay"]["streak"] == 4 and h["craigslist"]["streak"] == 4  # fake CL returns nothing too
    FakeEbay.fail = False
    FakeEbay.listings = market_pool()
    scan(conn, tmp_path, n)
    assert settings.load(conn)["health"]["ebay"] == {**h["ebay"], "streak": 0, "warned": False,
                                                    "last_ok": settings.load(conn)["health"]["ebay"]["last_ok"]}


def test_runs_rows_and_lock(conn, tmp_path):
    add_watch(conn)
    FakeEbay.listings = market_pool()
    scan(conn, tmp_path, RecordingNotifier())
    rows = conn.execute("SELECT source, listings_found, error FROM runs ORDER BY id").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("ebay", 7, None), ("craigslist", 0, None)]
    from dealwatch.runner import scan_lock
    with scan_lock(tmp_path / "scan.lock") as got:
        assert got
        assert scan(conn, tmp_path, RecordingNotifier()).skipped


def test_disabled_watch_not_scanned(conn, tmp_path):
    add_watch(conn, enabled=0, max_price=1000)
    FakeEbay.listings = [ebay(1, 900)]
    n = RecordingNotifier()
    r = scan(conn, tmp_path, n)
    assert n.alerts == [] and r.parsed == {}


def test_ollama_part_check_blocks_parts_and_caches(conn, tmp_path, monkeypatch):
    from dealwatch import llm
    calls = []

    def fake(url, model, item, title, timeout=60):
        calls.append(title)
        return ("part", "seat pan frame") if "frame" in title else ("item", "complete chair")
    monkeypatch.setattr(llm, "check", fake)
    settings.put(conn, "ollama_url", "http://ollama")
    add_watch(conn, max_price=1000)
    FakeEbay.listings = [ebay(1, 900), ebay(2, 200, title="Herman Miller Aeron seat pan frame size B")]
    n = RecordingNotifier()
    scan(conn, tmp_path, n)
    assert [a["price_total"] for a in n.alerts] == [900]
    assert conn.execute("SELECT llm_verdict FROM listings WHERE source_id = 'e2'").fetchone()[0] == "part"
    scan(conn, tmp_path, RecordingNotifier())
    assert len(calls) == 2  # verdicts cached: nothing asked twice


def test_ollama_down_still_alerts_with_note(conn, tmp_path, monkeypatch):
    from dealwatch import llm
    monkeypatch.setattr(llm, "check", lambda *a, **k: None)
    settings.put(conn, "ollama_url", "http://ollama")
    add_watch(conn, max_price=1000)
    FakeEbay.listings = [ebay(1, 900)]
    n = RecordingNotifier()
    scan(conn, tmp_path, n)
    assert len(n.alerts) == 1 and "Ollama unavailable" in n.alerts[0]["note"]
