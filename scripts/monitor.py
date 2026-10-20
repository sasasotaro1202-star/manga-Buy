"""Observe registered product pages. Unknown data remains unknown."""
from __future__ import annotations

import json
import time
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from urllib.robotparser import RobotFileParser

from manga_core import ROOT, now_jst, load_json, save_json, allowed_host, valid_https_url, extract_product_data

CATALOG_PATH = ROOT / "config" / "manga_catalog.json"
MARKETPLACES_PATH = ROOT / "config" / "marketplaces.json"
LISTINGS_PATH = ROOT / "data" / "listings.json"
OBSERVATIONS_PATH = ROOT / "data" / "observations.json"
HISTORY_PATH = ROOT / "data" / "price_history.jsonl"


def append_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


class AllowlistedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_domains):
        super().__init__()
        self.allowed_domains = allowed_domains

    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not valid_https_url(newurl) or not allowed_host(newurl, self.allowed_domains):
            raise URLError("Redirect target is not on the HTTPS marketplace allowlist")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def check_robots(url, user_agent, timeout, allowed_domains, robots_cache):
    parsed = urlparse(url)
    origin = f"https://{parsed.netloc}"
    robots_url = origin + "/robots.txt"
    if robots_url not in robots_cache:
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            req = Request(robots_url, headers={"User-Agent": user_agent})
            with urlopen(req, timeout=timeout) as response:
                if response.status >= 400:
                    robots_cache[robots_url] = (False, "ROBOTS_HTTP_STATUS_" + str(response.status))
                else:
                    content = response.read(512_000).decode("utf-8", errors="replace")
                    parser.parse(content.splitlines())
                    robots_cache[robots_url] = (True, parser)
        except HTTPError as exc:
            # A true 404 means the origin publishes no robots policy. Other failures fail closed.
            if exc.code == 404:
                parser.parse([])
                robots_cache[robots_url] = (True, parser)
            else:
                robots_cache[robots_url] = (False, "ROBOTS_HTTP_STATUS_" + str(exc.code))
        except Exception as exc:
            robots_cache[robots_url] = (False, "ROBOTS_UNVERIFIED:" + type(exc).__name__)
    allowed, value = robots_cache[robots_url]
    if not allowed:
        return False, str(value)
    try:
        return bool(value.can_fetch(user_agent, url)), "ROBOTS_DISALLOWED"
    except Exception:
        return False, "ROBOTS_UNVERIFIED"


def fetch_product(url, config, allowed_domains, robots_cache):
    timeout = int(config.get("timeout_seconds", 20))
    max_bytes = int(config.get("max_response_bytes", 3_000_000))
    user_agent = str(config.get("user_agent") or "manga-Buy/0.1")
    if not valid_https_url(url) or not allowed_host(url, allowed_domains):
        return {"retrieval_status": "URL_NOT_ALLOWLISTED", "error": "Only allowlisted HTTPS product pages can be checked."}
    robots_allowed, robots_reason = check_robots(url, user_agent, timeout, allowed_domains, robots_cache)
    if not robots_allowed:
        return {"retrieval_status": "ROBOTS_BLOCKED_OR_UNKNOWN", "error": robots_reason}
    try:
        opener = build_opener(AllowlistedRedirectHandler(allowed_domains))
        request = Request(url, headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja-JP,ja;q=0.9",
        })
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            if not valid_https_url(final_url) or not allowed_host(final_url, allowed_domains):
                return {"retrieval_status": "REDIRECT_NOT_ALLOWLISTED", "error": "Final URL left the HTTPS marketplace allowlist."}
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                return {"retrieval_status": "RESPONSE_TOO_LARGE", "error": "Product page exceeded the configured response limit."}
            charset = response.headers.get_content_charset() or "utf-8"
            html = raw.decode(charset, errors="replace")
        extracted = extract_product_data(html, final_url)
        extracted["retrieval_status"] = "OK" if extracted.get("parse_status") == "OK" else extracted.get("parse_status")
        extracted["final_url"] = final_url
        return extracted
    except HTTPError as exc:
        return {"retrieval_status": "HTTP_ERROR", "error": "HTTP " + str(exc.code)}
    except URLError as exc:
        return {"retrieval_status": "NETWORK_ERROR", "error": str(exc.reason)[:240]}
    except Exception as exc:
        return {"retrieval_status": "FETCH_ERROR", "error": type(exc).__name__ + ": " + str(exc)[:240]}


