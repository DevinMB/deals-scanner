"""Offline parser tests. craigslist_search.json is a real API response (trimmed), captured 2026-10-23."""

import json
from pathlib import Path

from dealwatch.models import Watch
from dealwatch.sources import Context, Listing, miles, money
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


def ebay_card(**kw):
    card = {"id": "377563046560", "title": "EVGA GeForce RTX 3090 FTW3 ULTRA 24GB", "cond": "Pre-Owned",
            "prices": ["$650.00"], "rows": ["+$25.00 delivery", "Located in United States"],
            "seller": "jaswa-6839 100% positive (2.1K)"}
    return {**card, **kw}


def test_ebay_parse_card():
    from dealwatch.sources.ebay import Ebay
    l = Ebay._parse(ebay_card())
    assert (l.price_total, l.seller_fb_count, l.seller_fb_pct) == (675.0, 2100.0, 100.0)
    assert l.location == "Located in United States" and l.url == "https://www.ebay.com/itm/377563046560"


def test_ebay_skips_junk_cards():
    from dealwatch.sources.ebay import Ebay
    assert Ebay._parse(ebay_card(title="Shop on eBay", prices=["$20.00"])) is None
    assert Ebay._parse(ebay_card(prices=["$500.00", "$700.00"])) is None  # variation price range
    assert Ebay._parse(ebay_card(cond="For parts or not working")) is None
    assert Ebay._parse(ebay_card(seller="newbie")).seller_fb_count == 0  # no feedback = untrusted


def test_offerup_parse_tile():
    from dealwatch.sources.offerup import OfferUp
    l = OfferUp._parse("https://offerup.com/item/detail/9409f614?cid=x", ["$500", "Herman Miller Aeron Chair Size B", "Holland, MI"])
    assert (l.source_id, l.price_total, l.title, l.location) == ("9409f614", 500.0, "Herman Miller Aeron Chair Size B", "Holland, MI")
    assert OfferUp._parse("https://offerup.com/item/detail/1", ["Free", "Couch"]) is None


def test_local_query_broadens_specific_searches():
    from dealwatch.sources import local_query
    assert local_query("rtx 4090 24gb") == "rtx 4090"
    assert local_query("nvidia a100 80gb pcie") == "a100"
    assert local_query("radeon pro w7900 48gb") == "pro w7900"
    assert local_query("herman miller aeron") == "herman miller aeron"
    assert local_query("nvidia") == "nvidia"  # never empty


def test_walmart_parses_next_data(monkeypatch):
    from dealwatch.sources import walmart
    data = {"props": {"pageProps": {"initialData": {"searchResult": {"itemStacks": [{"items": [
        {"__typename": "Product", "usItemId": "1", "name": "Filtrete 16x25x1 MPR 1000, 6 Filters", "price": 64.97,
         "canonicalUrl": "/ip/filtrete/1?classType=REGULAR", "sellerName": "Walmart.com",
         "availabilityStatusV2": {"value": "IN_STOCK"}},
        {"__typename": "Product", "usItemId": "2", "name": "No price variant", "price": 0, "canonicalUrl": "/ip/2"},
        {"__typename": "Product", "usItemId": "3", "name": "Gone", "price": 10, "canonicalUrl": "/ip/3",
         "availabilityStatusV2": {"value": "OUT_OF_STOCK"}},
        {"__typename": "AdPlaceholder"}]}]}}}}}
    page = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'
    monkeypatch.setattr(walmart, "http_get", lambda url, **p: type("R", (), {"text": page})())
    got = walmart.Walmart(Context()).search("filtrete", AERON)
    assert [(l.source_id, l.price_total, l.url) for l in got] == [("1", 64.97, "https://www.walmart.com/ip/filtrete/1")]


def test_amazon_parse_card():
    from dealwatch.sources.amazon import Amazon
    card = {"asin": "B07FP5X3JM", "title": "Sponsored\nFiltrete\n16x25x1 Air Filter, MERV 11, MPR 1000, 6 HVAC Filters",
            "price": "$70.39", "priceText": "Price, product page\n$70.39\n($11.73/count)\nTypical: $79.99"}
    l = Amazon._parse(card)
    assert l.title == "Filtrete 16x25x1 Air Filter, MERV 11, MPR 1000, 6 HVAC Filters" and l.price_total == 70.39
    assert l.extra == {"sponsored": True, "store_typical": 79.99} and l.url == "https://www.amazon.com/dp/B07FP5X3JM"
    assert Amazon._parse({**card, "price": ""}) is None


def test_slickdeals_rss(monkeypatch):
    from dealwatch.sources import slickdeals
    rss = b"""<?xml version="1.0"?><rss><channel><title>search</title>
    <item><title>Filtrete 20x25x1 MERV 12 MPR 1500, 6-Pack $66.25 at Amazon</title><link>https://slickdeals.net/f/19001-filtrete</link></item>
    <item><title>3M MPR 2500 Filters 4-Pack $44.99 Instore @ Costco. $49.99 on website.</title><link>https://slickdeals.net/f/19002-3m</link></item>
    <item><title>Free filter guide</title><link>https://slickdeals.net/f/19003</link></item>
    <item><title>Old one $25.08 at Amazon</title><link>https://slickdeals.net/f/18000-old?utm_source=rss</link>
      <pubDate>Fri, 24 Jul 2026 08:00:14 +0000</pubDate></item></channel></rss>"""
    monkeypatch.setattr(slickdeals, "http_get", lambda url, **p: type("R", (), {"content": rss})())
    got = slickdeals.Slickdeals(Context()).search("filtrete", AERON)
    assert [(l.source_id, l.price_total, l.location) for l in got] == [("19001-filtrete", 66.25, "Amazon"), ("19002-3m", 44.99, "Costco")]


def test_query_words_ignore_punctuation():
    from dealwatch.scoring import Rules, classify, query_words
    w = Watch(id=9, name="Filtrete", search_queries=["Filtrete 16x25x1 Air Filter, MERV 11, MPR 1000"])
    assert query_words(w) == ["filtrete", "16x25x1", "air", "filter", "merv", "11", "mpr", "1000"]
    assert classify("Filtrete 16x25x1 Air Filter MPR 1000 MERV 11 (6 pack)", w, Rules.from_settings())



def test_slickdeals_expired_check(monkeypatch):
    from dealwatch.sources import slickdeals
    pages = {"https://slickdeals.net/f/1": '{"isExpiredDeal":"Yes","threadId":1}', "https://slickdeals.net/f/2": '{"isExpiredDeal":"No"}'}
    monkeypatch.setattr(slickdeals, "http_get", lambda url, **p: type("R", (), {"text": pages[url]})())
    src = slickdeals.Slickdeals(Context())
    mk = lambda u: Listing("slickdeals", u[-1], "t", u, 10.0)
    assert src.is_available(mk("https://slickdeals.net/f/1")) is False
    assert src.is_available(mk("https://slickdeals.net/f/2")) is True
