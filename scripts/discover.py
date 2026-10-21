"""Discover candidate product URLs through a public search page.

Search results are leads only. No price is taken from snippets and discovery does not
prove listing identity, coverage, condition, shipping, stock or purchase eligibility.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import time
import urllib.robotparser
from urllib.error import HTTPError
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
    """Extract result anchors only; exclude search-page navigation and related links."""
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self._current = None
        self._stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        data = {key.lower(): value for key, value in attrs if key}
        classes = set(str(data.get("class") or "").split())
        in_b_algo = any(
            stack_tag in {"li", "div"} and "b_algo" in stack_classes
            for stack_tag, stack_classes in self._stack
        )
        in_result_heading = in_b_algo and any(stack_tag == "h2" for stack_tag, _ in self._stack)
        # DuckDuckGo's result class is stable in its HTML endpoint; Bing uses
        # the h2 anchor within a b_algo list item. Generic page links are ignored.
        is_result = (
            tag == "a" and (
                bool(classes.intersection({"result__a", "result-link", "web-result-link"}))
                or in_result_heading
            )
        )
        if is_result and data.get("href"):
            self._current = {
                "url": data["href"],
                "title": "",
                "classes": str(data.get("class") or ""),
            }
        if tag not in self.VOID_TAGS:
            self._stack.append((tag, classes))

    def handle_data(self, data):
        if self._current is not None:
            self._current["title"] += data

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "a" and self._current is not None:
            self._current["title"] = self._current["title"].strip()
            self.items.append(self._current)
            self._current = None
        # Recover gracefully from imperfect search HTML by dropping the matching
        # closing element and any malformed descendants above it.
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


class MarketplaceResultsParser(HTMLParser):
    """Extract anchor destinations from a marketplace's own result page."""
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        data = {key.lower(): value for key, value in attrs if key}
        href = data.get("href")
        if href:
            self._current = {"url": href, "title": "", "classes": str(data.get("class") or "")}

    def handle_data(self, data):
        if self._current is not None:
            self._current["title"] += data

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._current is not None:
            self._current["title"] = self._current["title"].strip()
            self.items.append(self._current)
            self._current = None

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


