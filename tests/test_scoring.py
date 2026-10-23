"""Scoring tests. GPU watches mirror reference/config.json models; Aeron is the brief's example."""

import pytest

from dealwatch.models import Watch
from dealwatch.scoring import (DEFAULTS, Match, Rules, classify, effective_typical, is_market_sample, score, seller_ok,
                               typical_price)

RULES = Rules.from_settings()

# As the GPU importer will build it: model excludes + the GPU-only blacklist
RTX3090 = Watch(id=1, name="RTX 3090", search_queries=["rtx 3090 24gb"],
                include_regex=r"(?<!\d)3090(?!\d)(?!\s*ti)",
                exclude_terms=["3090 ti", "3090ti", "optiplex", "precision", "snow"] + DEFAULTS["gpu_import_exclude"],
                typical_override=750)
RTX3090_BUNDLE = Watch(**{**RTX3090.__dict__, "bundle_enabled": True})
AERON = Watch(id=2, name="Herman Miller Aeron", search_queries=["herman miller aeron"])


def c(title, watch=RTX3090, body=""):
    return classify(title, watch, RULES, body)


# ---------- classify ----------

@pytest.mark.parametrize("title", [
    "EVGA RTX 3090 FTW3 for parts",
    "RTX 3090 not working no display",
    "RTX 3090 Founders Edition BROKEN",
    "NVIDIA RTX 3090 no post",
    "RTX 3090 box only",
    "EKWB water block for RTX 3090",
    "Backplate RTX 3090 Founders",
    "PCIe riser cable RTX 3090",
    "Rent RTX 3090 cloud GPU per hour",
    "RTX 3090 laptop",
    "WTB RTX 3090",
    "Looking for RTX 3090",
    "Lot of RTX 3090 cards",
    "Fan shroud for ASUS RTX 3090",
    "Gaming PC parting out RTX 3090",
])
def test_hard_blacklist_drops(title):
    assert c(title) is None


@pytest.mark.parametrize("title", [
    "RTX 3090 Ti 24GB",          # include_regex lookahead
    "rtx 3090ti founders",       # exclude term
    "Dell Precision 7920 RTX 3090",
    "RTX 3080 10GB",             # no match
])
def test_other_items_do_not_match(title):
    assert c(title) is None


def test_clean_match():
    assert c("NVIDIA GeForce RTX 3090 Founders Edition 24GB") == Match(count=1, is_bundle=False, flags=[])


@pytest.mark.parametrize("title,count", [
    ("2x NVIDIA RTX 3090 24GB", 2),
    ("Pair of RTX 3090 FE", 2),
    ("Quad RTX 3090 set", 4),
    ("8x rtx 3090 mining rig cards", 8),
    ("Dual fan RTX 3090", 1),    # "dual fan" is not a quantity
])
def test_quantity(title, count):
    m = c(title)
    assert m is not None and m.count == count


def test_soft_flags():
    m = c("RTX 3090 untested, sold as-is, read description")
    assert m.flags == ["as-is", "description", "read", "untested"]


def test_bundle_dropped_unless_enabled():
    t = "Gaming PC i9-10900K 64GB DDR4 RTX 3090"
    assert c(t) is None
    assert c(t, RTX3090_BUNDLE).is_bundle


def test_bundle_found_in_body():
    title = "Custom gaming PC, Ryzen 9 5950X, 32GB RAM"
    body = "Selling my rig. Graphics: EVGA RTX 3090 FTW3. Works great."
    assert c(title, RTX3090_BUNDLE) is None
    m = c(title, RTX3090_BUNDLE, body)
    assert m.is_bundle and m.count == 1
    assert c(title, RTX3090, body) is None  # body ignored when bundles are off


def test_body_ignored_when_title_names_other_gpu():
    assert c("Gaming PC RTX 4080", RTX3090_BUNDLE, "upgradeable to a 3090 later") is None


def test_card_pulled_from_pc_is_not_bundle():
    assert c("RTX 3090 pulled from a gaming PC, works great").is_bundle is False


def test_default_include_is_all_query_words():
    assert c("Herman Miller Aeron Size B Fully Loaded Graphite", AERON) == Match(1, False, [])
    assert c("Herman Miller Sayl chair", AERON) is None
    assert c("Pair of Herman Miller Aeron chairs", AERON).count == 2


