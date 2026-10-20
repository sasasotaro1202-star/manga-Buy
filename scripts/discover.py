"""Discover candidate product URLs through a public search page.

Search results are leads only. No price is taken from snippets and discovery does not
prove listing identity, coverage, condition, shipping, stock or purchase eligibility.
"""
from __future__ import annotations

import argparse
import base64
import json
import time
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

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
    """Unwrap common public search redirects; marketplace allowlisting still happens later."""
    url = str(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = urljoin("https://html.duckduckgo.com/html/", url)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if "duckduckgo.com" in host:
        target = parse_qs(parsed.query).get("uddg")
        if target and target[0].startswith("https://"):
            return target[0]

    if host == "bing.com" or host.endswith(".bing.com"):
        params = parse_qs(parsed.query)
        for key in ("u", "url"):
            for value in params.get(key, []):
                candidate = value
                if key == "u" and candidate.startswith("a1"):
                    token = candidate[2:]
                    try:
                        token += "=" * ((4 - len(token) % 4) % 4)
                        decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8", errors="strict")
                        if decoded.startswith("https://"):
                            return decoded
                    except (ValueError, UnicodeError):
                        pass
                if candidate.startswith("https%3A%2F%2F"):
                    candidate = candidate.replace("%3A", ":").replace("%2F", "/")
                if candidate.startswith("https://"):
                    return candidate

    return url if url.startswith("https://") else ""



def parse_bing_rss(raw):
    """Parse a public RSS response into link/title leads without using snippets as prices."""
    root = ET.fromstring(raw)
    results = []
    for item in root.iter():
        if str(item.tag).rsplit("}", 1)[-1].lower() != "item":
            continue
        fields = {}
        for child in list(item):
            key = str(child.tag).rsplit("}", 1)[-1].lower()
            if key in {"link", "title"}:
                fields[key] = "".join(child.itertext()).strip()
        if fields.get("link"):
            results.append({"url": fields["link"], "title": fields.get("title", ""), "classes": "bing-rss"})
    return results

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


def search_engine_urls(discovery_config):
    """Use configured public HTML search pages; a second engine is a fallback, not bypass."""
    raw = discovery_config.get("engine_urls")
    if not isinstance(raw, list) or not raw:
        raw = [
            discovery_config.get("engine_url") or "https://html.duckduckgo.com/html/?q={query}",
            "https://www.bing.com/search?q={query}",
        ]
    result = []
    for item in raw:
        template = str(item or "").strip()
        if template.startswith("https://") and "{query}" in template and template not in result:
            result.append(template)
    return result


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
    engine_urls = search_engine_urls(discovery_config)

    for manga in catalog.get("manga", [])[:max_queries]:
        query = (
            str(manga.get("search_title") or manga.get("title") or "")
            + ' 中古 全巻セット (site:jp.mercari.com OR site:shopping.bookoff.co.jp OR '
            + 'site:item.rakuten.co.jp OR site:shopping.yahoo.co.jp OR site:auctions.yahoo.co.jp '
            + 'OR site:suruga-ya.jp OR site:netoff.co.jp OR site:mandarake.co.jp)'
        )
        seen_for_manga = set()
        accepted = 0
        for engine_index, engine_url in enumerate(engine_urls):
            if accepted >= max_per_manga:
                break
            search_url = engine_url.format(query=quote(query, safe=""))
            queries_checked += 1
            try:
                request = Request(search_url, headers={
                    "User-Agent": "manga-Buy/0.1 (+https://github.com/sasasotaro1202-star/manga-Buy)",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.5",
                })
                with urlopen(request, timeout=timeout) as response:
                    raw = response.read(max_bytes + 1)
                    if len(raw) > max_bytes:
                        raise ValueError("SEARCH_RESPONSE_TOO_LARGE")
                    charset = response.headers.get_content_charset() or "utf-8"
                    html = raw.decode(charset, errors="replace")
                is_rss = "format=rss" in search_url.lower()
                if is_rss:
                    # Bing RSS is XML, not HTML. Parse only link/title; snippets never become prices.
                    search_results = parse_bing_rss(raw)
                else:
                    parser = SearchResultsParser()
                    parser.feed(html)
                    search_results = parser.items
                new_from_engine = 0
                rejected_hosts = {}
                rejected_paths = []
                for result in search_results:
                    url = unwrap_search_url(result.get("url", ""))
                    if not valid_https_url(url):
                        rejected_hosts["invalid_or_non_https"] = rejected_hosts.get("invalid_or_non_https", 0) + 1
                        continue
                    if not allowed_host(url, allowed_domains):
                        host = urlparse(url).hostname or "unknown"
                        rejected_hosts[host] = rejected_hosts.get(host, 0) + 1
                        continue
                    if not is_product_candidate(url, allowed_domains):
                        if len(rejected_paths) < 3:
                            rejected_paths.append(url)
                        continue
                    if url in seen_for_manga:
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
                        "discovered_by_engine": urlparse(search_url).hostname,
                        "verification_status": "UNVERIFIED_LEAD",
                        "price_jpy": None,
                        "availability": "UNKNOWN",
                        "volume_coverage": None,
                        "condition": "UNKNOWN",
                        "shipping_jpy": None,
                        "notes": "検索で発見したリンク。販売価格や全巻条件の証拠ではない。",
                    }
                    accepted += 1
                    new_from_engine += 1
                    if accepted >= max_per_manga:
                        break
                lower_html = html.lower()
                diagnostics.append({
                    "manga_id": manga.get("id"),
                    "search_url": search_url,
                    "search_engine": urlparse(search_url).hostname,
                    "response_bytes": len(raw),
                    "anchor_count": len(search_results),
                    "candidate_count": new_from_engine,
                    "fallback_used": engine_index > 0,
                    "rejected_host_counts": rejected_hosts,
                    "rejected_path_samples": rejected_paths,
                    "has_result_marker": bool(new_from_engine) or any(token in lower_html for token in (
                        "result__a", "b_algo", "web-result", "result__body", "<item>"
                    )),
                    "page_appears_blocked": any(token in lower_html for token in (
                        "captcha", "bot detection", "unusual traffic", "verify you are human",
                        "consent required", "enable javascript to continue"
                    )),
                })
            except Exception as exc:
                errors.append({
                    "manga_id": manga.get("id"),
                    "manga_title": manga.get("title"),
                    "search_url": search_url,
                    "search_engine": urlparse(search_url).hostname,
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
        "queries_using_fallback": sum(1 for item in diagnostics if item.get("fallback_used")),
        "queries_finding_candidates": sum(1 for item in diagnostics if item.get("candidate_count", 0) > 0),
        "queries_appearing_blocked": sum(1 for item in diagnostics if item.get("page_appears_blocked")),
        "new_candidate_count": len(set(current_by_url) - set(previous_by_url)),
        "current_seen_count": len(current_by_url),
        "retained_candidate_count": len(candidates),
        "candidates": candidates,
    }
    save_json(OUTPUT_PATH, result)
    print(json.dumps({key: result[key] for key in (
        "generated_at", "discovery_status", "queries_checked", "queries_with_anchors",
        "queries_with_result_marker", "queries_using_fallback", "queries_finding_candidates",
        "queries_appearing_blocked", "new_candidate_count",
        "current_seen_count", "retained_candidate_count"
    )}, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Limit manga queries for a test run")
    args = parser.parse_args()
    run_discovery(args.limit)
