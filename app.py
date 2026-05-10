import os
import requests
from flask import Flask, request, jsonify, send_from_directory
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

import time
_cache = {}  # key: normalized query, value: (timestamp, result)
CACHE_TTL = 1800  # 30 minutes

def get_cached(query):
    key = query.lower().strip()
    if key in _cache:
        ts, result = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return result
    return None

def set_cached(query, result):
    key = query.lower().strip()
    _cache[key] = (time.time(), result)


app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
client = Anthropic()

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
KEEPA_API_KEY = os.environ.get('KEEPA_API_KEY', '')
LINKUP_API_KEY = os.environ.get('LINKUP_API_KEY', '')

def lookup_upc(upc):
    try:
        r = requests.get(f'https://api.upcitemdb.com/prod/trial/lookup?upc={upc}', timeout=5)
        data = r.json()
        if data.get('items'):
            item = data['items'][0]
            name = item.get('title', '')
            brand = item.get('brand', '')
            return {
                'found': True,
                'name': name,
                'brand': brand,
                'search_query': f'{brand} {name}'.strip(),
                'upc': upc
            }
    except Exception:
        pass
    return {'found': False}

def identify_with_claude(image_b64):
    if not ANTHROPIC_API_KEY:
        return {'error': 'Anthropic API key not configured'}
    try:
        if ',' in image_b64:
            image_b64 = image_b64.split(',', 1)[1]

        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=300,
            temperature=0,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': image_b64}
                    },
                    {
                        'type': 'text',
                        'text': '''Identify this product for resale pricing. Reply in EXACTLY this format:
PRODUCT: [full product name including size or quantity]
BRAND: [brand name]
SEARCH: [search query to find this item on Amazon or eBay - brand + product + size, no condition words]

Example:
PRODUCT: Speed Stick Regular Deodorant 3oz
BRAND: Mennen
SEARCH: Speed Stick Regular Deodorant 3oz'''
                    }
                ]
            }]
        )
        text = response.content[0].text.strip()
        product = ''
        brand = ''
        search = ''
        for line in text.split('\n'):
            if line.startswith('PRODUCT:'):
                product = line[8:].strip()
            elif line.startswith('BRAND:'):
                brand = line[6:].strip()
            elif line.startswith('SEARCH:'):
                search = line[7:].strip()
        return {
            'found': True,
            'name': product or text.split('\n')[0][:80],
            'brand': brand,
            'search_query': search or product,
            'source': 'claude_vision'
        }
    except Exception as e:
        return {'error': str(e)}

def get_keepa_data(upc):
    if not KEEPA_API_KEY:
        return {'configured': False}
    try:
        r = requests.get(
            'https://api.keepa.com/product',
            params={'key': KEEPA_API_KEY, 'domain': 1, 'code': upc, 'stats': 90, 'history': 0},
            timeout=10
        )
        data = r.json()
        if not data.get('products'):
            return {'configured': True, 'found': False}

        product = data['products'][0]
        stats = product.get('stats', {})

        def price(val):
            return round(val / 100, 2) if val and val != -1 else None

        current = stats.get('current', [])
        buy_box = price(current[18]) if len(current) > 18 else None
        amazon_price = price(current[0]) if len(current) > 0 else None
        new_3p_price = price(current[1]) if len(current) > 1 else None

        avg90 = stats.get('avg90', [])
        avg_new = price(avg90[1]) if len(avg90) > 1 else None

        return {
            'configured': True,
            'found': True,
            'title': product.get('title', ''),
            'asin': product.get('asin', ''),
            'amazon_price': amazon_price,
            'new_3p_price': new_3p_price,
            'buy_box': buy_box,
            'avg_new_90d': avg_new,
        }
    except Exception as e:
        return {'configured': True, 'error': str(e)}

def get_linkup_data(query):
    if not LINKUP_API_KEY:
        return {'configured': False}
    try:
        r = requests.post(
            'https://api.linkup.so/v1/search',
            headers={'Authorization': f'Bearer {LINKUP_API_KEY}', 'Content-Type': 'application/json'},
            json={
                'q': f'{query} retail price Amazon eBay sold listing resale value',
                'depth': 'standard',
                'outputType': 'sourcedAnswer'
            },
            timeout=15
        )
        data = r.json()
        return {
            'configured': True,
            'answer': data.get('answer', ''),
            'sources': [{'name': s.get('name'), 'url': s.get('url')} for s in data.get('sources', [])[:4]]
        }
    except Exception as e:
        return {'configured': True, 'error': str(e)}

