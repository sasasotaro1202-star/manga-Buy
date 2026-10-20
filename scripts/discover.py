"""Discover candidate product URLs through a public search page.

Search results are leads only. No price is taken from snippets and discovery does not
prove listing identity, coverage, condition, shipping, stock or purchase eligibility.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import Request, urlopen

from manga_core import ROOT, now_jst, load_json, save_json, allowed_host, valid_https_url

CATALOG_PATH = ROOT / "config" / "manga_catalog.json"
MARKETPLACES_PATH = ROOT / "config" / "marketplaces.json"
OUTPUT_PATH = ROOT / "data" / "discovered_candidates.json"


class SearchResultsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        data = {key.lower(): value for key, value in attrs if key}
        # Search engines change markup frequently. Collect every anchor and use
        # strict destination host/path allowlists later instead of relying on
        # one CSS class name that can silently break discovery.
        if data.get("href"):
            self._current = {
                "url": data["href"],
                "title": "",
                "classes": str(data.get("class") or ""),
            }

    def handle_data(self, data):
        if self._current is not None:
            self._current["title"] += data

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._current is not None:
            self._current["title"] = self._current["title"].strip()
            self.items.append(self._current)
            self._current = None


def unwrap_search_url(url):
    url = str(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = urljoin("https://html.duckduckgo.com/html/", url)
    parsed = urlparse(url)
    if "duckduckgo.com" in (parsed.hostname or ""):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            # parse_qs already decoded this value once; do not decode it again.
            return target[0]
    return url if url.startswith("https://") else ""


def is_product_candidate(url, allowed_domains):
    if not valid_https_url(url) or not allowed_host(url, allowed_domains):
        return False
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")
    if not path or path == "/":
        return False
    blocked = ("/search", "/search/", "/category", "/categories", "/filter", "/campaign", "/help", "/guide", "/login", "/signup")
    if any(token in path for token in blocked):
        return False
    # Reject known search-result routes, while keeping product/item URLs.
    if parsed.hostname and "mercari.com" in parsed.hostname and path.startswith("/search"):
        return False
    if parsed.hostname and "amazon.co.jp" in parsed.hostname and not any(
        token in path for token in ("/dp/", "/gp/product/", "/gp/aw/d/")
    ):
        return False
    if parsed.hostname and "auctions.yahoo.co.jp" in parsed.hostname and "/jp/auction/" not in path:
        return False
    if parsed.hostname and "item.rakuten.co.jp" in parsed.hostname and len(path.split("/")) < 3:
        return False
    return True


def run_discovery(limit=None):
    catalog = load_json(CATALOG_PATH, {"manga": []})
    marketplace_config = load_json(MARKETPLACES_PATH, {})
    discovery_config = marketplace_config.get("discovery", {})
    allowed_domains = marketplace_config.get("allowed_domains", [])
    timeout = int(discovery_config.get("timeout_seconds", 20))
    max_bytes = int(discovery_config.get("max_response_bytes", 2_000_000))
    max_per_manga = int(discovery_config.get("max_candidates_per_manga", 12))
    delay = float(discovery_config.get("delay_seconds", 2))
    max_queries = min(int(discovery_config.get("max_queries_per_run", 25)), limit or 25)

    old_data = load_json(OUTPUT_PATH, {"candidates": []})
    old_candidates = old_data.get("candidates", [])
    previous_by_url = {
        item.get("listing_url"): item for item in old_candidates
        if isinstance(item, dict) and item.get("listing_url")
    }
    current_by_url = {}
    errors = []
    diagnostics = []
    queries_checked = 0
    now = now_jst().isoformat()
    query_base = str(discovery_config.get("engine_url") or "https://html.duckduckgo.com/html/?q={query}")

    for manga in catalog.get("manga", [])[:max_queries]:
        query = str(manga.get("search_title") or manga.get("title") or "") + " 中古 全巻セット"
        search_url = query_base.format(query=quote(query))
        queries_checked += 1
        try:
            request = Request(search_url, headers={
                "User-Agent": "manga-Buy/0.1 (+https://github.com/sasasotaro1202-star/manga-Buy)",
                "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.5",
            })
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise ValueError("SEARCH_RESPONSE_TOO_LARGE")
                charset = response.headers.get_content_charset() or "utf-8"
                html = raw.decode(charset, errors="replace")
            parser = SearchResultsParser()
            parser.feed(html)
            diagnostics.append({
                "manga_id": manga.get("id"),
                "search_url": search_url,
                "response_bytes": len(raw),
                "anchor_count": len(parser.items),
                "has_result_marker": "result__a" in html or "result__body" in html,
                "page_appears_blocked": any(token in html.lower() for token in (
                    "captcha", "bot detection", "unusual traffic", "verify you are human"
                )),
            })
            accepted = 0
            seen_for_manga = set()
            for result in parser.items:
                url = unwrap_search_url(result.get("url", ""))
                if not is_product_candidate(url, allowed_domains) or url in seen_for_manga:
                    continue
                seen_for_manga.add(url)
                old = previous_by_url.get(url, {})
                current_by_url[url] = {
                    "manga_id": manga.get("id"),
                    "manga_title": manga.get("title"),
                    "listing_url": url,
                    "search_result_title": result.get("title"),
                    "source_domain": urlparse(url).hostname,
                    "first_seen_at": old.get("first_seen_at") or now,
                    "last_seen_at": now,
                    "discovered_by_query": query,
                    "verification_status": "UNVERIFIED_LEAD",
                    "price_jpy": None,
                    "availability": "UNKNOWN",
                    "volume_coverage": None,
                    "condition": "UNKNOWN",
                    "shipping_jpy": None,
                    "notes": "検索で発見したリンク。販売価格や全巻条件の証拠ではない。",
                }
                accepted += 1
                if accepted >= max_per_manga:
                    break
        except Exception as exc:
            errors.append({
                "manga_id": manga.get("id"),
                "manga_title": manga.get("title"),
                "search_url": search_url,
                "error": type(exc).__name__ + ": " + str(exc)[:240],
                "checked_at": now,
            })
        finally:
            time.sleep(delay)

    # Keep earlier discoveries so transient search outages don't erase useful leads.
    merged = dict(previous_by_url)
    merged.update(current_by_url)
    candidates = list(merged.values())
    candidates.sort(key=lambda item: (item.get("manga_id") or "", item.get("listing_url") or ""))
    status = (
        "OK" if current_by_url and not errors
        else "PARTIAL" if current_by_url
        else "FAILED" if errors
        else "EMPTY"
    )
    result = {
        "schema_version": 1,
        "generated_at": now,
        "discovery_status": status,
        "queries_checked": queries_checked,
        "query_errors": errors,
        "query_diagnostics": diagnostics,
        "queries_with_anchors": sum(1 for item in diagnostics if item.get("anchor_count")),
        "queries_with_result_marker": sum(1 for item in diagnostics if item.get("has_result_marker")),
        "queries_appearing_blocked": sum(1 for item in diagnostics if item.get("page_appears_blocked")),
        "new_candidate_count": len(set(current_by_url) - set(previous_by_url)),
        "current_seen_count": len(current_by_url),
        "retained_candidate_count": len(candidates),
        "candidates": candidates,
    }
    save_json(OUTPUT_PATH, result)
    print(json.dumps({key: result[key] for key in (
        "generated_at", "discovery_status", "queries_checked", "queries_with_anchors",
        "queries_with_result_marker", "queries_appearing_blocked", "new_candidate_count",
        "current_seen_count", "retained_candidate_count"
    )}, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Limit manga queries for a test run")
    args = parser.parse_args()
    run_discovery(args.limit)
