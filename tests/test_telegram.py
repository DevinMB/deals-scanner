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
    assert n.alert(alert(tier="LONG SHOT", quiet=True), [{"id": 1, "chat_id": None}]) == {1}  # None = owner chat
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
    assert TelegramNotifier(TOKEN, ["42"], enabled=lambda: False).alert(alert(), [{"id": 1, "chat_id": None}]) == set()
    assert post.calls == []


def test_alert_per_recipient_and_recent_chats(monkeypatch):
    post = FakePost([{"ok": True}, {"ok": False, "description": "bot was blocked by the user"}])
    monkeypatch.setattr(telegram.httpx, "post", post)
    n = TelegramNotifier(TOKEN, ["42"])
    assert n.alert(alert(), [{"id": 1, "chat_id": None}, {"id": 2, "chat_id": "99"}]) == {1}
    assert [d["chat_id"] for _, d in post.calls] == ["42", "99"]
    updates = {"ok": True, "result": [
        {"message": {"chat": {"id": 99, "first_name": "Alex", "last_name": "K", "username": "alexk"}}},
        {"message": {"chat": {"id": 99, "first_name": "Alex", "last_name": "K"}}},
        {"edited_message": {"chat": {"id": -100, "title": "Deals group"}}}]}
    monkeypatch.setattr(telegram.httpx, "get", lambda url, timeout: httpx.Response(200, json=updates))
    chats, err = n.recent_chats()
    assert err == "" and [(c["chat_id"], c["name"]) for c in chats] == [("99", "Alex K"), ("-100", "Deals group")]


def test_suggest_filters_excludes_that_would_block_the_item(monkeypatch):
    import json as _json
    from dealwatch import llm
    reply = {"search_terms": ["Fujifilm X100V", "x100v body"], "typical_price_usd": 1400.4, "note": "check shutter",
             "exclude": ["x100", "X100VI", "fujifilm", "lens hood", "x100vi", "body", "c++ case"]}
    monkeypatch.setattr(llm.httpx, "post", lambda url, timeout, json: httpx.Response(
        200, json={"message": {"content": _json.dumps(reply)}}, request=httpx.Request("POST", url)))
    s, err = llm.suggest("http://ollama", "m", "Fujifilm X100V")
    assert err == "" and s["search_terms"] == ["fujifilm x100v", "x100v body"] and s["typical_price"] == 1400
    # "fujifilm" (in the name) and "body" (in a search term) are dropped; the rest become whole-word patterns
    assert s["exclude"] == [r"\bx100\b", r"\bx100vi\b", r"\blens\ hood\b", r"\bc\+\+\ case\b"]
    import re
    assert not re.search(s["exclude"][0], "fujifilm x100v silver")  # whole word: x100 doesn't block x100v
    assert llm.suggest("", "m", "x")[0] is None
