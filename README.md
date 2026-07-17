# Woot Size Finder

Filter Woot's apparel and shoe deals (Sports & Outdoors, plus the Baby/Boys'/Girls' Apparel
subcategories buried in Home & Kitchen) by gender, garment type, and size — only showing sizes
that are actually in stock right now. Filters are faceted: picking a type narrows the size
options down to sizes that actually exist for that type, and vice versa.

## How it works

Woot's own site loads its deal listings from a public GraphQL API: an AWS AppSync endpoint
fronted by CloudFront, called with a client-side API key that ships in Woot's JS bundle (the
same key every visitor's browser uses to load woot.com). That API returns, per offer, a list of
purchasable items — each with Color/Size/Gender attributes plus live SoldOut/Quantity — which is
exactly the "what's available in my size" data this tool needs. No HTML scraping involved.

This isn't an official or documented API. Woot could change it at any time without notice.

## Run locally

```
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5050.

## Deploy

Includes a `render.yaml` for one-click deploy on [Render](https://render.com)'s free tier:
push this repo to GitHub, then create a new Blueprint on Render pointing at it.

The `Procfile` (`gunicorn app:app --workers 1 --timeout 120`) also works on most other Python
PaaS hosts (Railway, Fly.io, Heroku-compatible platforms, etc.). The long timeout matters: a
full scrape across categories takes 15-20s, longer than gunicorn's default 30s budget leaves
much room for. The app also warms its cache in a background thread on boot so that scrape
doesn't block the process from binding its port or answering the first real request.

## Known limitations

- Covers the "Sports & Outdoors" and "Home & Kitchen" categories, which is where Woot files
  its apparel/shoe subcategories today — worth double-checking if that changes. The
  print-on-demand "Shirt" tee shop (shirt.woot.com) is deliberately excluded: its handful of
  designs come in every size/color at effectively infinite stock, which drowned out real deals.
- Garment type (Tops/Pants/Shoes/...) is guessed from the title since Woot doesn't tag it.
  For the Home & Kitchen category specifically, items that don't match a known garment
  keyword are dropped entirely rather than shown as "Other" — otherwise perfume, jewelry, and
  watches leak in, since those also carry Size + Gender-ish attributes.
- Data is cached in memory for 15 minutes and refetched fully on cold start.
