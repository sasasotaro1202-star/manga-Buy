"""Produce an evidence-aware Markdown report with every known product URL."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import quote

from manga_core import (
    ROOT, now_jst, load_json, save_json, manga_map, listing_decision,
    expected_count, safe_markdown, minimum_cost_cover
)

CATALOG_PATH = ROOT / "config" / "manga_catalog.json"
MARKETPLACES_PATH = ROOT / "config" / "marketplaces.json"
CONDITION_PATH = ROOT / "config" / "condition_policy.json"
LISTINGS_PATH = ROOT / "data" / "listings.json"
OBSERVATIONS_PATH = ROOT / "data" / "observations.json"
DISCOVERED_PATH = ROOT / "data" / "discovered_candidates.json"
REPORT_PATH = ROOT / "data" / "reports" / "latest.md"


def search_url(template, query):
    encoded = quote(query, safe="")
    return str(template).replace("{query}", encoded)


def yen(value):
    if not isinstance(value, int):
        return "未確認"
    return "¥" + format(value, ",")


def generate_report():
    catalog_data = load_json(CATALOG_PATH, {"manga": []})
    marketplace_config = load_json(MARKETPLACES_PATH, {"marketplaces": []})
    policy = load_json(CONDITION_PATH, {})
    listing_data = load_json(LISTINGS_PATH, {"listings": []})
    observations_data = load_json(OBSERVATIONS_PATH, {"observations": {}})
    discovered_data = load_json(DISCOVERED_PATH, {"candidates": []})
    observed_by_id = observations_data.get("observations", {})
    manga_entries = catalog_data.get("manga", [])
    manga_by_id = manga_map(catalog_data)
    listings_by_manga = {}
    for listing in listing_data.get("listings", []):
        if isinstance(listing, dict):
            listings_by_manga.setdefault(listing.get("manga_id"), []).append(listing)
    discovered_by_manga = {}
    for item in discovered_data.get("candidates", []):
        if isinstance(item, dict):
            discovered_by_manga.setdefault(item.get("manga_id"), []).append(item)

    confirmed_count = 0
    provisional_count = 0
    error_count = 0
    lines = [
        "# manga-Buy 最新レポート",
        "",
        "確認時刻（JST）： " + now_jst().isoformat(),
        "",
        "## このレポートの読み方",
        "",
        "- **確認済み最安**：登録済み出品の中で、全巻・版・状態・在庫・24時間以内の価格観測・7日以内の送料/手数料確認をすべて通過した最安値。",
        "- **暫定候補**：出品は見つかったが、巻数または取引条件の確認が足りず、購入可能な全巻セットとは断定できない。",
        "- **未確認/エラー**：元の商品URLを必ず残す。取得エラーは売り切れを意味しない。",
        "- 検索リンクは出品探し用であり、表示価格や在庫をこの監視システムが確認したことを意味しない。",
        "- 表示対象は登録/発見済み候補だけです。全インターネットの絶対最安値を保証するものではありません。",
        "",
        "## 全体状況",
        "",
        f"- 追跡作品：{len(manga_entries)}",
        f"- 登録商品URL：{len(listing_data.get('listings', []))}",
        f"- 発見候補URL：{len(discovered_data.get('candidates', []))}",
        f"- 未検証の初期候補シード：{discovered_data.get('manual_seed_count', 0)}",
        f"- 直近の商品取得成功：{observations_data.get('summary', {}).get('ok_count', 0)}",
        f"- 直近の商品取得エラー：{observations_data.get('summary', {}).get('error_count', 0)}",
        f"- 検索発見状態：{discovered_data.get('discovery_status', 'NOT_RUN')}",
        f"- 検索結果リンクを含むページ：{discovered_data.get('queries_with_anchors', 0)}/{discovered_data.get('queries_checked', 0)}",
        f"- 検索結果マーカーを検出したページ：{discovered_data.get('queries_with_result_marker', 0)}/{discovered_data.get('queries_checked', 0)}",
        f"- ブロック/認証画面の疑い：{discovered_data.get('queries_appearing_blocked', 0)}",
        "",
        "## 第一候補・第二候補の比較",
        "",
    ]

    for group, label in (("first", "第一候補"), ("second", "第二候補")):
        group_items = [entry for entry in manga_entries if entry.get("candidate_group") == group]
        lines.extend(["### " + label, ""])
        for manga in group_items:
            count, count_confirmed = expected_count(manga, allow_provisional=True)
            confirmed = []
            provisional = []
            registered = listings_by_manga.get(manga.get("id"), [])
            all_decisions = []
            basket_candidates = []
            for listing in registered:
                observation = observed_by_id.get(str(listing.get("id") or ""), {})
                decision_args = {
                    "now": now_jst(),
                    "max_price_age_hours": int(marketplace_config.get("monitor", {}).get("max_price_age_hours", 24)),
                    "max_cost_age_days": int(marketplace_config.get("monitor", {}).get("max_cost_check_age_days", 7)),
                }
                decision = listing_decision(listing, observation, manga, policy, **decision_args)
                all_decisions.append(decision)
                if decision["status"] == "CONFIRMED":
                    confirmed.append(decision)
                elif decision["status"] == "PROVISIONAL":
                    provisional.append(decision)

                # Independently admit verified partial lots for a no-gap, no-double-buy
                # basket plan. They never qualify as standalone full-set listings.
                basket_decision = listing_decision(
                    listing, observation, manga, policy, require_complete=False, **decision_args
                )
                if basket_decision["status"] == "CONFIRMED":
                    basket_candidates.append({
                        "status": "CONFIRMED",
                        "total_jpy": basket_decision["total_jpy"],
                        "included_ranges": basket_decision["included_ranges"],
                        "listing_url": basket_decision["listing_url"],
                        "seller": basket_decision.get("seller"),
                        "condition": basket_decision.get("condition"),
                        "listing_id": basket_decision.get("listing_id"),
                    })
                if observation.get("retrieval_status") != "OK":
                    error_count += 1
            confirmed.sort(key=lambda row: (row.get("total_jpy") or 10**12, row.get("condition") == "RENTAL_USED"))
            provisional.sort(key=lambda row: (row.get("total_jpy") or 10**12, row.get("condition") == "RENTAL_USED"))
            lines.append("#### " + safe_markdown(manga.get("title")) )
            lines.append("")
            if count is not None:
                status_text = "公式/基準確認済み" if count_confirmed else "暫定・再確認必要"
                lines.append(f"- 対象巻数：{count}巻（{status_text}）")
            else:
                lines.append("- 対象巻数：未確認。確定全巻判定不可。")
            if manga.get("volume_count_source_url"):
                lines.append("- 巻数確認元：[公式/確認ページ](" + manga["volume_count_source_url"] + ")")
            if confirmed:
                cheapest = confirmed[0]
                confirmed_count += 1
                lines.append(f"- **登録済み出品内の確認済み最安：{yen(cheapest['total_jpy'])}**")
                lines.append(f"  - 商品価格 {yen(cheapest['price_jpy'])} + 送料 {yen(cheapest['shipping_jpy'])} + 必須手数料 {yen(cheapest['mandatory_fees_jpy'])}")
                lines.append(f"  - 状態：{cheapest['condition']} / 在庫：{cheapest['availability']} / 観測：{cheapest.get('retrieved_at')}")
                if cheapest.get("quality_note"):
                    lines.append("  - 注意：" + safe_markdown(cheapest["quality_note"]))
                lines.append("  - [商品ページ](" + cheapest["listing_url"] + ")")
                for alternative in confirmed[1:5]:
                    lines.append(f"  - 比較候補：{yen(alternative['total_jpy'])} — [" + safe_markdown(alternative.get("seller") or "販売ページ") + "](" + alternative["listing_url"] + ")")
            elif provisional:
                provisional_count += 1
                cheapest = provisional[0]
                lines.append(f"- 暫定候補の最安表示：{yen(cheapest['total_jpy'])} — [商品ページ](" + cheapest["listing_url"] + ")")
                lines.append("  - **購入可能な全巻最安とは確定していません。** 巻数メタデータまたは登録情報を再確認してください。")
            else:
                lines.append("- 確認済みの購入可能な全巻セット：まだなし。")

            # Only attempt combination when the series' current required volume count
            # is source/curation-confirmed. A provisional count is never a completeness proof.
            basket_plan = minimum_cost_cover(basket_candidates, count) if count_confirmed and count else None
            if basket_plan:
                duplicate_count = basket_plan.get("duplicate_volume_count", 0)
                duplicate_text = f"（重複巻 {duplicate_count}冊分あり）" if duplicate_count else "（重複巻なし）"
                lines.append(f"- **確認済み出品を組み合わせた最小総額プラン：{yen(basket_plan['total_jpy'])}** {duplicate_text}")
                for item in basket_plan.get("listings", []):
                    item_ranges = item.get("included_ranges") or []
                    volume_text = ", ".join(f"{start}〜{end}巻" for start, end in item_ranges)
                    lines.append(f"  - [{safe_markdown(item.get('seller') or item.get('listing_id') or '商品ページ')}]({item.get('listing_url')}) — {volume_text}: {yen(item.get('total_jpy'))}")
            elif count_confirmed and count and registered:
                lines.append("- 組み合わせ購入プラン：現在の登録出品では、巻抜けなしの確認済みプランを作れません。")
            else:
                lines.append("- 組み合わせ購入プラン：巻数の確定または条件を満たす登録出品が不足。")

            if all_decisions:
                lines.append("- 登録商品ページの確認結果：")
                for decision in all_decisions:
                    observation = observed_by_id.get(str(decision.get("listing_id") or ""), {})
                    label_text = decision.get("status")
                    reasons = ", ".join(decision.get("reasons") or []) or "条件通過"
                    lines.append("  - [" + safe_markdown(decision.get("seller") or decision.get("listing_id") or "商品") + "](" + str(decision.get("listing_url") or "") + ") — " + label_text + " / " + reasons)
                    if observation.get("retrieval_status") != "OK":
                        lines.append("    - 取得状態：" + safe_markdown(observation.get("retrieval_status")) + " / " + safe_markdown(observation.get("error")))
            discovered = discovered_by_manga.get(manga.get("id"), [])
            if discovered:
                lines.append("- 発見・初期登録済み候補リンク（すべて未検証。全巻・版・状態・送料は未確認）：")
                observations_for_url = {
                    str(value.get("listing_url") or ""): value
                    for value in observed_by_id.values() if isinstance(value, dict)
                }
                for item in discovered[:8]:
                    item_url = str(item.get("listing_url") or "")
                    observed = observations_for_url.get(item_url, {})
                    label = safe_markdown(item.get("search_result_title") or item.get("source_domain") or "候補リンク")
                    provenance = "（初期URLシード）" if item.get("discovered_by_source") == "manual_mercari_search_seed" else ""
                    lines.append("  - [" + label + "](" + item_url + ")" + provenance)
                    if observed:
                        if observed.get("retrieval_status") == "OK" and isinstance(observed.get("price_jpy"), int):
                            lines.append("    - 構造化データ観測：" + yen(observed.get("price_jpy")) + " / 在庫：" + safe_markdown(observed.get("availability") or "UNKNOWN") + " / 時刻：" + safe_markdown(observed.get("retrieved_at")) + "（全巻・送料込み総額ではありません）")
                        else:
                            lines.append("    - 取得結果：" + safe_markdown(observed.get("retrieval_status") or "未取得") + " / " + safe_markdown(observed.get("error") or "価格・在庫の構造化データを確認できませんでした"))
                    else:
                        lines.append("    - 未取得。検索発見URLであり、購入条件は未確認です。")
            query = str(manga.get("search_title") or manga.get("title") or "") + " " + ("全巻セット 中古" if manga.get("series_status") == "completed" else "既刊全巻セット 中古")
            lines.append("- 各サイトで現在の出品を探す：")
            enabled = [site for site in marketplace_config.get("marketplaces", []) if site.get("enabled")]
            for site in enabled:
                link = search_url(site.get("search_template", ""), query)
                lines.append("  - [" + safe_markdown(site.get("name")) + "](" + link + ")")
            lines.append("")

    lines.extend([
        "## データ品質・未解決事項",
        "",
        "- 各価格の出所は商品ページと取得時刻にひも付けて保存します。",
        "- 価格を抽出できない、通貨が不明、複数価格が競合する場合は最安ランキングに入れません。",
        "- 送料/必須手数料が不明、状態が不明、版や巻の収録範囲が未確認の場合は確認済みの合計にしません。",
        "- robots.txtで不許可、アクセス制限、ネットワーク障害、HTTPエラーは在庫切れではなく取得エラーとして扱います。",
        "- 検索状態 EMPTY は「検索成功」ではなく候補URLを1件も抽出できなかったことを示します。ブロック/HTML変更の診断を確認し、検索リンクから実際の商品ページを登録してください。",
        "- 価格履歴は data/price_history.jsonl に保存されます。単発観測のみで値下がり傾向を断定しません。",
        "",
        "## 更新・検証",
        "",
        "- 観測生成時刻：" + str(observations_data.get("generated_at") or "未実行"),
        "- 検索生成時刻：" + str(discovered_data.get("generated_at") or "未実行"),
        "- 巻数メタデータ監査は scripts/validate.py の結果も参照してください。",
        "",
    ])
    content = "\n".join(lines)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(content + "\n", encoding="utf-8")
    print("REPORT_WRITTEN=" + str(REPORT_PATH))
    print("CONFIRMED_SERIES_WITH_AT_LEAST_ONE_OFFER=" + str(confirmed_count))
    print("PROVISIONAL_SERIES_WITH_AT_LEAST_ONE_OFFER=" + str(provisional_count))
    print("LISTING_RETRIEVAL_ERRORS=" + str(error_count))
    return content


if __name__ == "__main__":
    generate_report()
