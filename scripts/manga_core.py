"""Shared, fail-closed logic for manga-Buy. Python standard library only."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")
CONFIRMED_VOLUME_STATUSES = {"OFFICIAL_CONFIRMED", "CURATED_BASELINE"}
ACCEPTED_CONDITIONS = {"GOOD", "ACCEPTABLE", "RENTAL_USED"}
BLOCKING_DAMAGE_FLAGS = {
    "WATER_DAMAGE", "MOLD", "MISSING_PAGES", "UNREADABLE",
    "SEVERE_TEAR", "HEAVY_WRITING", "STRONG_ODOR",
}


def now_jst() -> datetime:
    return datetime.now(JST).replace(microsecond=0)


def parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def age_hours(value, now=None):
    parsed = parse_dt(value)
    if parsed is None:
        return None
    current = parse_dt(now) if now is not None else now_jst()
    if current is None:
        return None
    return (current - parsed).total_seconds() / 3600


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def save_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=False)
        file.write("\n")
    os.replace(tmp, path)


def valid_https_url(url):
    try:
        parsed = urlparse(str(url or ""))
        return parsed.scheme.lower() == "https" and bool(parsed.hostname)
    except Exception:
        return False


def allowed_host(url, domains):
    try:
        host = (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return any(host == str(domain).lower() or host.endswith("." + str(domain).lower()) for domain in domains)


def as_positive_int(value):
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite() or number <= 0 or number > 1_000_000:
        return None
    if number != number.to_integral_value():
        return None
    return int(number)


def as_nonnegative_int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite() or number < 0 or number > 1_000_000:
        return None
    if number != number.to_integral_value():
        return None
    return int(number)


def manga_map(catalog):
    return {entry.get("id"): entry for entry in catalog.get("manga", []) if entry.get("id")}


def expected_count(manga, allow_provisional=False):
    status = str(manga.get("volume_count_status") or "NEEDS_REVIEW").upper()
    value = manga.get("expected_volume_count")
    if status in CONFIRMED_VOLUME_STATUSES and isinstance(value, int) and value > 0:
        return value, True
    if allow_provisional:
        value = manga.get("provisional_volume_count")
        if isinstance(value, int) and value > 0:
            return value, False
    return None, False


def normalized_ranges(value, max_volume=300):
    """Parse inclusive ranges [[1, 8], [10, 12]]. Invalid input is rejected."""
    if not isinstance(value, list) or not value:
        return None
    parsed = []
    for pair in value:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None
        start, end = pair
        if isinstance(start, bool) or isinstance(end, bool):
            return None
        if not isinstance(start, int) or not isinstance(end, int):
            return None
        if start < 1 or end < start or end > max_volume:
            return None
        parsed.append((start, end))
    parsed.sort()
    for index, current in enumerate(parsed):
        if index and current[0] <= parsed[index - 1][1]:
            return None
    return parsed


def ranges_cover_all(ranges, count):
    if not ranges or not isinstance(count, int) or count < 1:
        return False
    covered = set()
    for start, end in ranges:
        covered.update(range(start, end + 1))
    return all(volume in covered for volume in range(1, count + 1))


def missing_volumes(ranges, count):
    if not isinstance(count, int) or count < 1:
        return []
    covered = set()
    for start, end in ranges or []:
        covered.update(range(start, end + 1))
    return [volume for volume in range(1, count + 1) if volume not in covered]


class ProductPageParser(HTMLParser):
    """Capture only structured product metadata; do not guess prices from prose."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.json_ld = []
        self.meta = {}
        self.title_chunks = []
        self._in_json_ld = False
        self._script_chunks = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = {str(key).lower(): value for key, value in attrs if key}
        tag = tag.lower()
        if tag == "script" and "ld+json" in str(attrs.get("type", "")).lower():
            self._in_json_ld = True
            self._script_chunks = []
        elif tag == "meta":
            key = attrs.get("itemprop") or attrs.get("property") or attrs.get("name")
            content = attrs.get("content")
            if key and content is not None:
                self.meta[str(key).lower()] = str(content).strip()
        elif tag == "title":
            self._in_title = True

    def handle_data(self, data):
        if self._in_json_ld:
            self._script_chunks.append(data)
        if self._in_title:
            self.title_chunks.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "script" and self._in_json_ld:
            raw = "".join(self._script_chunks).strip()
            if raw:
                self.json_ld.append(raw)
            self._in_json_ld = False
            self._script_chunks = []
        elif tag == "title":
            self._in_title = False