def test_watch_exclude_terms_are_regex():
    w = Watch(**{**AERON.__dict__, "exclude_terms": ["Size A", r"\bmesh only\b"]})
    assert c("Herman Miller Aeron size a", w) is None
    assert c("Herman Miller Aeron MESH ONLY seat", w) is None
    assert c("Herman Miller Aeron Size B", w) is not None


def test_gpu_terms_are_not_global():
    laptop = Watch(id=3, name="ThinkPad X1", search_queries=["thinkpad x1"])
    assert c("Lenovo ThinkPad X1 Carbon laptop with cable", laptop) is not None
    assert c("RTX 3090 laptop") is None  # still blocked on the GPU watch


# ---------- score / tier ----------

def m(count=1, bundle=False, flags=()):
    return Match(count=count, is_bundle=bundle, flags=list(flags))


def s(total, match=None, watch=RTX3090, typical=1000.0, local=False, trusted=True):
    return score(total, match or m(), watch, RULES, typical, local=local, seller_trusted=trusted)


def test_deal_on_ebay():
    r = s(600)
    assert (r.tier, r.ratio, r.reasons) == ("DEAL", 0.6, [])


def test_above_threshold_is_nothing():
    assert s(601) is None


def test_local_between_050_and_060_is_long_shot():
    assert s(500, local=True).tier == "DEAL"
    r = s(550, local=True)
    assert r.tier == "LONG SHOT" and "normal local pricing" in r.reasons[0]


def test_scam_range_is_long_shot_and_floor_drops():
    r = s(250)
    assert r.tier == "LONG SHOT" and "scam/broken risk" in r.reasons[0]
    assert s(79) is None  # below longshot_floor 0.08


def test_flags_and_seller_make_long_shot():
    assert s(500, m(flags=["untested"])).reasons == ["title mentions: untested"]
    assert s(500, trusted=False).reasons == ["seller has little or mixed feedback"]


def test_longshots_can_be_disabled():
    w = Watch(**{**RTX3090.__dict__, "longshot_enabled": False})
    assert s(250, watch=w) is None


def test_quantity_multiplies_typical():
    assert s(1200, m(count=2)).ratio == 0.6


def test_bundle_uses_bundle_ratio():
    assert s(850, m(bundle=True)).tier == "BUNDLE DEAL"
    assert s(851, m(bundle=True)) is None


def test_no_typical_never_alerts():
    assert s(1, typical=None) is None


def test_max_price_forces_deal_regardless_of_median():
    w = Watch(**{**AERON.__dict__, "max_price": 500})
    r = s(480, watch=w, typical=None)
    assert r.tier == "DEAL" and r.ratio is None
    assert s(480, watch=w, typical=600).tier == "DEAL"  # ratio 0.8 would not alert on its own
    assert s(501, watch=w, typical=None) is None


def test_seller_ok():
    assert seller_ok(None, None, RULES)
    assert seller_ok(25, 97.0, RULES)
    assert not seller_ok(24, 100.0, RULES)
    assert not seller_ok(1000, 96.9, RULES)
    assert not seller_ok(1000, None, RULES)


# ---------- market ----------

def test_market_sample_filter():
    assert is_market_sample(m(), "Located in United States")
    assert not is_market_sample(m(), "Located in China")
    assert not is_market_sample(m(count=2), "United States")
    assert not is_market_sample(m(bundle=True), "United States")
    assert not is_market_sample(m(flags=["untested"]), "United States")
    assert not is_market_sample(None, "United States")


def test_typical_price_needs_min_samples():
    assert typical_price([800] * 5, RULES) == (None, 5)
    assert typical_price([800] * 6, RULES) == (800, 6)


def test_typical_price_trims_10_percent():
    vals = [100] + [800] * 8 + [5000]  # 10 samples: one trimmed from each end
    assert typical_price(vals, RULES) == (800, 10)


def test_effective_typical():
    assert effective_typical(810, RTX3090) == 750  # override wins when set and on
    assert effective_typical(None, RTX3090) == 750
    off = Watch(**{**RTX3090.__dict__, "override_enabled": False})
    assert effective_typical(810, off) == 810
    assert effective_typical(None, off) is None
    assert effective_typical(810, AERON) == 810
    assert effective_typical(None, AERON) is None
