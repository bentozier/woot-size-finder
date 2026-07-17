# Woot Size Finder

Filter Woot's Sports & Outdoors deals (apparel + shoes) by gender, garment type, and size —
only showing sizes that are actually in stock right now.

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

The `Procfile` (`gunicorn app:app`) also works on most other Python PaaS hosts (Railway,
Fly.io, Heroku-compatible platforms, etc.).

## Known limitations

- Only covers the "Sports & Outdoors" category, which is where Woot files its apparel/shoe
  subcategories today — worth double-checking if that changes.
- Garment type (Tops/Pants/Shoes/...) is guessed from the title since Woot doesn't tag it.
- Data is cached in memory for 15 minutes and refetched fully on cold start (~12 API calls),
  so the first request after a deploy or restart takes a few seconds.