def _type_names(value):
    if isinstance(value, str):
        return {value.lower()}
    if isinstance(value, list):
        return {str(item).lower() for item in value}
    return set()


def _walk_offer_objects(value, offers, products):
    if isinstance(value, dict):
        types = _type_names(value.get("@type"))
        if "product" in types or "productgroup" in types:
            products.append(value)
        if "offer" in types or "aggregateoffer" in types:
            offers.append(value)
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                _walk_offer_objects(nested, offers, products)
    elif isinstance(value, list):
        for nested in value:
            _walk_offer_objects(nested, offers, products)


def _shipping_from_offer(offer):
    details = offer.get("shippingDetails")
    if isinstance(details, list):
        details = details[0] if len(details) == 1 else None
    if not isinstance(details, dict):
        return None
    rate = details.get("shippingRate")
    if not isinstance(rate, dict):
        return None
    if str(rate.get("currency") or "").upper() not in {"JPY", "YEN"}:
        return None
    return as_nonnegative_int(rate.get("value"))


def _availability(value):
    value = str(value or "").lower()
    if "instock" in value or "limitedavailability" in value:
        return "IN_STOCK"
    if "outofstock" in value or "soldout" in value or "discontinued" in value:
        return "OUT_OF_STOCK"
    if "preorder" in value or "presale" in value:
        return "PREORDER"
    return "UNKNOWN"


def extract_product_data(html, url):
    parser = ProductPageParser()
    try:
        parser.feed(html)
    except Exception:
        pass

    offers = []
    products = []
    for raw in parser.json_ld:
        try:
            structured = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        _walk_offer_objects(structured, offers, products)

    title = next((str(product.get("name")).strip() for product in products if product.get("name")), "")
    if not title:
        title = "".join(parser.title_chunks).strip() or parser.meta.get("og:title", "")

    offer_values = []
    for offer in offers:
        currency = str(offer.get("priceCurrency") or "").upper()
        types = _type_names(offer.get("@type"))
        # AggregateOffer.lowPrice is a price-range floor, not proof of the selected item's
        # checkout price. Never rank it as a direct offer; only explicit Offer.price qualifies.
        if "aggregateoffer" in types:
            continue
        raw_price = offer.get("price")
        price = as_positive_int(raw_price)
        if price is None:
            continue
        avail = _availability(offer.get("availability"))
        shipping = _shipping_from_offer(offer)
        seller = offer.get("seller")
        if isinstance(seller, dict):
            seller = seller.get("name")
        offer_values.append({
            "price_jpy": price if currency in {"JPY", "YEN"} else None,
            "currency": currency or None,
            "availability": avail,
            "shipping_jpy": shipping,
            "seller": str(seller).strip() if seller else None,
            "raw_price": str(raw_price),
        })

    method = "JSON_LD" if offer_values else None
    if not offer_values:
        meta_price = parser.meta.get("price") or parser.meta.get("product:price:amount")
        currency = (parser.meta.get("pricecurrency")
                    or parser.meta.get("product:price:currency") or "").upper()
        price = as_positive_int(meta_price)
        if price is not None and currency:
            offer_values = [{
                "price_jpy": price if currency in {"JPY", "YEN"} else None,
                "currency": currency,
                "availability": _availability(parser.meta.get("itemprop:availability")),
                "shipping_jpy": None,
                "seller": None,
                "raw_price": str(meta_price),
            }]
            method = "META_PRODUCT"

    unique = {(x.get("price_jpy"), x.get("currency"), x.get("availability"), x.get("shipping_jpy")) for x in offer_values}
    if not offer_values:
        status = "PRICE_NOT_FOUND"
        price = None
        currency = None
        stock = "UNKNOWN"
        shipping = None
        seller = None
    elif len(unique) != 1:
        status = "AMBIGUOUS_PRICE"
        price = None
        currency = None
        stock = "UNKNOWN"
        shipping = None
        seller = None
    else:
        item = offer_values[0]
        currency = item.get("currency")
        price = item.get("price_jpy")
        stock = item.get("availability") or "UNKNOWN"
        shipping = item.get("shipping_jpy")
        seller = item.get("seller")
        if currency not in {"JPY", "YEN"}:
            status = "NON_JPY_CURRENCY"
            price = None
        elif price is None:
            status = "INVALID_PRICE"
        else:
            status = "OK"

    return {
        "source_url": url,
        "title": title or None,
        "price_jpy": price,
        "currency": currency,
        "availability": stock,
        "shipping_jpy_from_page": shipping,
        "seller_from_page": seller,
        "parse_method": method,
        "parse_status": status,
    }


