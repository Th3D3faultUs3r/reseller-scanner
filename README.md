# Reseller Scanner

Mobile PWA for scanning thrift store items and getting instant resale price data.

## How it works

1. Tap **Scan Item** — takes a photo using your phone camera
2. App attempts barcode detection on the image (server-side via pyzbar)
3. If barcode found → looks up product in UPCitemdb (696M products)
4. If no barcode (or lookup fails) → Claude Haiku vision identifies the item
5. Pulls Amazon price history + sales rank from Keepa
6. Searches web for sold prices via Linkup
7. Calculates net profit and gives a BUY / PASS verdict

---

## Setup (local)

### 1. Install system dependency for barcode scanning

**Mac:**
```
brew install zbar
```
**Linux/Ubuntu:**
```
sudo apt-get install libzbar0
```
**Windows:** Download and install ZBar from https://zbar.sourceforge.net

### 2. Install Python dependencies

```
pip install -r requirements.txt
```

### 3. Set environment variables

Copy `.env` and fill in your API keys:

```
ANTHROPIC_API_KEY=your_key_here
KEEPA_API_KEY=your_key_here
LINKUP_API_KEY=your_key_here
```

### 4. Run locally

```
python app.py
```

Open http://localhost:5000 in your browser.

---

## Deploy to Railway (recommended — free tier available)

Railway gives you HTTPS automatically, which is required for mobile camera access.

### Steps

1. Create a free account at https://railway.app
2. Click **New Project** → **Deploy from GitHub repo**
   - Or use Railway CLI: `railway init` then `railway up`
3. Add environment variables in Railway dashboard → Variables:
   - `ANTHROPIC_API_KEY`
   - `KEEPA_API_KEY`
   - `LINKUP_API_KEY`
4. Railway auto-detects the `Procfile` and deploys
5. Your app gets a public HTTPS URL — open it on your phone

### Alternative: Render.com

1. Connect your GitHub repo at https://render.com
2. Create a new **Web Service**
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`
5. Add env vars in the Render dashboard

---

## API Keys

| Key | Where to get it | Cost |
|-----|----------------|------|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com | Pay-per-use (Haiku is cheap) |
| `KEEPA_API_KEY` | https://keepa.com/#!api | €49/month — **confirm before purchasing** |
| `LINKUP_API_KEY` | https://linkup.so | Free trial available |

**Note on Keepa:** The app works without Keepa — it will skip Amazon price history and fall back to Linkup web search only. Test with Linkup first before buying Keepa.

---

## App endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serves the PWA |
| `/health` | GET | Shows API key status |
| `/scan` | POST | Main scan endpoint |

### /scan request

Form-data fields:
- `image` (file, required) — photo from camera
- `cost` (float, optional) — what you paid, for margin calc
- `platform` (string, optional) — `ebay`, `amazon`, `mercari`, `poshmark` (default: `ebay`)

### /scan response

```json
{
  "identification": {
    "source": "barcode | claude_vision",
    "title": "Product Name",
    "brand": "Brand",
    "category": "Category",
    "condition": "New",
    "confidence": "High | Medium | Low",
    "asin": "B0XXXXXXXX",
    "notes": "...",
    "upc": "012345678901"
  },
  "barcode": "012345678901",
  "pricing": {
    "amazon": {
      "source": "keepa",
      "amazon_price_current": 24.99,
      "marketplace_new_avg90": 22.50,
      "sales_rank_current": 45231,
      "best_price_estimate": 22.50
    },
    "web": {
      "source": "linkup",
      "answer": "This item typically sells for $18-25 on eBay...",
      "sources": ["https://ebay.com/...", "..."]
    }
  },
  "margin": {
    "cost": 2.00,
    "sell_price": 22.50,
    "platform": "ebay",
    "fees": 2.98,
    "estimated_shipping": 5.00,
    "net_profit": 12.52,
    "roi_percent": 626.0,
    "verdict": "BUY"
  }
}
```

---

## Architecture notes

- Barcode detection uses **pyzbar** (server-side) — more reliable than client-side JS for static images
- Vision fallback uses **Claude Haiku** (fast, cheap, accurate for product ID)
- Amazon data via **Keepa** — 90-day avg price + sales rank = sell-through signal
- Web sold prices via **Linkup** — searches eBay, Amazon, etc.
- All API keys stay on the server — never exposed to the browser
- PWA installable on iOS/Android from browser (Add to Home Screen)
