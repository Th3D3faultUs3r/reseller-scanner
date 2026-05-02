import os
import base64
import json
import re
import io
import requests
from flask import Flask, request, jsonify, send_from_directory
from PIL import Image
import anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static')

ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
KEEPA_API_KEY = os.getenv('KEEPA_API_KEY')
LINKUP_API_KEY = os.getenv('LINKUP_API_KEY')

# Optional: pyzbar for server-side barcode decoding
try:
    from pyzbar.pyzbar import decode as pyzbar_decode
    PYZBAR_AVAILABLE = True
except ImportError:
    PYZBAR_AVAILABLE = False


# ---------------------------------------------------------------------------
# Barcode extraction
# ---------------------------------------------------------------------------

def extract_barcode_from_image(image_bytes):
    """Attempt to decode a barcode/QR code from raw image bytes using pyzbar."""
    if not PYZBAR_AVAILABLE:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
        results = pyzbar_decode(img)
        for r in results:
            code = r.data.decode('utf-8').strip()
            if code:
                return code
    except Exception:
        pass
    return None


def lookup_upc(upc):
    """Query UPCitemdb for product details."""
    try:
        resp = requests.get(
            'https://api.upcitemdb.com/prod/trial/lookup',
            params={'upc': upc},
            timeout=6
        )
        data = resp.json()
        items = data.get('items', [])
        if items:
            item = items[0]
            return {
                'source': 'barcode',
                'upc': upc,
                'title': item.get('title', ''),
                'brand': item.get('brand', ''),
                'category': item.get('category', ''),
                'description': item.get('description', ''),
                'asin': item.get('asin', '') or '',
                'confidence': 'High',
            }
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Claude Haiku vision identification
# ---------------------------------------------------------------------------

def identify_with_claude(image_bytes, content_type='image/jpeg'):
    """Use Claude Haiku to identify an item from a photo."""
    if not ANTHROPIC_API_KEY:
        return {'error': 'ANTHROPIC_API_KEY not configured', 'source': 'claude_vision'}

    # Normalise media type
    media_type = content_type if content_type.startswith('image/') else 'image/jpeg'
    if media_type not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
        media_type = 'image/jpeg'

    b64 = base64.standard_b64encode(image_bytes).decode('utf-8')

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = (
        'You are an expert resale item identifier. Examine this photo carefully and respond '
        'with JSON only — no other text.\n\n'
        'Return exactly this structure:\n'
        '{\n'
        '  "title": "full product name including model/size/quantity",\n'
        '  "brand": "brand name, or Unknown",\n'
        '  "category": "product category",\n'
        '  "condition": "New / Like New / Used / Unknown",\n'
        '  "confidence": "High / Medium / Low",\n'
        '  "search_query": "best eBay/Amazon search query to find sold prices",\n'
        '  "asin": "Amazon ASIN if visible or known, else empty string",\n'
        '  "notes": "any detail useful for resale: quantity, colour, sealed/open, etc."\n'
        '}\n\n'
        'Be specific. Include model numbers, sizes, and counts when visible.'
    )

    message = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=512,
        messages=[{
            'role': 'user',
            'content': [
                {
                    'type': 'image',
                    'source': {
                        'type': 'base64',
                        'media_type': media_type,
                        'data': b64,
                    }
                },
                {'type': 'text', 'text': prompt}
            ]
        }]
    )

    raw = message.content[0].text.strip()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            result['source'] = 'claude_vision'
            return result
        except json.JSONDecodeError:
            pass
    return {'title': raw, 'source': 'claude_vision', 'confidence': 'Low'}


# ---------------------------------------------------------------------------
# Pricing: Keepa (Amazon)
# ---------------------------------------------------------------------------

def _keepa_cents(val):
    """Convert Keepa's internal price format (cents * 100) to dollars."""
    if val and isinstance(val, (int, float)) and val > 0:
        return round(val / 100, 2)
    return None


def get_keepa_data(asin):
    """Fetch Amazon price history and sales rank from Keepa."""
    if not KEEPA_API_KEY:
        return None
    if not asin:
        return None
    try:
        resp = requests.get(
            'https://api.keepa.com/product',
            params={
                'key': KEEPA_API_KEY,
                'domain': 1,       # amazon.com
                'asin': asin,
                'stats': 90,       # include 90-day statistics
                'history': 0,      # skip raw history arrays to keep response small
            },
            timeout=12
        )
        data = resp.json()
        products = data.get('products', [])
        if not products:
            return None

        p = products[0]
        stats = p.get('stats') or {}
        current = stats.get('current') or []
        avg90 = stats.get('avg90') or []

        # current / avg90 are arrays indexed by price type:
        #   0 = Amazon, 1 = Marketplace New, 3 = Sales Rank
        amazon_current = _keepa_cents(current[0]) if len(current) > 0 else None
        new_current    = _keepa_cents(current[1]) if len(current) > 1 else None
        rank_current   = current[3] if len(current) > 3 and isinstance(current[3], int) else None

        amazon_avg90   = _keepa_cents(avg90[0]) if len(avg90) > 0 else None
        new_avg90      = _keepa_cents(avg90[1]) if len(avg90) > 1 else None

        best_price = amazon_avg90 or new_avg90 or amazon_current or new_current

        return {
            'source': 'keepa',
            'asin': asin,
            'title': p.get('title', ''),
            'amazon_price_current': amazon_current,
            'marketplace_new_current': new_current,
            'amazon_price_avg90': amazon_avg90,
            'marketplace_new_avg90': new_avg90,
            'sales_rank_current': rank_current,
            'best_price_estimate': best_price,
        }
    except Exception as e:
        return {'source': 'keepa', 'error': str(e)}