def listing_decision(listing, observation, manga, condition_policy, now=None,
                     max_price_age_hours=24, max_cost_age_days=7):
    """Return eligibility, final known total and explicit reasons; never fill unknowns with zero."""
    now = parse_dt(now) if now is not None else now_jst()
    reasons = []
    observation = observation or {}
    if not valid_https_url(listing.get("listing_url")):
        reasons.append("INVALID_PRODUCT_URL")
    if observation.get("retrieval_status") != "OK":
        reasons.append("RETRIEVAL_NOT_OK")
    if observation.get("currency") not in {"JPY", "YEN"}:
        reasons.append("PRICE_CURRENCY_NOT_CONFIRMED_JPY")
    price = as_positive_int(observation.get("price_jpy"))
    if price is None:
        reasons.append("PRICE_UNKNOWN")
    retrieved_at = observation.get("retrieved_at")
    price_age = age_hours(retrieved_at, now)
    if price_age is None:
        reasons.append("PRICE_TIMESTAMP_UNKNOWN")
    elif price_age < -0.05 or price_age > max_price_age_hours:
        reasons.append("PRICE_STALE")
    if observation.get("availability") != "IN_STOCK":
        reasons.append("STOCK_NOT_CONFIRMED_IN_STOCK")

    if str(listing.get("title_match") or "").upper() != "EXACT":
        reasons.append("TITLE_MATCH_NOT_CONFIRMED")
    if str(listing.get("edition_match") or "").upper() != "EXACT":
        reasons.append("EDITION_MATCH_NOT_CONFIRMED")

    condition = str(listing.get("condition") or "UNKNOWN").upper()
    accepted = set(condition_policy.get("accepted_conditions", ACCEPTED_CONDITIONS))
    if condition not in accepted:
        reasons.append("CONDITION_NOT_ACCEPTABLE_OR_UNKNOWN")
    flags = {str(flag).upper() for flag in (listing.get("damage_flags") or [])}
    blockers = set(condition_policy.get("blocking_damage_flags", BLOCKING_DAMAGE_FLAGS))
    if flags & blockers:
        reasons.append("BLOCKING_DAMAGE_FLAG:" + ",".join(sorted(flags & blockers)))

    count, count_confirmed = expected_count(manga, allow_provisional=True)
    ranges = normalized_ranges(listing.get("included_ranges"))
    if ranges is None:
        reasons.append("VOLUME_COVERAGE_UNKNOWN_OR_INVALID")
    elif count is None:
        reasons.append("VOLUME_COUNT_UNKNOWN")
    elif not ranges_cover_all(ranges, count):
        reasons.append("NOT_ALL_REQUIRED_VOLUMES_COVERED")
    if not count_confirmed:
        reasons.append("VOLUME_COUNT_NOT_SOURCE_CONFIRMED")

    shipping = as_nonnegative_int(listing.get("shipping_jpy"))
    fees = as_nonnegative_int(listing.get("mandatory_fees_jpy"))
    if shipping is None:
        reasons.append("SHIPPING_UNKNOWN")
    if fees is None:
        reasons.append("MANDATORY_FEES_UNKNOWN")
    costs_checked_at = listing.get("costs_checked_at")
    costs_age = age_hours(costs_checked_at, now)
    if costs_age is None:
        reasons.append("SHIPPING_FEE_CHECK_TIMESTAMP_UNKNOWN")
    elif costs_age < -0.05 or costs_age > max_cost_age_days * 24:
        reasons.append("SHIPPING_OR_FEE_CHECK_STALE")

    total = None
    cost_fields_ok = shipping is not None and fees is not None and costs_age is not None and 0 <= costs_age <= max_cost_age_days * 24
    price_fields_ok = (
        observation.get("retrieval_status") == "OK"
        and observation.get("currency") in {"JPY", "YEN"}
        and price is not None
        and price_age is not None
        and 0 <= price_age <= max_price_age_hours
    )
    core_ok = (
        price_fields_ok
        and observation.get("availability") == "IN_STOCK"
        and valid_https_url(listing.get("listing_url"))
        and str(listing.get("title_match") or "").upper() == "EXACT"
        and str(listing.get("edition_match") or "").upper() == "EXACT"
        and condition in accepted
        and not bool(flags & blockers)
        and ranges is not None
        and count is not None
        and ranges_cover_all(ranges, count)
        and cost_fields_ok
    )
    if price is not None and shipping is not None and fees is not None:
        total = price + shipping + fees

    if not count_confirmed:
        status = "PROVISIONAL" if core_ok else "NOT_ELIGIBLE"
    elif core_ok:
        status = "CONFIRMED"
    else:
        status = "NOT_ELIGIBLE"

    return {
        "listing_id": listing.get("id"),
        "manga_id": manga.get("id"),
        "title": manga.get("title"),
        "listing_url": listing.get("listing_url"),
        "seller": listing.get("seller") or observation.get("seller_from_page"),
        "price_jpy": price,
        "shipping_jpy": shipping,
        "mandatory_fees_jpy": fees,
        "total_jpy": total,
        "currency": observation.get("currency"),
        "condition": condition,
        "damage_flags": sorted(flags),
        "availability": observation.get("availability") or "UNKNOWN",
        "retrieved_at": retrieved_at,
        "costs_checked_at": costs_checked_at,
        "volume_count": count,
        "volume_count_confirmed": count_confirmed,
        "included_ranges": ranges,
        "status": status,
        "reasons": list(dict.fromkeys(reasons)),
        "quality_note": "レンタル落ち" if condition == "RENTAL_USED" else None,
    }


