"""
Woot size finder — sample.

Woot's own site loads its deal listings from a public GraphQL API
(discovered via the network tab): an AWS AppSync endpoint fronted by
CloudFront, called with a client-side API key that ships in Woot's own
JS bundle (the same key every visitor's browser uses). That API returns,
per offer, a list of purchasable Items — each with Color/Size/Gender
attributes plus live SoldOut/Quantity — which is exactly the "what's
available in my size" data this tool needs. No HTML scraping required.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify, render_template_string, request

GRAPHQL_URL = "https://d24qg5zsx8xdc4.cloudfront.net/graphql"
API_KEY = "da2-hk2jpo7aljfvxollvmieghuqlu"
HEADERS = {
    "x-api-key": API_KEY,
    "User-Agent": "Mozilla/5.0 (compatible; WootSizeFinder/1.0)",
}

# Woot's internal category keys that carry apparel/shoes today:
#   sport  = "Sports & Outdoors" (Men's/Women's/Kids' apparel & shoe subcategories)
#   home   = "Home & Kitchen" (Baby/Boys'/Girls' Apparel live in here, mixed in with furniture etc.)
#   shirt  = the print-on-demand tee business (shirt.woot.com) — always-in-stock, sizes
#            encoded as "Men's Small" / "Women's Large" instead of a separate Gender attribute.
CATEGORIES = ["sport", "home", "shirt"]
PAGE_SIZE = 50
MAX_WORKERS = 6
CACHE_TTL_SECONDS = 15 * 60

SEARCH_QUERY = """
{{
  searchOffers(Filter: {{ Categories: ["{category}"] }}, Sort: BestSelling, Limit: {limit}, Skip: {skip}) {{
    TotalHits
    Offers {{
      Id
      Title
      Slug
      Site {{ Hostname }}
      Items {{
        SoldOut
        Quantity
        SalePrice
        ListPrice
        Attributes {{ Key Value }}
        Photos {{ Url }}
      }}
    }}
  }}
}}
"""

# Woot doesn't tag items with a garment type (top/pants/shoes), so this
# is a best-effort keyword guess from the title. Order matters: more
# specific keywords should come first.
TYPE_KEYWORDS = [
    ("Shoes", ["shoe", "sneaker", "boot", "sandal", "slip-on", "slip on", "cleat", "flip flop", "clog"]),
    ("Dresses & Sets", ["dress", "gown", "jumpsuit", "overall", "bodysuit", "romper", "onesie", "pajama", "sleeper"]),
    ("Pants", ["jean", "pant", "short", "legging", "jogger", "trouser", "skirt"]),
    ("Outerwear", ["jacket", "coat", "hoodie", "sweater", "cardigan", "vest"]),
    ("Tops", ["shirt", "tee", "t-shirt", "top", "tank", "polo", "blouse"]),
    ("Intimates & Accessories", ["boxer", "brief", "sock", "bra", "underwear", "glove", "hat", "belt", "swimsuit", "bathing suit"]),
]

GENDER_MAP = {
    "men": "Men", "men's": "Men", "mens": "Men", "male": "Men",
    "women": "Women", "women's": "Women", "womens": "Women", "female": "Women", "ladies": "Women",
    "boys": "Kids", "boys'": "Kids", "girls": "Kids", "girls'": "Kids",
    "kids": "Kids", "kid's": "Kids", "kids'": "Kids", "youth boys": "Kids", "youth girls": "Kids",
    "unisex": "Unisex", "unisex-adult": "Unisex", "unisex-adults": "Unisex", "adults": "Unisex",
}

# The "shirt" category encodes gender as a prefix on the Size value itself
# ("Men's Small") instead of a separate Gender attribute.
SIZE_GENDER_PREFIX_RE = re.compile(r"^(Men's|Women's|Boys'|Girls'|Kids'|Unisex)\s+(.+)$", re.IGNORECASE)

# Best-effort ordering for common letter sizes so the size filter and each
# card's size pills read S, M, L instead of sorting alphabetically (which
# would put "Large" before "Medium" before "Small").
SIZE_ORDER_NAMES = [
    "xxs", "extra small", "x-small", "xs",
    "s", "small", "sm",
    "m", "medium", "md",
    "l", "large", "lg",
    "xl", "extra large", "x-large",
    "xxl", "2xl", "xx-large",
    "xxxl", "3xl", "xxx-large",
]
SIZE_ORDER = {name: i for i, name in enumerate(SIZE_ORDER_NAMES)}
SIZE_NUMERIC_PREFIX_RE = re.compile(r"^(\d+(\.\d+)?)")

# Jewelry/fragrance/electronics items sometimes carry both a Size and a
# Gender-ish attribute too (e.g. "Men's Cologne 3.4 oz", a "36mm" watch
# face) and would otherwise slip through as if they were clothing sizes.
NON_APPAREL_SIZE_RE = re.compile(
    r"\b(oz|ounce|fl\s*oz|carat|ct|ml|gram|grams|can)\b"  # fragrance/cosmetics
    r"|\d+(\.\d+)?\s*mm\b"  # watch faces
    r"|\d+(\.\d+)?\s*x\s*\d+"  # product dimensions
    r"|^\d{2,3}-\d{1,2}-\d{2,3}$",  # eyewear frame codes, e.g. "52-18-145"
    re.IGNORECASE,
)


def normalize_gender(raw):
    if not raw:
        return "Unisex"
    key = raw.strip().lower()
    return GENDER_MAP.get(key, "Other")


def classify_garment(title):
    lowered = title.lower()
    for label, keywords in TYPE_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return label
    return "Other"


def extract_size_and_gender(attrs):
    size = attrs.get("Size")
    gender_raw = attrs.get("Gender")
    if size and not gender_raw:
        match = SIZE_GENDER_PREFIX_RE.match(size)
        if match:
            gender_raw = match.group(1)
            size = match.group(2)
    return size, gender_raw


def size_sort_key(size):
    raw = (size or "").strip()
    low = raw.lower()
    try:
        return (0, float(raw), raw)
    except ValueError:
        pass
    if low in SIZE_ORDER:
        return (1, SIZE_ORDER[low], raw)
    match = SIZE_NUMERIC_PREFIX_RE.match(raw)
    if match:
        return (0, float(match.group(1)), raw)
    return (2, 0, low)


def fetch_offers_page(category, skip):
    query = SEARCH_QUERY.format(category=category, limit=PAGE_SIZE, skip=skip)
    response = requests.get(GRAPHQL_URL, headers=HEADERS, params={"query": query}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]["searchOffers"]


def offer_to_variants(offer, category):
    title = offer["Title"]
    hostname = offer["Site"]["Hostname"]
    url = f"https://{hostname}/offers/{offer['Slug']}"
    # The whole "shirt" category is printed tees; title keywords don't
    # reliably say so (deal titles are jokes, e.g. "The Scientific Meepthod").
    garment_type = "Tops" if category == "shirt" else classify_garment(title)

    # "Home & Kitchen" is mostly non-apparel (perfume, jewelry, bath towels),
    # some of which still carry both a Size and a Gender-ish attribute
    # (e.g. "Men's Cologne 3.4 oz"). Woot's own category/taxonomy data is
    # too unreliable to filter on, so require a real garment-keyword match
    # for anything sourced from "home" instead of trusting Size+Gender alone.
    if category == "home" and garment_type == "Other":
        return []

    variants = []
    for item in offer["Items"]:
        if item.get("SoldOut") or not item.get("Quantity"):
            continue
        attrs = {a["Key"]: a["Value"] for a in item["Attributes"]}
        size, gender_raw = extract_size_and_gender(attrs)
        # Require both a size AND some gender signal — this is what keeps
        # non-apparel "Home & Kitchen" items (furniture, bath towels, ...)
        # out, since those carry a bare "Size" attribute but no Gender.
        if not size or not gender_raw:
            continue
        if NON_APPAREL_SIZE_RE.search(size):
            continue
        photos = item.get("Photos") or []
        variants.append({
            "title": title,
            "url": url,
            "garment_type": garment_type,
            "gender": normalize_gender(gender_raw),
            "color": attrs.get("Color"),
            "size": size,
            "sale_price": item.get("SalePrice"),
            "list_price": item.get("ListPrice"),
            "photo_url": photos[0]["Url"] if photos else None,
        })
    return variants


def fetch_all_variants():
    first_pages = {category: fetch_offers_page(category, 0) for category in CATEGORIES}

    jobs = []
    for category, data in first_pages.items():
        skip = PAGE_SIZE
        while skip < data["TotalHits"]:
            jobs.append((category, skip))
            skip += PAGE_SIZE

    offers_by_category = [(category, offer) for category, data in first_pages.items() for offer in data["Offers"]]

    if jobs:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(fetch_offers_page, category, skip): category for category, skip in jobs}
            for future in futures:
                category = futures[future]
                for offer in future.result()["Offers"]:
                    offers_by_category.append((category, offer))

    seen_offer_ids = set()
    variants = []
    for category, offer in offers_by_category:
        offer_id = offer.get("Id")
        if offer_id in seen_offer_ids:
            continue
        seen_offer_ids.add(offer_id)
        variants.extend(offer_to_variants(offer, category))

    return variants


_cache = {"variants": [], "fetched_at": 0}


def get_variants(force=False):
    stale = (time.time() - _cache["fetched_at"]) > CACHE_TTL_SECONDS
    if force or stale or not _cache["variants"]:
        _cache["variants"] = fetch_all_variants()
        _cache["fetched_at"] = time.time()
    return _cache["variants"]


def compute_facets(variants, gender, garment_type, size):
    """Facet counts for each filter, computed against the *other* active
    filters (not itself) — the standard faceted-search pattern, so picking
    a Gender narrows the Size/Type options without those two dropdowns
    disappearing altogether."""
    def keep(v, ignore):
        if ignore != "gender" and gender and v["gender"].lower() != gender:
            return False
        if ignore != "type" and garment_type and v["garment_type"].lower() != garment_type:
            return False
        if ignore != "size" and size and v["size"].lower() != size:
            return False
        return True

    genders = sorted({v["gender"] for v in variants if keep(v, "gender")})
    types = sorted({v["garment_type"] for v in variants if keep(v, "type")})
    sizes = sorted({v["size"] for v in variants if keep(v, "size")}, key=size_sort_key)
    return {"genders": genders, "types": types, "sizes": sizes}


app = Flask(__name__)


@app.route("/api/deals")
def api_deals():
    variants = get_variants()

    gender = request.args.get("gender", "").strip().lower()
    garment_type = request.args.get("type", "").strip().lower()
    size = request.args.get("size", "").strip().lower()

    results = variants
    if gender:
        results = [v for v in results if v["gender"].lower() == gender]
    if garment_type:
        results = [v for v in results if v["garment_type"].lower() == garment_type]
    if size:
        results = [v for v in results if v["size"].lower() == size]

    # Fold same product+color back into one card listing all matching sizes.
    grouped = {}
    for v in results:
        key = (v["title"], v["url"], v["color"])
        card = grouped.setdefault(key, {
            "title": v["title"],
            "url": v["url"],
            "color": v["color"],
            "gender": v["gender"],
            "garment_type": v["garment_type"],
            "sale_price": v["sale_price"],
            "list_price": v["list_price"],
            "photo_url": v["photo_url"],
            "sizes": [],
        })
        card["sizes"].append(v["size"])

    for card in grouped.values():
        card["sizes"].sort(key=size_sort_key)

    cards = sorted(grouped.values(), key=lambda c: c["title"])
    facets = compute_facets(variants, gender, garment_type, size)
    return jsonify({
        "cards": cards,
        "facets": facets,
        "fetched_at": _cache["fetched_at"],
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    variants = get_variants(force=True)
    return jsonify({"variant_count": len(variants), "fetched_at": _cache["fetched_at"]})


@app.route("/")
def index():
    variants = get_variants()
    facets = compute_facets(variants, "", "", "")
    return render_template_string(INDEX_HTML, **facets)


INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Woot Size Finder</title>
  <style>
    :root {
      --green: #0a8f5c;
      --green-dark: #086b46;
      --bg: #fafafa;
      --card-bg: #fff;
      --border: #e4e4e7;
      --text: #18181b;
      --text-muted: #71717a;
    }
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: var(--bg); color: var(--text); }
    .container { max-width: 1120px; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
    header h1 { margin: 0 0 0.25rem; font-size: 1.9rem; letter-spacing: -0.01em; }
    header .sub { color: var(--text-muted); margin: 0 0 1.5rem; max-width: 640px; line-height: 1.4; }
    .filter-bar { display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: end; background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px; padding: 1rem; margin-bottom: 1.25rem; position: sticky; top: 0.75rem; z-index: 10; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
    .field label { display: block; font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; color: var(--text-muted); margin-bottom: 0.35rem; }
    select { padding: 0.5rem 0.6rem; font-size: 0.95rem; border: 1px solid var(--border); border-radius: 6px; background: #fff; min-width: 150px; color: var(--text); }
    select:focus { outline: 2px solid var(--green); outline-offset: -1px; }
    .btn { padding: 0.55rem 1rem; font-size: 0.88rem; border-radius: 6px; border: 1px solid var(--border); background: #fff; cursor: pointer; font-weight: 600; color: var(--text); }
    .btn:hover { background: #f4f4f5; }
    .btn-primary { background: var(--green); border-color: var(--green); color: #fff; }
    .btn-primary:hover { background: var(--green-dark); }
    .status { color: var(--text-muted); font-size: 0.85rem; margin: 0 0 1rem; min-height: 1.2em; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 1rem; }
    .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px; padding: 0.85rem; transition: box-shadow 0.15s ease, transform 0.15s ease; }
    .card:hover { box-shadow: 0 8px 20px rgba(0,0,0,0.08); transform: translateY(-2px); }
    .card img { width: 100%; height: 170px; object-fit: contain; background: #f4f4f5; border-radius: 8px; margin-bottom: 0.65rem; }
    .card a.title { color: var(--text); text-decoration: none; font-weight: 600; font-size: 0.95rem; line-height: 1.3; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .card a.title:hover { color: var(--green); }
    .meta { color: var(--text-muted); font-size: 0.8rem; margin: 0.4rem 0; }
    .price { font-weight: 700; font-size: 1.05rem; }
    .price .strike { color: #a1a1aa; font-weight: 400; text-decoration: line-through; font-size: 0.82rem; margin-left: 0.4rem; }
    .sizes { margin-top: 0.55rem; display: flex; flex-wrap: wrap; gap: 0.3rem; }
    .size-pill { background: #eefcf5; color: var(--green-dark); border-radius: 5px; padding: 0.15rem 0.5rem; font-size: 0.78rem; font-weight: 600; }
    .empty { grid-column: 1/-1; text-align: center; color: var(--text-muted); padding: 3.5rem 1rem; }
    .more-hint { grid-column: 1/-1; text-align: center; color: var(--text-muted); padding: 1rem; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>Woot Size Finder</h1>
      <p class="sub">Live scrape of Woot's apparel &amp; shoe deals (Sports &amp; Outdoors, Home &amp; Kitchen, and the Shirt tee shop), filtered to sizes currently in stock.</p>
    </header>

    <div class="filter-bar">
      <div class="field">
        <label>Gender</label>
        <select id="gender" onchange="loadDeals()">
          <option value="">Any</option>
          {% for g in genders %}<option value="{{ g }}">{{ g }}</option>{% endfor %}
        </select>
      </div>
      <div class="field">
        <label>Type</label>
        <select id="type" onchange="loadDeals()">
          <option value="">Any</option>
          {% for t in types %}<option value="{{ t }}">{{ t }}</option>{% endfor %}
        </select>
      </div>
      <div class="field">
        <label>Size</label>
        <select id="size" onchange="loadDeals()">
          <option value="">Any</option>
          {% for s in sizes %}<option value="{{ s }}">{{ s }}</option>{% endfor %}
        </select>
      </div>
      <button class="btn" onclick="refresh()">Refresh data</button>
    </div>

    <div class="status" id="status"></div>
    <div class="grid" id="results"></div>
  </div>

  <script>
    const MAX_CARDS = 150;

    function escapeHtml(value) {
      const div = document.createElement('div');
      div.textContent = value == null ? '' : String(value);
      return div.innerHTML;
    }

    // Rebuilds a <select>'s options from a facet list. Returns true if the
    // previously-selected value is no longer valid given the other active
    // filters (so the caller knows to re-fetch with the corrected filter).
    function populateSelect(el, options, prevValue) {
      el.innerHTML = '<option value="">Any</option>' +
        options.map(o => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`).join('');
      if (prevValue && options.includes(prevValue)) {
        el.value = prevValue;
        return false;
      }
      el.value = '';
      return Boolean(prevValue);
    }

    function setStatus(text) {
      document.getElementById('status').textContent = text;
    }

    async function loadDeals() {
      const genderEl = document.getElementById('gender');
      const typeEl = document.getElementById('type');
      const sizeEl = document.getElementById('size');
      const gender = genderEl.value, type = typeEl.value, size = sizeEl.value;

      setStatus('Loading...');
      const params = new URLSearchParams({ gender, type, size });
      const res = await fetch('/api/deals?' + params.toString());
      const data = await res.json();

      const g1 = populateSelect(genderEl, data.facets.genders, gender);
      const g2 = populateSelect(typeEl, data.facets.types, type);
      const g3 = populateSelect(sizeEl, data.facets.sizes, size);
      if (g1 || g2 || g3) {
        // One of the current selections is no longer valid for the others
        // (e.g. switched Type away from a size that only existed for the
        // old type) — re-fetch once with the corrected combination.
        return loadDeals();
      }

      renderCards(data.cards);
      const age = Math.round((Date.now() / 1000 - data.fetched_at) / 60);
      setStatus(data.cards.length + ' matching color/product combos (data ' + age + ' min old)');
    }

    async function refresh() {
      setStatus('Refreshing from Woot, this can take a bit...');
      await fetch('/api/refresh', { method: 'POST' });
      await loadDeals();
    }

    function renderCards(cards) {
      const el = document.getElementById('results');
      if (cards.length === 0) {
        el.innerHTML = '<div class="empty">No deals match those filters right now. Try loosening one.</div>';
        return;
      }
      const shown = cards.slice(0, MAX_CARDS);
      const html = shown.map(c => {
        const sizesHtml = c.sizes.map(s => `<span class="size-pill">${escapeHtml(s)}</span>`).join('');
        const salePrice = typeof c.sale_price === 'number' ? '$' + c.sale_price.toFixed(2) : 'N/A';
        const listPriceHtml = (typeof c.list_price === 'number' && c.list_price !== c.sale_price)
          ? `<span class="strike">$${c.list_price.toFixed(2)}</span>`
          : '';
        const imgHtml = c.photo_url
          ? `<img src="${escapeHtml(c.photo_url)}" alt="${escapeHtml(c.title)}" loading="lazy">`
          : '<div class="card-img-placeholder" style="height:170px;background:#f4f4f5;border-radius:8px;margin-bottom:0.65rem"></div>';
        return `
          <div class="card">
            ${imgHtml}
            <a class="title" href="${escapeHtml(c.url)}" target="_blank" rel="noopener">${escapeHtml(c.title)}</a>
            <div class="meta">${escapeHtml(c.gender)} &middot; ${escapeHtml(c.garment_type)} &middot; ${escapeHtml(c.color || '')}</div>
            <div class="price">${salePrice} ${listPriceHtml}</div>
            <div class="sizes">${sizesHtml}</div>
          </div>`;
      }).join('');
      el.innerHTML = cards.length > MAX_CARDS
        ? html + `<div class="more-hint">Showing first ${MAX_CARDS} of ${cards.length} — narrow the filters to see more.</div>`
        : html;
    }

    loadDeals();
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5050)
