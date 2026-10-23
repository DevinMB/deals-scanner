import httpx

from dealwatch import telegram
from dealwatch.telegram import TelegramNotifier, format_alert

TOKEN = "123456:SECRET-token"


def alert(**kw):
    a = {"watch": "Herman Miller Aeron", "watch_id": 2, "listing_id": 9, "tier": "DEAL", "ratio": 420 / 810,
         "typical": 810, "typical_src": "eBay asking, 31 samples", "reasons": [], "count": 1, "is_bundle": False,
         "title": "Herman Miller Aeron Size B Fully Loaded Graphite", "url": "https://cl/1", "price_total": 420.0,
         "source": "Craigslist", "location": "34 mi from Grand Rapids, MI", "seller": None, "condition": None,
         "note": None, "best_offer": False, "max_price": None, "quiet": False}
    return {**a, **kw}


def test_layout_matches_brief_example():
    assert format_alert(alert()) == "\n".join([
        "🚨 DEAL: Herman Miller Aeron — 48% below typical",
        "$420 all-in vs ~$810 typical (eBay asking, 31 samples)",
        "Herman Miller Aeron Size B Fully Loaded Graphite",
        "Source: Craigslist · 34 mi from Grand Rapids, MI",
        "https://cl/1",
        "Check photos, seller history & returns before buying.",
    ])


def test_double_siren_at_half_price_and_web_link():
    text = format_alert(alert(ratio=0.45, price_total=365.0), base_url="http://192.168.10.225:8080/")
    assert text.startswith("🚨🚨 DEAL")
    assert "Deal Watch: http://192.168.10.225:8080/watches/2" in text


def test_long_shot_lists_reasons_and_ebay_details():
    text = format_alert(alert(tier="LONG SHOT", ratio=0.25, price_total=200.0, source="eBay",
                              reasons=["price is 75% below typical — scam/broken risk"],
                              seller="bob 98% positive (12)", condition="Pre-Owned", best_offer=True))
    assert text.startswith("🎲 LONG SHOT: Herman Miller Aeron — 75% below typical")
    assert "Why it's a long shot: price is 75% below typical — scam/broken risk" in text
    assert "Condition: Pre-Owned · Best Offer" in text and "Seller: bob 98% positive (12)" in text


def test_quantity_bundle_and_max_price():
    text = format_alert(alert(watch="RTX 3090", count=2, is_bundle=True, ratio=0.8, typical=750, price_total=1200.0))
    assert text.startswith("🖥️🚨 BUNDLE DEAL: 2x RTX 3090 inside a bundle — 20% below the item alone")
    assert "vs ~$1,500 typical" in text and "Confirm the RTX 3090" in text
    text = format_alert(alert(ratio=None, typical=None, max_price=500, price_total=480.0))
    assert text.splitlines()[:2] == ["🚨 DEAL: Herman Miller Aeron", "$480 all-in, at or under your $500 max price"]


def test_rss_note_shown():
    assert "(price parsed from post text — verify)" in format_alert(alert(note="price parsed from post text — verify"))


class FakePost:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, timeout, data):
        self.calls.append((url, data))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return httpx.Response(200, json=r)


def test_send_ok_quiet_and_no_preview(monkeypatch):
    post = FakePost([{"ok": True}])
    monkeypatch.setattr(telegram.httpx, "post", post)
    n = TelegramNotifier(TOKEN, ["42"])
    assert n.alert(alert(tier="LONG SHOT", quiet=True)) is True
    url, data = post.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert (data["chat_id"], data["disable_notification"], data["disable_web_page_preview"]) == ("42", "true", "true")


def test_failure_is_redacted(monkeypatch):
    err = httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage")
    monkeypatch.setattr(telegram.httpx, "post", FakePost([err, {"ok": False, "description": "chat not found"}]))
    n = TelegramNotifier(TOKEN, ["42", "43"])
    ok, reason = n.test()
    assert not ok and TOKEN not in reason and "<token>" in reason and "chat not found" in reason


def test_dry_run_unconfigured_and_switched_off(monkeypatch):
    post = FakePost([])
    monkeypatch.setattr(telegram.httpx, "post", post)
    assert TelegramNotifier(TOKEN, ["42"], dry_run=True).send("hi") == (False, "dry-run mode: not sent")
    assert TelegramNotifier(None, [""]).test()[0] is False
    assert TelegramNotifier(TOKEN, ["42"], enabled=lambda: False).alert(alert()) is False
    assert post.calls == []