def minimum_cost_cover(candidates, count):
    """Cheapest coverage of a sequential volume range using only confirmed candidates.

    Candidate entries must include total_jpy, included_ranges (one contiguous range),
    and listing_url. Cost is minimised first; overlapping duplicate volumes break ties.
    """
    if not isinstance(count, int) or count < 1:
        return None
    usable = []
    for candidate in candidates:
        ranges = candidate.get("included_ranges")
        if candidate.get("status") != "CONFIRMED" or not isinstance(candidate.get("total_jpy"), int):
            continue
        if not ranges or len(ranges) != 1:
            continue
        start, end = ranges[0]
        if 1 <= start <= end <= count:
            usable.append(candidate)

    # State value: (cash total, duplicate-volume count, picked candidate indices).
    dp = {0: (0, 0, [])}
    for covered in range(count):
        state = dp.get(covered)
        if state is None:
            continue
        for index, candidate in enumerate(usable):
            start, end = candidate["included_ranges"][0]
            if start > covered + 1 or end <= covered:
                continue
            next_covered = min(count, end)
            if next_covered <= covered:
                continue
            duplicates = max(0, min(covered, end) - start + 1)
            proposal = (state[0] + candidate["total_jpy"], state[1] + duplicates, state[2] + [index])
            previous = dp.get(next_covered)
            if previous is None or proposal[:2] < previous[:2]:
                dp[next_covered] = proposal
    final = dp.get(count)
    if final is None:
        return None
    chosen = [usable[index] for index in final[2]]
    return {
        "total_jpy": final[0],
        "duplicate_volume_count": final[1],
        "listings": chosen,
        "status": "CONFIRMED_COMBINATION" if final[1] == 0 else "CONFIRMED_WITH_DUPLICATES",
    }


def safe_markdown(text):
    text = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    return text.replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")
