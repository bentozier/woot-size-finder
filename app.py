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
import time

import requests
from flask import Flask, jsonify, render_template_string, request

GRAPHQL_URL = "https://d24qg5zsx8xdc4.cloudfront.net/graphql"
API_KEY = "da2-hk2jpo7aljfvxollvmieghuqlu"
HEADERS = {
    "x-api-key": API_KEY,
    "User-Agent": "Mozilla/5.0 (compatible; WootSizeFinder/1.0)",
}

# "sport" is Woot's internal key for the "Sports & Outdoors" category,
# which is where Woot files all of its Men's/Women's/Kids' apparel and
# shoe subcategories today.
CATEGORY = "sport"
PAGE_SIZE = 200
CACHE_TTL_SECONDS = 15 * 60

SEARCH_QUERY = """
{{
  searchOffers(Filter: {{ Categories: ["{category}"] }}, Sort: BestSelling, Limit: {limit}, Skip: {skip}) {{
    TotalHits
    Offers {{
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
    ("Pants", ["jean", "pant", "short", "legging", "jogger", "trouser", "skirt"]),
    ("Outerwear", ["jacket", "coat", "hoodie", "sweater", "vest"]),
    ("Tops", ["shirt", "tee", "t-shirt", "top", "tank", "polo", "blouse"]),
    ("Intimates & Accessories", ["boxer", "brief", "sock", "bra", "underwear", "glove", "hat", "belt"]),
]


GENDER_MAP = {
    "men": "Men", "men's": "Men", "mens": "Men", "male": "Men",
    "women": "Women", "women's": "Women", "womens": "Women", "female": "Women", "ladies": "Women",
    "boys": "Kids", "girls": "Kids", "kids": "Kids", "kid's": "Kids", "youth boys": "Kids", "youth girls": "Kids",
    "unisex": "Unisex", "unisex-adult": "Unisex", "unisex-adults": "Unisex", "adults": "Unisex",
}


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


def fetch_all_variants():
    variants = []
    skip = 0
    total = None
    while total is None or skip < total:
        query = SEARCH_QUERY.format(category=CATEGORY, limit=PAGE_SIZE, skip=skip)
        response = requests.get(GRAPHQL_URL, headers=HEADERS, params={"query": query}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(payload["errors"])

        data = payload["data"]["searchOffers"]
        total = data["TotalHits"]
        offers = data["Offers"]

        for offer in offers:
            title = offer["Title"]
            hostname = offer["Site"]["Hostname"]
            url = f"https://{hostname}/offers/{offer['Slug']}"
            garment_type = classify_garment(title)

            for item in offer["Items"]:
                if item.get("SoldOut") or not item.get("Quantity"):
                    continue
                attrs = {a["Key"]: a["Value"] for a in item["Attributes"]}
                size = attrs.get("Size")
                if not size:
                    continue
                photos = item.get("Photos") or []
                variants.append({
                    "title": title,
                    "url": url,
                    "garment_type": garment_type,
                    "gender": normalize_gender(attrs.get("Gender")),
                    "color": attrs.get("Color"),
                    "size": size,
                    "sale_price": item.get("SalePrice"),
                    "list_price": item.get("ListPrice"),
                    "photo_url": photos[0]["Url"] if photos else None,
                })

        if not offers:
            break
        skip += PAGE_SIZE

    return variants


_cache = {"variants": [], "fetched_at": 0}


def get_variants(force=False):
    stale = (time.time() - _cache["fetched_at"]) > CACHE_TTL_SECONDS
    if force or stale or not _cache["variants"]:
        _cache["variants"] = fetch_all_variants()
        _cache["fetched_at"] = time.time()
    return _cache["variants"]


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

    cards = sorted(grouped.values(), key=lambda c: c["title"])
    return jsonify({
        "cards": cards,
        "fetched_at": _cache["fetched_at"],
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    variants = get_variants(force=True)
    return jsonify({"variant_count": len(variants), "fetched_at": _cache["fetched_at"]})


@app.route("/")
def index():
    variants = get_variants()
    genders = sorted({v["gender"] for v in variants if v["gender"]})
    types = sorted({v["garment_type"] for v in variants})
    sizes = sorted({v["size"] for v in variants if v["size"]})
    return render_template_string(INDEX_HTML, genders=genders, types=types, sizes=sizes)


INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Woot Size Finder</title>
  <style>
    body { font-family: -apple-system, sans-serif; max-width: 1000px; margin: 2rem auto; padding: 0 1rem; color: #222; background: #fff; }
    h1 { margin-bottom: 0.25rem; }
    .sub { color: #666; margin-bottom: 1.5rem; }
    .filters { display: flex; gap: 0.75rem; flex-wrap: wrap; margin-bottom: 1.5rem; align-items: end; }
    .filters label { display: block; font-size: 0.8rem; color: #555; margin-bottom: 0.25rem; }
    select, input { padding: 0.4rem; font-size: 0.95rem; }
    button { padding: 0.45rem 0.9rem; cursor: pointer; }
    #results { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 1rem; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 0.9rem; }
    .card img { width: 100%; height: 180px; object-fit: contain; background: #f7f7f7; border-radius: 6px; margin-bottom: 0.6rem; }
    .card a { color: #0a5; text-decoration: none; font-weight: 600; }
    .card a:hover { text-decoration: underline; }
    .meta { color: #666; font-size: 0.85rem; margin: 0.3rem 0; }
    .sizes { margin-top: 0.5rem; }
    .size-pill { display: inline-block; background: #eef; border-radius: 4px; padding: 0.15rem 0.5rem; margin: 0.1rem; font-size: 0.8rem; }
    .price { font-weight: 600; }
    .status { color: #888; font-size: 0.85rem; margin-bottom: 1rem; }
  </style>
</head>
<body>
  <h1>Woot Size Finder</h1>
  <p class="sub">Sample scraper over Woot's Sports &amp; Outdoors category (apparel + shoes), filtered to sizes currently in stock.</p>

  <div class="filters">
    <div>
      <label>Gender</label>
      <select id="gender">
        <option value="">Any</option>
        {% for g in genders %}<option value="{{ g }}">{{ g }}</option>{% endfor %}
      </select>
    </div>
    <div>
      <label>Type</label>
      <select id="type">
        <option value="">Any</option>
        {% for t in types %}<option value="{{ t }}">{{ t }}</option>{% endfor %}
      </select>
    </div>
    <div>
      <label>Size</label>
      <input list="size-options" id="size" placeholder="e.g. M, 10, 32">
      <datalist id="size-options">
        {% for s in sizes %}<option value="{{ s }}">{% endfor %}
      </datalist>
    </div>
    <div><button onclick="loadDeals()">Filter</button></div>
    <div><button onclick="refresh()">Refresh data</button></div>
  </div>

  <div class="status" id="status"></div>
  <div id="results"></div>

  <script>
    async function loadDeals() {
      const gender = document.getElementById('gender').value;
      const type = document.getElementById('type').value;
      const size = document.getElementById('size').value;
      const params = new URLSearchParams({ gender, type, size });
      document.getElementById('status').textContent = 'Loading...';
      const res = await fetch('/api/deals?' + params.toString());
      const data = await res.json();
      renderCards(data.cards);
      const age = Math.round((Date.now() / 1000 - data.fetched_at) / 60);
      document.getElementById('status').textContent =
        data.cards.length + ' matching color/product combos (data ' + age + ' min old)';
    }

    async function refresh() {
      document.getElementById('status').textContent = 'Refreshing from Woot, this can take a bit...';
      await fetch('/api/refresh', { method: 'POST' });
      await loadDeals();
    }

    const MAX_CARDS = 150;

    function renderCards(cards) {
      const el = document.getElementById('results');
      const shown = cards.slice(0, MAX_CARDS);
      const html = shown.map(c => {
        const sizesHtml = c.sizes.sort().map(s => `<span class="size-pill">${s}</span>`).join('');
        const salePrice = typeof c.sale_price === 'number' ? '$' + c.sale_price.toFixed(2) : 'N/A';
        const listPriceHtml = (typeof c.list_price === 'number' && c.list_price !== c.sale_price)
          ? `<span style="color:#999;font-weight:400;text-decoration:line-through">$${c.list_price.toFixed(2)}</span>`
          : '';
        const imgHtml = c.photo_url ? `<img src="${c.photo_url}" alt="${c.title}" loading="lazy">` : '';
        return `
          <div class="card">
            ${imgHtml}
            <a href="${c.url}" target="_blank">${c.title}</a>
            <div class="meta">${c.gender} &middot; ${c.garment_type} &middot; ${c.color || ''}</div>
            <div class="price">${salePrice} ${listPriceHtml}</div>
            <div class="sizes">${sizesHtml}</div>
          </div>`;
      }).join('');
      el.innerHTML = html;
      if (cards.length > MAX_CARDS) {
        el.innerHTML += `<p style="grid-column:1/-1;color:#888">Showing first ${MAX_CARDS} of ${cards.length} — narrow the filters to see more.</p>`;
      }
    }

    loadDeals();
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5050)
