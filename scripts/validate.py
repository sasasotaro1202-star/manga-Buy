"""Validate catalogue integrity and the no-false-cheapest purchase contract."""
from __future__ import annotations

import json
import sys

from manga_core import ROOT, load_json, valid_https_url, expected_count, normalized_ranges

CATALOG_PATH = ROOT / "config" / "manga_catalog.json"
MARKETPLACES_PATH = ROOT / "config" / "marketplaces.json"
CONDITION_PATH = ROOT / "config" / "condition_policy.json"
LISTINGS_PATH = ROOT / "data" / "listings.json"


def main():
    errors = []
    catalog = load_json(CATALOG_PATH, {})
    marketplace_config = load_json(MARKETPLACES_PATH, {})
    policy = load_json(CONDITION_PATH, {})
    listing_data = load_json(LISTINGS_PATH, {})
    manga = catalog.get("manga", [])
    ids = [item.get("id") for item in manga]
    if len(manga) != 25:
        errors.append(f"Expected exactly 25 user-selected work groups; found {len(manga)}.")
    if len(set(ids)) != len(ids):
        errors.append("Catalogue manga IDs must be unique.")
    if any(not value for value in ids):
        errors.append("Every manga entry needs a stable ID.")
    first_count = sum(item.get("candidate_group") == "first" for item in manga)
    second_count = sum(item.get("candidate_group") == "second" for item in manga)
    if first_count != 9 or second_count != 16:
        errors.append(f"Candidate groups should be 9/16; found {first_count}/{second_count}.")

    allowed_statuses = {"OFFICIAL_CONFIRMED", "CURATED_BASELINE", "NEEDS_REVIEW"}
    for item in manga:
        count = item.get("expected_volume_count")
        provisional = item.get("provisional_volume_count")
        status = item.get("volume_count_status")
        if status not in allowed_statuses:
            errors.append(f"{item.get('id')}: invalid volume_count_status {status!r}.")
        if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 1):
            errors.append(f"{item.get('id')}: expected_volume_count must be a positive integer or null.")
        if provisional is not None and (not isinstance(provisional, int) or isinstance(provisional, bool) or provisional < 1):
            errors.append(f"{item.get('id')}: provisional_volume_count must be a positive integer or null.")
        if status == "OFFICIAL_CONFIRMED":
            if count is None or not valid_https_url(item.get("volume_count_source_url")):
                errors.append(f"{item.get('id')}: official-confirmed volume count needs count and HTTPS source URL.")
        groups = item.get("volume_groups")
        if not isinstance(groups, list) or not groups:
            errors.append(f"{item.get('id')}: volume_groups must not be empty.")
        else:
            seen_seq = set()
            for group in groups:
                start, end, group_count = group.get("seq_start"), group.get("seq_end"), group.get("count")
                if not all(isinstance(v, int) for v in (start, end, group_count)) or start < 1 or end < start or end - start + 1 != group_count:
                    errors.append(f"{item.get('id')}: malformed volume group {group!r}.")
                    continue
                for volume in range(start, end + 1):
                    if volume in seen_seq:
                        errors.append(f"{item.get('id')}: overlapping sequential volume map at {volume}.")
                    seen_seq.add(volume)
            if count is not None and len(seen_seq) != count:
                errors.append(f"{item.get('id')}: mapped volume total {len(seen_seq)} differs from expected count {count}.")

    allowed_domains = marketplace_config.get("allowed_domains", [])
    if not allowed_domains:
        errors.append("Marketplace allowlist is empty.")
    discovery_config = marketplace_config.get("discovery", {})
    search_engines = discovery_config.get("engine_urls", [])
    search_targets = discovery_config.get("search_targets", [])
    if not search_engines:
        errors.append("Discovery needs at least one configured search engine.")
    for engine_url in search_engines:
        if not str(engine_url).startswith("https://") or "{query}" not in str(engine_url):
            errors.append("Every discovery engine URL must be HTTPS and contain {query}.")
    target_ids = [target.get("id") for target in search_targets if isinstance(target, dict)]
    if not search_targets:
        errors.append("Discovery needs marketplace-specific search targets.")
    if len(target_ids) != len(set(target_ids)):
        errors.append("Discovery target IDs must be unique.")
    for target in search_targets:
        if not isinstance(target, dict):
            errors.append("Every discovery target must be an object.")
            continue
        domain = str(target.get("domain") or "").lower()
        template = str(target.get("query_template") or "")
        if not domain or not any(domain == item or domain.endswith("." + item) or item.endswith("." + domain) for item in allowed_domains):
            errors.append(f"Discovery target {target.get('id')!r} domain is not allowlisted.")
        if "{title}" not in template or not template.startswith("site:"):
            errors.append(f"Discovery target {target.get('id')!r} needs a title and site-scoped query template.")
        if " OR " in template:
            errors.append(f"Discovery target {target.get('id')!r} must not use an ambiguous OR mega-query.")
        direct_url = str(target.get("search_page_template") or "")
        if not direct_url.startswith("https://") or "{query}" not in direct_url:
            errors.append(f"Discovery target {target.get('id')!r} needs an HTTPS direct-search URL template.")

    for site in marketplace_config.get("marketplaces", []):
        if site.get("enabled") and "{query}" not in str(site.get("search_template") or ""):
            errors.append(f"Marketplace {site.get('id')} needs a search template containing {{query}}.")
        if site.get("enabled") and not str(site.get("search_template") or "").startswith("https://"):
            errors.append(f"Marketplace {site.get('id')} must use HTTPS.")

    accepted = set(policy.get("accepted_conditions", []))
    if "UNKNOWN" in accepted or "POOR" in accepted:
        errors.append("UNKNOWN/POOR condition must not be allowed into confirmed rankings.")
    if not policy.get("blocking_damage_flags"):
        errors.append("Blocking damage policy must not be empty.")

    seen_listing_ids = set()
    for listing in listing_data.get("listings", []):
        lid = listing.get("id")
        if not lid or lid in seen_listing_ids:
            errors.append(f"Missing or duplicate listing ID: {lid!r}.")
        seen_listing_ids.add(lid)
        if listing.get("manga_id") not in set(ids):
            errors.append(f"Listing {lid!r} refers to unknown manga_id.")
        if not valid_https_url(listing.get("listing_url")):
            errors.append(f"Listing {lid!r} needs a real HTTPS product URL.")
        if normalized_ranges(listing.get("included_ranges")) is None:
            errors.append(f"Listing {lid!r} needs valid included_ranges.")
        if listing.get("shipping_jpy") is not None and (not isinstance(listing.get("shipping_jpy"), int) or listing["shipping_jpy"] < 0):
            errors.append(f"Listing {lid!r} has invalid shipping_jpy.")
        if listing.get("mandatory_fees_jpy") is not None and (not isinstance(listing.get("mandatory_fees_jpy"), int) or listing["mandatory_fees_jpy"] < 0):
            errors.append(f"Listing {lid!r} has invalid mandatory_fees_jpy.")

    review_needed = [item for item in manga if item.get("volume_count_status") == "NEEDS_REVIEW"]
    if errors:
        print("VALIDATION_FAIL")
        for error in errors:
            print("- " + error)
        return 1
    print("VALIDATION_OK")
    print(f"Works={len(manga)} First={first_count} Second={second_count}")
    print(f"RegisteredListings={len(listing_data.get('listings', []))}")
    print(f"VolumeCountNeedsReview={len(review_needed)}")
    if review_needed:
        print("Review required before all-volume claims for: " + ", ".join(item["id"] for item in review_needed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
