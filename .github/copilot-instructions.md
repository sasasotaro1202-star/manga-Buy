# manga-Buy — project instructions

## Objective
Build a free-first used physical manga price intelligence system that minimises the verified cash cost of getting every required volume in an acceptable condition. The reference style is the user's PC price intelligence repository, but this repository is independent.

## Non-negotiable evidence rules
- Never invent a listing URL, seller, price, stock, shipping cost, volume count, condition, discount, or sale date.
- Keep item page URL visible when retrieval fails, parsing fails, stock is unknown, cost is incomplete, or the result is stale.
- A search page is a discovery link, not evidence of a product's price or stock.
- A tool error is not evidence that a listing is sold out.
- Never treat unknown shipping or mandatory checkout fees as zero.
- Only direct product-page structured data or expressly timestamped human verification can support an observation. Do not infer prices from unrelated search snippets or page text.
- Default verified-freshness window: price/stock <=24 hours; shipping/mandatory fees human-check <=7 days.
- Rank confirmed all-volume sets separately from provisional and incomplete candidates.
- Do not label the result “absolute cheapest”. Use “lowest confirmed among registered listings”.
- Never bypass login, CAPTCHA, robots.txt, rate limits, or access controls. No paid API is a required dependency.

## User's condition floor
Prioritise price, but exclude items with water damage, mould, missing pages, unreadable/severe damage, strong odour, or heavy writing. Yellowing, fading, light scuffs, minor folds, no obi and normal signs of use may be acceptable. Rental-up books and books without dust jackets may be shown only when explicitly labelled; do not silently merge them with ordinary copies. Unknown condition never qualifies as confirmed.

## Edition and completeness
- Keep printed editions distinct: standard volumes, bunko, complete edition, wide edition, special edition, rental-up, and cut/cropped volumes are not interchangeable.
- Ongoing series have a provisional current count and a verified count/source status. Never use a provisional count to assert “complete”.
- Tokyo Ghoul's main series and Tokyo Ghoul:re are separate mapped blocks; combined set coverage must account for both.
- A price ranking must check coverage, title match, edition match, stock, condition, JPY price, source URL, shipping/fees, and freshness.
- The report must disclose coverage and evidence gaps rather than silently dropping failed products.

## Design and operations
- Python standard library only; no paid API keys/secrets.
- Unit tests use unittest.
- GitHub Actions runs a small daily search discovery and approved-URL monitor, validates configuration, regenerates reports, and commits changes only when generated files changed.
- Respect robots.txt. On any ambiguity, fail closed and retain a clickable original URL.
