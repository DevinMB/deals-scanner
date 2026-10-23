"""Offline parser tests. craigslist_search.json is a real API response (trimmed), captured 2026-10-23."""

import json
from pathlib import Path

from dealwatch.models import Watch
from dealwatch.sources import Context, miles, money
from dealwatch.sources import craigslist, rss
from dealwatch.sources.craigslist import Craigslist
from dealwatch.sources.rss import Rss, prices_near, rss_items

FIXTURES = Path(__file__).parent / "fixtures"
AERON = Watch(id=1, name="Herman Miller Aeron", search_queries=["herman miller aeron"])
RTX3090 = Watch(id=2, name="RTX 3090", search_queries=["rtx 3090"], include_regex=r"(?<!\d)3090(?!\d)(?!\s*ti)")


class FakeResponse:
    def __init__(self, data=None, content=b""):
        self._data, self.content = data, content

    def json(self):
        return self._data


def test_money_and_miles():
    assert money("Price: $1,234.50 obo") == 1234.5
    assert money("free") is None
    chicago = miles({"lat": 42.96, "lon": -85.67}, 41.8852, -87.6367)
    assert 120 < chicago < 130


def test_craigslist_parses_real_items(monkeypatch):
    data = json.loads((FIXTURES / "craigslist_search.json").read_text())
    monkeypatch.setattr(craigslist, "http_get", lambda url, **p: FakeResponse({"data": data}))
    got = Craigslist(Context()).search("herman miller aeron", AERON)
    assert len(got) == 3
    first = got[0]
    assert first.source_id == str(data["decode"]["minPostingId"] + data["items"][0][0])
    assert first.price_total == float(data["items"][0][3])
    assert first.url.startswith("https://www.craigslist.org/view/d/")
    assert first.location.endswith("mi from Grand Rapids, MI") and first.distance_mi < 150
    assert first.seller_fb_count is None


def test_craigslist_radius_filter(monkeypatch):
    data = json.loads((FIXTURES / "craigslist_search.json").read_text())
    monkeypatch.setattr(craigslist, "http_get", lambda url, **p: FakeResponse({"data": data}))
    near = Watch(**{**AERON.__dict__, "radius_miles": 10})
    assert Craigslist(Context()).search("herman miller aeron", near) == []


def test_craigslist_no_results(monkeypatch):
    monkeypatch.setattr(craigslist, "http_get", lambda url, **p: FakeResponse({"data": {"items": [], "decode": 0}}))
    assert Craigslist(Context()).search("zzz", AERON) == []


ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>t3_a</id><title>[USA-MI] [H] EVGA RTX 3090 FTW3 [W] PayPal, Local Cash</title>
 <link href="https://reddit.com/a"/><content>Asking $700 shipped for the 3090, $40 for the cable kit</content></entry>
<entry><id>t3_b</id><title>[USA-CA] [H] PayPal [W] RTX 3090</title><link href="https://reddit.com/b"/><content/></entry>
</feed>"""
RSS2 = b"""<?xml version="1.0"?><rss><channel>
<item><guid>sd1</guid><title>Herman Miller Aeron Size B Remastered $899 + Free S/H</title>
 <link>https://slickdeals.net/1</link><description>Great chair</description></item>
</channel></rss>"""


def test_rss_items_atom_and_rss2():
    assert [i[0] for i in rss_items(ATOM)] == ["t3_a", "t3_b"]
    assert list(rss_items(RSS2))[0][:3] == ("sd1", "Herman Miller Aeron Size B Remastered $899 + Free S/H",
                                            "https://slickdeals.net/1")


def test_prices_near_item_mention():
    # Only prices after the mention, and only 3+ digits (skips small add-ons), as in the reference
    assert prices_near("asking $700 shipped for the 3090, $40 for the cable kit", RTX3090) == []
    assert prices_near("3090 for $700 shipped, $1,250 for two", RTX3090) == [700.0, 1250.0]


def test_rss_swap_reads_only_have_side(monkeypatch):
    feeds = {"a": ATOM, "b": RSS2}
    monkeypatch.setattr(rss, "http_get", lambda url, **p: FakeResponse(content=feeds[url]))
    monkeypatch.setattr(rss, "FEED_SLEEP", 0)
    ctx = Context()
    ctx.settings = {**ctx.settings, "rss_feeds": [{"name": "swap", "url": "a", "used": True},
                                                  {"name": "sd", "url": "b", "used": False}]}
    src = Rss(ctx)
    gpu = src.search("", RTX3090)
    assert [l.source_id for l in gpu] == ["t3_a"]  # t3_b only *wants* a 3090
    chair = src.search("", AERON)
    assert [(l.source_id, l.price_total) for l in chair] == [("sd1", 899.0)]
    assert chair[0].extra["note"].startswith("price parsed")