def run_monitor():
    import hashlib

    marketplace_config = load_json(MARKETPLACES_PATH, {})
    monitor_config = marketplace_config.get("monitor", {})
    allowlist = marketplace_config.get("allowed_domains", [])
    listing_data = load_json(LISTINGS_PATH, {"listings": []})
    discovered_data = load_json(ROOT / "data" / "discovered_candidates.json", {"candidates": []})
    previous = load_json(OBSERVATIONS_PATH, {"observations": {}})
    now = now_jst().isoformat()
    observations = {}
    history_rows = []
    robots_cache = {}
    catalog = load_json(CATALOG_PATH, {"manga": []})
    manga_entries = catalog.get("manga", [])
    manga_by_id = {entry.get("id"): entry for entry in manga_entries}
    url_seen = set()
    watch_entries = []

    for listing in listing_data.get("listings", []):
        if isinstance(listing, dict):
            curated = dict(listing)
            curated["_auto_discovered"] = False
            watch_entries.append(curated)
            if curated.get("listing_url"):
                url_seen.add(str(curated["listing_url"]))

    # Inspect a small, balanced sample of newly found direct product URLs.
    # They are price sightings only; the report never treats them as complete sets.
    max_auto = max(0, int(monitor_config.get("max_auto_watch_candidates", 75)))
    per_manga = max(0, int(monitor_config.get("max_auto_candidates_per_manga", 3)))
    discovery_by_manga = {}
    for candidate in discovered_data.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        manga_id = candidate.get("manga_id")
        url = str(candidate.get("listing_url") or "").strip()
        if manga_id not in manga_by_id or not valid_https_url(url) or url in url_seen:
            continue
        if not allowed_host(url, allowlist):
            continue
        discovery_by_manga.setdefault(manga_id, []).append(candidate)

    auto_entries = []
    for manga in manga_entries:
        candidates = discovery_by_manga.get(manga.get("id"), [])
        candidates.sort(key=lambda item: str(item.get("last_seen_at") or ""), reverse=True)
        for candidate in candidates[:per_manga]:
            url = str(candidate["listing_url"])
            listing_id = "discovered-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            auto_entries.append({
                "id": listing_id,
                "manga_id": candidate.get("manga_id"),
                "listing_url": url,
                "seller": candidate.get("source_domain"),
                "search_result_title": candidate.get("search_result_title"),
                "_auto_discovered": True,
            })
            url_seen.add(url)
            if len(auto_entries) >= max_auto:
                break
        if len(auto_entries) >= max_auto:
            break

    watch_entries.extend(auto_entries)
    request_delay = max(0.0, float(monitor_config.get("request_delay_seconds", 0.8)))

    for index, listing in enumerate(watch_entries):
        listing_id = str(listing.get("id") or "").strip()
        url = str(listing.get("listing_url") or "").strip()
        is_auto = bool(listing.get("_auto_discovered"))
        if not listing_id or not url:
            continue
        if listing.get("manga_id") not in manga_by_id:
            observations[listing_id] = {
                "listing_id": listing_id, "listing_url": url, "retrieved_at": now,
                "retrieval_status": "UNKNOWN_MANGA_ID", "error": "manga_id is not in the curated catalogue."
            }
            continue
        result = fetch_product(url, monitor_config, allowlist, robots_cache)
        prev = (previous.get("observations", {}) or {}).get(listing_id) or {}
        previous_valid_price = (
            prev.get("price_jpy")
            if prev.get("retrieval_status") == "OK" and prev.get("currency") in {"JPY", "YEN"}
            else None
        )
        obs = {
            **result,
            "listing_id": listing_id,
            "manga_id": listing.get("manga_id"),
            "listing_url": url,
            "retrieved_at": now,
            "auto_discovered_candidate": is_auto,
            "search_result_title": listing.get("search_result_title"),
            "previous_valid_price_jpy": previous_valid_price,
        }
        observations[listing_id] = obs
        history_rows.append(obs)
        if request_delay and index < len(watch_entries) - 1:
            time.sleep(request_delay)

    summary = {
        "watched_count": len(observations),
        "auto_discovered_count": sum(bool(item.get("auto_discovered_candidate")) for item in observations.values()),
        "ok_count": sum(item.get("retrieval_status") == "OK" for item in observations.values()),
        "error_count": sum(item.get("retrieval_status") != "OK" for item in observations.values()),
    }
    payload = {
        "schema_version": 1,
        "generated_at": now,
        "observations": observations,
        "summary": summary,
    }
    save_json(OBSERVATIONS_PATH, payload)
    if history_rows:
        append_jsonl(HISTORY_PATH, history_rows)
    print(json.dumps({"generated_at": now, **summary}, ensure_ascii=False))
    return payload


if __name__ == "__main__":
    run_monitor()