def analyze_verdict(product_name, linkup_answer, cost_paid):
    """Use Claude to extract key prices and generate a verdict from Linkup research."""
    if not ANTHROPIC_API_KEY or not linkup_answer:
        return None
    try:
        cost_str = f'${cost_paid}' if cost_paid else 'unknown'
        prompt = f'''Product: {product_name}
Thrift store cost: {cost_str}
Price research: {linkup_answer}

Based on this research, reply in EXACTLY this format:
RETAIL: [retail/new price, e.g. $12.99, or "unknown"]
RESALE: [what it sells for on eBay/resale, e.g. $18.00, or "no market"]
VERDICT: [BUY or PASS or MAYBE]
REASON: [one sentence - why buy or pass, mention profit potential if known]'''

        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=150,
            temperature=0,
            messages=[{'role': 'user', 'content': prompt}]
        )
        text = response.content[0].text.strip()
        retail = ''
        resale = ''
        verdict = ''
        reason = ''
        for line in text.split('\n'):
            if line.startswith('RETAIL:'):
                retail = line[7:].strip()
            elif line.startswith('RESALE:'):
                resale = line[7:].strip()
            elif line.startswith('VERDICT:'):
                verdict = line[8:].strip().upper()
            elif line.startswith('REASON:'):
                reason = line[7:].strip()
        return {'retail': retail, 'resale': resale, 'verdict': verdict, 'reason': reason}
    except Exception:
        return None

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/scan', methods=['POST'])
def scan():
    data = request.get_json()
    image_b64 = data.get('image', '')
    upc = data.get('upc', '')
    cost_paid = data.get('cost_paid', 0)

    result = {'identification': {}, 'keepa': {}, 'linkup': {}, 'verdict': {}, 'margin': {}}

    # Identify item
    if upc:
        upc_result = lookup_upc(upc)
        if upc_result['found']:
            result['identification'] = upc_result
            search_query = upc_result.get('search_query', '')
        else:
            vision = identify_with_claude(image_b64)
            result['identification'] = vision
            search_query = vision.get('search_query') or ''
    elif image_b64:
        vision = identify_with_claude(image_b64)
        result['identification'] = vision
        search_query = vision.get('search_query') or ''
    else:
        return jsonify({'error': 'No image or UPC provided'}), 400

    # Get pricing data
    if upc:
        result['keepa'] = get_keepa_data(upc)
    else:
        result['keepa'] = {'configured': bool(KEEPA_API_KEY), 'found': False}

    # Check cache for Linkup + verdict (keyed on normalized search_query)
    cached = get_cached(search_query) if search_query else None
    if cached:
        result['linkup'] = cached['linkup']
        result['verdict'] = cached['verdict']
    else:
        result['linkup'] = get_linkup_data(search_query)

    # Margin from Keepa if available (always computed fresh — cost_paid may differ)
    sell_price = result['keepa'].get('buy_box') or result['keepa'].get('avg_new_90d')
    if sell_price and cost_paid:
        fees = sell_price * 0.15
        shipping = 4.00
        net = sell_price - fees - shipping - float(cost_paid)
        result['margin'] = {
            'sell_price': sell_price,
            'cost_paid': float(cost_paid),
            'estimated_fees': round(fees, 2),
            'estimated_shipping': shipping,
            'net_profit': round(net, 2),
            'roi_pct': round((net / float(cost_paid)) * 100, 1) if cost_paid else None
        }

    # AI verdict from Linkup research (always, even without barcode)
    if not cached:
        product_name = result['identification'].get('name', search_query)
        linkup_answer = result['linkup'].get('answer', '')
        if linkup_answer:
            ai_verdict = analyze_verdict(product_name, linkup_answer, cost_paid)
            if ai_verdict:
                result['verdict'] = ai_verdict
        # Store linkup + verdict in cache for future identical queries
        if search_query:
            set_cached(search_query, {'linkup': result['linkup'], 'verdict': result['verdict']})

    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