def robots_can_fetch(url, user_agent, timeout, cache):
    """Fail closed on unknown robots policy; allow only a true robots.txt 404."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False, "INVALID_ORIGIN"
    robots_url = parsed.scheme + "://" + parsed.netloc + "/robots.txt"
    if robots_url not in cache:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            request = Request(robots_url, headers={"User-Agent": user_agent})
            with urlopen(request, timeout=timeout) as response:
                text = response.read(512_000).decode("utf-8", errors="replace")
            parser.parse(text.splitlines())
            cache[robots_url] = (True, parser, "ROBOTS_CHECKED")
        except HTTPError as exc:
            if exc.code == 404:
                parser.parse([])
                cache[robots_url] = (True, parser, "ROBOTS_404")
            else:
                cache[robots_url] = (False, None, "ROBOTS_HTTP_" + str(exc.code))
        except Exception as exc:
            cache[robots_url] = (False, None, "ROBOTS_UNVERIFIED:" + type(exc).__name__)
    allowed, parser, reason = cache[robots_url]
    if not allowed:
        return False, reason
    try:
        return bool(parser.can_fetch(user_agent, url)), reason if parser.can_fetch(user_agent, url) else "ROBOTS_DISALLOWED"
    except Exception:
        return False, "ROBOTS_UNVERIFIED"


def unwrap_search_url(url, base_url=None):
    """Unwrap public-search redirects; resolve relative links against the engine that returned them."""
    url = str(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        # Bing HTML result links are commonly relative /ck/a redirects. Resolving
        # them against DuckDuckGo causes every result to be misclassified as a DDG URL.
        url = urljoin(base_url or "https://html.duckduckgo.com/html/", url)
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
    """Accept only recognizable item-detail routes, never generic search/navigation pages."""
    if not valid_https_url(url) or not allowed_host(url, allowed_domains):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower().rstrip("/")
    if not path or path == "/":
        return False

    # Mercari: a concrete item ID is mandatory; /shops, /todos and other navigation
    # routes are not listings even though they are HTTPS pages on an allowed host.
    if host == "jp.mercari.com":
        return bool(
            re.fullmatch(r"/item/m[a-z0-9]{3,}", path)
            or re.fullmatch(r"/shops/product/[a-z0-9]+", path)
        )

    # Amazon product detail pages only; reject /s search pages and all generic routes.
    if host == "www.amazon.co.jp" or host == "amazon.co.jp":
        return bool(
            re.fullmatch(r"/dp/[a-z0-9]{8,}", path)
            or re.fullmatch(r"/gp/product/[a-z0-9]{8,}", path)
            or re.fullmatch(r"/gp/aw/d/[a-z0-9]{8,}", path)
        )

    # BOOKOFF item-detail URLs are under /used/ with a numeric item code. Avoid
    # broad routes such as /book, /cart or category/navigation pages.
    if host == "shopping.bookoff.co.jp":
        if not path.startswith("/used/"):
            return False
        last_segment = path.split("/")[-1]
        return len(last_segment) >= 6 and any(char.isdigit() for char in last_segment)

    if host == "auctions.yahoo.co.jp":
        return bool(re.fullmatch(r"/jp/auction/[a-z0-9]+", path))

    # A Rakuten result is a product only on item.rakuten.co.jp, not its search host.
    if host == "search.rakuten.co.jp":
        return False
    if host == "item.rakuten.co.jp":
        parts = [part for part in path.split("/") if part]
        return len(parts) >= 2

    # Yahoo! Shopping item pages use a store host and an HTML item slug.
    if host == "shopping.yahoo.co.jp":
        return False
    if host.endswith(".shopping.yahoo.co.jp"):
        parts = [part for part in path.split("/") if part]
        return len(parts) == 2 and parts[-1].endswith(".html")

    # The current crawler has explicit acceptance rules for its configured targets.
    # Unknown allowlisted hosts fail closed until a product URL pattern is tested.
    return False

def search_engine_urls(discovery_config):
    """Use configured public HTML search pages; a second engine is a fallback, not bypass."""
    raw = discovery_config.get("engine_urls")
    if not isinstance(raw, list) or not raw:
        raw = [
            discovery_config.get("engine_url") or "https://html.duckduckgo.com/html/?q={query}",
            "https://www.bing.com/search?q={query}",
            "https://www.bing.com/search?format=rss&q={query}",
        ]
    result = []
    for item in raw:
        template = str(item or "").strip()
        if template.startswith("https://") and "{query}" in template and template not in result:
            result.append(template)
    return result



def search_queries_for_manga(manga, discovery_config):
    """Build independent marketplace-specific searches plus direct marketplace URLs."""
    title = str(manga.get("search_title") or manga.get("title") or "").replace('"', " ").strip()
    targets = discovery_config.get("search_targets")
    if not isinstance(targets, list) or not targets:
        return [{
            "id": "fallback-broad",
            "domain": None,
            "query": '"' + title + '" 漫画 中古 全巻セット',
            "search_page_template": None,
            "search_term_template": "{title}",
        }]
    queries = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        template = str(target.get("query_template") or "").strip()
        target_id = str(target.get("id") or "").strip()
        domain = str(target.get("domain") or "").strip().lower()
        search_page_template = str(target.get("search_page_template") or "").strip() or None
        search_term_template = str(target.get("search_term_template") or "{title}")
        if not template or not target_id or not domain:
            continue
        try:
            query = template.format(title=title)
        except (KeyError, ValueError):
            continue
        queries.append({
            "id": target_id,
            "domain": domain,
            "query": query,
            "search_page_template": search_page_template,
            "search_term_template": search_term_template,
        })
    return queries

def run_discovery(limit=None):
    catalog = load_json(CATALOG_PATH, {"manga": []})
    marketplace_config = load_json(MARKETPLACES_PATH, {})
    discovery_config = marketplace_config.get("discovery", {})
    allowed_domains = marketplace_config.get("allowed_domains", [])
    timeout = int(discovery_config.get("timeout_seconds", 3))
    max_bytes = int(discovery_config.get("max_response_bytes", 2_000_000))
    max_per_manga = max(1, int(discovery_config.get("max_candidates_per_manga", 8)))
    max_per_target = max(1, int(discovery_config.get("max_candidates_per_target", 2)))
    delay = max(0.0, min(float(discovery_config.get("delay_seconds", 0.15)), 30.0))
    max_queries = min(int(discovery_config.get("max_queries_per_run", 25)), limit or 25)
    user_agent = str((marketplace_config.get("monitor") or {}).get(
        "user_agent", "manga-Buy/0.1 (+https://github.com/sasasotaro1202-star/manga-Buy)"
    ))

    old_data = load_json(OUTPUT_PATH, {"candidates": []})
    old_candidates = old_data.get("candidates", [])
    previous_by_url = {
        item.get("listing_url"): item for item in old_candidates
        if isinstance(item, dict) and item.get("listing_url")
    }

    # Seed actual product URLs as unverified leads. Never import search-result prices
    # or stock claims; only the repository's own product-page monitor can observe them.
    seed_data = load_json(ROOT / "config" / "manual_candidate_seeds.json", {"candidates": []})
    catalog_ids = {str(item.get("id")) for item in catalog.get("manga", []) if item.get("id")}
    manual_seed_count = 0
    for seed in seed_data.get("candidates", []):
        if not isinstance(seed, dict):
            continue
        seed_url = str(seed.get("listing_url") or "").strip()
        seed_manga_id = str(seed.get("manga_id") or "")
        if seed_manga_id not in catalog_ids or not valid_https_url(seed_url) or not allowed_host(seed_url, allowed_domains):
            continue
        normalized_seed = dict(seed)
        normalized_seed.update({
            "verification_status": "UNVERIFIED_SEED",
            "price_jpy": None,
            "availability": "UNKNOWN",
            "condition": "UNKNOWN",
            "shipping_jpy": None,
            "volume_coverage": None,
        })
        previous_by_url.setdefault(seed_url, normalized_seed)
        manual_seed_count += 1
    current_by_url = {}
    errors = []
    diagnostics = []
    queries_checked = 0
    now = now_jst().isoformat()
    engine_urls = search_engine_urls(discovery_config)
    robots_cache = {}

    for manga in catalog.get("manga", [])[:max_queries]:
        manga_id = manga.get("id")
        query_specs = search_queries_for_manga(manga, discovery_config)
        accepted_for_manga = 0
        seen_for_manga = set()

        for query_spec in query_specs:
            if accepted_for_manga >= max_per_manga:
                break
            target_id = query_spec.get("id")
            target_domain = query_spec.get("domain")
            search_query = query_spec.get("query", "")
            search_title = str(manga.get("search_title") or manga.get("title") or "").strip()
            direct_template = str(query_spec.get("search_page_template") or "")
            direct_term_template = str(query_spec.get("search_term_template") or "{title}")
            try:
                direct_term = direct_term_template.format(title=search_title)
            except (KeyError, ValueError):
                direct_term = search_title
            source_queue = []
            if direct_template:
                source_queue.append({
                    "kind": "marketplace",
                    "template": direct_template,
                    "term": direct_term,
                    "engine_index": -1,
                })
            # Search-engine fallback is used only if the marketplace page did not yield
            # enough direct product URLs. It remains domain-filtered and never supplies prices.
            for engine_index, engine_template in enumerate(engine_urls):
                source_queue.append({
                    "kind": "search_engine",
                    "template": engine_template,
                    "term": search_query,
                    "engine_index": engine_index,
                })

            accepted_for_target = 0
            for source in source_queue:
                if accepted_for_manga >= max_per_manga or accepted_for_target >= max_per_target:
                    break
                search_url = source["template"].format(query=quote(source["term"], safe=""))
                queries_checked += 1
                try:
                    robots_ok, robots_reason = robots_can_fetch(search_url, user_agent, timeout, robots_cache)
                    if not robots_ok:
                        diagnostics.append({
                            "manga_id": manga_id,
                            "target_id": target_id,
                            "target_domain": target_domain,
                            "search_engine": urlparse(search_url).hostname,
                            "search_url": search_url,
                            "source_kind": source["kind"],
                            "result_link_count": 0,
                            "candidate_count": 0,
                            "robots_allowed": False,
                            "robots_reason": robots_reason,
                            "response_bytes": 0,
                        })
                        continue

                    request = Request(search_url, headers={
                        "User-Agent": user_agent,
                        "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/xml",
                        "Accept-Language": "ja-JP,ja;q=0.9",
                    })
                    with urlopen(request, timeout=timeout) as response:
                        final_url = response.geturl()
                        raw = response.read(max_bytes + 1)
                        if len(raw) > max_bytes:
                            raise ValueError("SEARCH_RESPONSE_TOO_LARGE")
                        charset = response.headers.get_content_charset() or "utf-8"
                        html = raw.decode(charset, errors="replace")

                    is_rss = source["kind"] == "search_engine" and "format=rss" in search_url.lower()
                    if is_rss:
                        search_results = parse_bing_rss(raw)
                    elif source["kind"] == "marketplace":
                        parser = MarketplaceResultsParser()
                        parser.feed(html)
                        search_results = parser.items
                    else:
                        parser = SearchResultsParser()
                        parser.feed(html)
                        search_results = parser.items

                    new_from_source = 0
                    rejected_hosts = {}
                    rejected_host_samples = {}
                    rejected_paths = []
                    for result in search_results:
                        url = unwrap_search_url(result.get("url", ""), base_url=final_url or search_url)
                        if not valid_https_url(url):
                            rejected_hosts["invalid_or_non_https"] = rejected_hosts.get("invalid_or_non_https", 0) + 1
                            continue
                        host = urlparse(url).hostname or "unknown"
                        if target_domain and not allowed_host(url, [target_domain]):
                            key = "off_target:" + host
                            rejected_hosts[key] = rejected_hosts.get(key, 0) + 1
                            rejected_host_samples.setdefault(key, [])
                            if len(rejected_host_samples[key]) < 2:
                                rejected_host_samples[key].append(url[:500])
                            continue
                        if not allowed_host(url, allowed_domains):
                            rejected_hosts[host] = rejected_hosts.get(host, 0) + 1
                            rejected_host_samples.setdefault(host, [])
                            if len(rejected_host_samples[host]) < 2:
                                rejected_host_samples[host].append(url[:500])
                            continue
                        if not is_product_candidate(url, allowed_domains):
                            if len(rejected_paths) < 3:
                                rejected_paths.append(url)
                            continue
                        if url in seen_for_manga:
                            continue
                        old = previous_by_url.get(url, {})
                        candidate = {
                            "manga_id": manga_id,
                            "manga_title": manga.get("title"),
                            "listing_url": url,
                            "search_result_title": result.get("title"),
                            "source_domain": host,
                            "first_seen_at": old.get("first_seen_at") or now,
                            "last_seen_at": now,
                            "discovered_by_query": search_query,
                            "discovered_by_target": target_id,
                            "discovered_by_source": source["kind"],
                            "discovered_by_engine": urlparse(search_url).hostname,
                            "verification_status": "UNVERIFIED_LEAD",
                            "price_jpy": None,
                            "availability": "UNKNOWN",
                            "volume_coverage": None,
                            "condition": "UNKNOWN",
                            "shipping_jpy": None,
                            "notes": "検索で発見した商品ページ候補。価格・在庫・全巻・状態・送料の証拠ではない。",
                        }
                        current_by_url[url] = candidate
                        seen_for_manga.add(url)
                        accepted_for_manga += 1
                        accepted_for_target += 1
                        new_from_source += 1
                        if accepted_for_manga >= max_per_manga or accepted_for_target >= max_per_target:
                            break

                    lower_html = html.lower()
                    diagnostics.append({
                        "manga_id": manga_id,
                        "target_id": target_id,
                        "target_domain": target_domain,
                        "search_url": search_url,
                        "final_url": final_url,
                        "search_engine": urlparse(search_url).hostname,
                        "source_kind": source["kind"],
                        "response_bytes": len(raw),
                        "result_link_count": len(search_results),
                        "candidate_count": new_from_source,
                        "fallback_used": source["kind"] == "search_engine",
                        "robots_allowed": True,
                        "robots_reason": robots_reason,
                        "rejected_host_counts": rejected_hosts,
                        "rejected_host_samples": rejected_host_samples,
                        "rejected_path_samples": rejected_paths,
                        "has_result_marker": bool(search_results) or any(token in lower_html for token in (
                            "result__a", "b_algo", "web-result", "検索結果", "商品一覧", "<item>"
                        )),
                        "page_appears_blocked": any(token in lower_html for token in (
                            "captcha", "bot detection", "unusual traffic", "verify you are human",
                            "consent required", "enable javascript to continue", "アクセスが集中"
                        )),
                    })
                except Exception as exc:
                    errors.append({
                        "manga_id": manga_id,
                        "manga_title": manga.get("title"),
                        "target_id": target_id,
                        "target_domain": target_domain,
                        "source_kind": source["kind"],
                        "query": source["term"],
                        "search_url": search_url,
                        "search_engine": urlparse(search_url).hostname,
                        "error": type(exc).__name__ + ": " + str(exc)[:240],
                        "checked_at": now,
                    })
                finally:
                    time.sleep(delay)

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
        "queries_with_anchors": sum(1 for item in diagnostics if item.get("result_link_count")),
        "queries_with_result_marker": sum(1 for item in diagnostics if item.get("has_result_marker")),
        "queries_using_fallback": sum(1 for item in diagnostics if item.get("fallback_used")),
        "queries_finding_candidates": sum(1 for item in diagnostics if item.get("candidate_count", 0) > 0),
        "queries_appearing_blocked": sum(1 for item in diagnostics if item.get("page_appears_blocked")),
        "new_candidate_count": len(set(current_by_url) - set(previous_by_url)),
        "current_seen_count": len(current_by_url),
        "retained_candidate_count": len(candidates),
        "manual_seed_count": manual_seed_count,
        "candidates": candidates,
    }
    save_json(OUTPUT_PATH, result)
    print(json.dumps({key: result[key] for key in (
        "generated_at", "discovery_status", "queries_checked", "queries_with_anchors",
        "queries_with_result_marker", "queries_using_fallback", "queries_finding_candidates",
        "queries_appearing_blocked", "new_candidate_count",
        "current_seen_count", "retained_candidate_count", "manual_seed_count"
    )}, ensure_ascii=False))
    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Limit manga queries for a test run")
    args = parser.parse_args()
    run_discovery(args.limit)