# ---------------------------------------------------------------------------
# Pricing: Linkup web search
# ---------------------------------------------------------------------------

def get_linkup_data(query):
    """Search for sold/resale prices using Linkup API."""
    if not LINKUP_API_KEY:
        return None
    if not query:
        return None
    try:
        resp = requests.post(
            'https://api.linkup.so/v1/search',
            headers={
                'Authorization': f'Bearer {LINKUP_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'q': f'{query} sold price resale eBay Amazon',
                'depth': 'standard',
                'outputType': 'sourcedAnswer',
            },
            timeout=15
        )
        data = resp.json()
        return {
            'source': 'linkup',
            'answer': data.get('answer', ''),
            'sources': [s.get('url', '') for s in data.get('sources', [])[:4]],
        }
    except Exception as e:
        return {'source': 'linkup', 'error': str(e)}


# ---------------------------------------------------------------------------
# Margin calculator
# ---------------------------------------------------------------------------

PLATFORM_FEES = {
    'ebay':     0.1325,
    'amazon':   0.15,
    'mercari':  0.10,
    'poshmark': 0.20,
    'etsy':     0.065,
}

def calculate_margin(cost, sell_price, platform='ebay'):
    """Return net profit, ROI, and a BUY / PASS verdict."""
    if cost is None or sell_price is None:
        return None
    fee_rate = PLATFORM_FEES.get(platform, 0.1325)
    fees = round(sell_price * fee_rate, 2)
    shipping = 5.00  # conservative flat estimate
    net = round(sell_price - fees - shipping - cost, 2)
    roi = round((net / cost) * 100, 1) if cost > 0 else 0
    return {
        'cost': cost,
        'sell_price': sell_price,
        'platform': platform,
        'fees': fees,
        'estimated_shipping': shipping,
        'net_profit': net,
        'roi_percent': roi,
        'verdict': 'BUY' if net > 5 and roi > 30 else 'PASS',
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'anthropic':  'configured' if ANTHROPIC_API_KEY else 'not configured',
        'keepa':      'configured' if KEEPA_API_KEY else 'not configured',
        'linkup':     'configured' if LINKUP_API_KEY else 'not configured',
        'pyzbar':     'available'  if PYZBAR_AVAILABLE else 'not available — install pyzbar + libzbar0',
    })


@app.route('/scan', methods=['POST'])
def scan():
    if 'image' not in request.files:
        return jsonify({'error': 'No image provided'}), 400

    file        = request.files['image']
    image_bytes = file.read()
    cost        = request.form.get('cost', type=float)
    platform    = request.form.get('platform', 'ebay')

    result = {
        'identification': None,
        'barcode': None,
        'pricing': {},
        'margin': None,
    }

    # ------------------------------------------------------------------
    # Step 1: Try barcode extraction (server-side pyzbar)
    # ------------------------------------------------------------------
    upc          = extract_barcode_from_image(image_bytes)
    product_info = None

    if upc:
        result['barcode'] = upc
        product_info = lookup_upc(upc)

    # ------------------------------------------------------------------
    # Step 2: Fall back to Claude Haiku vision
    # ------------------------------------------------------------------
    if not product_info:
        product_info = identify_with_claude(image_bytes, file.content_type or 'image/jpeg')

    result['identification'] = product_info

    # ------------------------------------------------------------------
    # Step 3: Pricing
    # ------------------------------------------------------------------
    asin         = (product_info or {}).get('asin', '')
    search_query = (product_info or {}).get('search_query') or (product_info or {}).get('title', '')

    if asin:
        keepa = get_keepa_data(asin)
        if keepa:
            result['pricing']['amazon'] = keepa

    if search_query:
        linkup = get_linkup_data(search_query)
        if linkup:
            result['pricing']['web'] = linkup

    # ------------------------------------------------------------------
    # Step 4: Margin calc
    # ------------------------------------------------------------------
    if cost is not None:
        sell_price = None
        if result['pricing'].get('amazon', {}).get('best_price_estimate'):
            sell_price = result['pricing']['amazon']['best_price_estimate']
        if sell_price:
            result['margin'] = calculate_margin(cost, sell_price, platform)

    return jsonify(result)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
