import os
import base64
import re
import requests
from flask import Flask, request, jsonify, send_from_directory
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32MB max upload
client = Anthropic()

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
KEEPA_API_KEY = os.environ.get('KEEPA_API_KEY', '')
LINKUP_API_KEY = os.environ.get('LINKUP_API_KEY', '')

def lookup_upc(upc):
    """Look up product info from UPC using UPCitemdb."""
    try:
        r = requests.get(f'https://api.upcitemdb.com/prod/trial/lookup?upc={upc}', timeout=5)
        data = r.json()
        if data.get('items'):
            item = data['items'][0]
            return {
                'found': True,
                'name': item.get('title', ''),
                'brand': item.get('brand', ''),
                'description': item.get('description', ''),
                'upc': upc
            }
    except Exception:
        pass
    return {'found': False}

def identify_with_claude(image_b64):
    """Use Claude Haiku vision to identify the item."""
    if not ANTHROPIC_API_KEY:
        return {'error': 'Anthropic API key not configured'}
    try:
        # Strip data URL prefix if present
        if ',' in image_b64:
            image_b64 = image_b64.split(',', 1)[1]

        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=512,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {
                            'type': 'base64',
                            'media_type': 'image/jpeg',
                            'data': image_b64
                        }
                    },
                    {
                        'type': 'text',
                        'text': 'Identify this item precisely. Give me: (1) Product name, (2) Brand, (3) Model/version if visible, (4) Condition (new/used/sealed), (5) A short search query I could use to find sold prices on eBay. Be specific and concise.'
                    }
                ]
            }]
        )
        text = response.content[0].text
        return {'found': True, 'description': text, 'source': 'claude_vision'}
    except Exception as e:
        return {'error': str(e)}

def get_keepa_data(upc):
    """Get Amazon pricing and sales rank history from Keepa."""
    if not KEEPA_API_KEY:
        return {'configured': False, 'message': 'Keepa not configured — add KEEPA_API_KEY to use Amazon pricing data'}
    try:
        # Search by UPC
        r = requests.get(
            'https://api.keepa.com/product',
            params={
                'key': KEEPA_API_KEY,
                'domain': 1,  # amazon.com
                'code': upc,
                'stats': 90,
                'history': 0
            },
            timeout=10
        )
        data = r.json()
        if not data.get('products'):
            return {'configured': True, 'found': False}

        product = data['products'][0]
        stats = product.get('stats', {})
        csv = product.get('csv', [])

        # Extract current prices (Keepa stores prices as integers * 100, -1 = unavailable)
        def price(val):
            return round(val / 100, 2) if val and val != -1 else None

        current = stats.get('current', [])
        amazon_price = price(current[0]) if len(current) > 0 else None
        new_3p_price = price(current[1]) if len(current) > 1 else None
        used_price = price(current[2]) if len(current) > 2 else None
        buy_box = price(current[18]) if len(current) > 18 else None

        avg90 = stats.get('avg90', [])
        avg_new = price(avg90[1]) if len(avg90) > 1 else None
        avg_used = price(avg90[2]) if len(avg90) > 2 else None

        return {
            'configured': True,
            'found': True,
            'title': product.get('title', ''),
            'asin': product.get('asin', ''),
            'amazon_price': amazon_price,
            'new_3p_price': new_3p_price,
            'used_price': used_price,
            'buy_box': buy_box,
            'avg_new_90d': avg_new,
            'avg_used_90d': avg_used,
            'sales_rank': product.get('salesRanks', {})
        }
    except Exception as e:
        return {'configured': True, 'error': str(e)}

def get_linkup_data(query):
    """Search for sold prices using Linkup API."""
    if not LINKUP_API_KEY:
        return {'configured': False, 'message': 'Linkup not configured — add LINKUP_API_KEY to use web price search'}
    try:
        r = requests.post(
            'https://api.linkup.so/v1/search',
            headers={'Authorization': f'Bearer {LINKUP_API_KEY}', 'Content-Type': 'application/json'},
            json={
                'q': f'{query} sold price eBay Poshmark Mercari resale value',
                'depth': 'standard',
                'outputType': 'sourcedAnswer'
            },
            timeout=15
        )
        data = r.json()
        return {
            'configured': True,
            'answer': data.get('answer', ''),
            'sources': [{'name': s.get('name'), 'url': s.get('url')} for s in data.get('sources', [])[:5]]
        }
    except Exception as e:
        return {'configured': True, 'error': str(e)}

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
    upc = data.get('upc', '')  # client-side barcode detection result
    cost_paid = data.get('cost_paid', 0)

    result = {
        'identification': {},
        'keepa': {},
        'linkup': {},
        'margin': {}
    }

    # Step 1: Identify the item
    if upc:
        upc_result = lookup_upc(upc)
        if upc_result['found']:
            result['identification'] = upc_result
            search_query = f"{upc_result.get('brand', '')} {upc_result.get('name', '')}".strip()
        else:
            # UPC lookup failed, fall back to vision
            vision = identify_with_claude(image_b64)
            result['identification'] = vision
            search_query = vision.get('description', '')[:200]
    elif image_b64:
        vision = identify_with_claude(image_b64)
        result['identification'] = vision
        search_query = vision.get('description', '')[:200]
    else:
        return jsonify({'error': 'No image or UPC provided'}), 400

    # Step 2: Get pricing data
    if upc:
        result['keepa'] = get_keepa_data(upc)
    else:
        result['keepa'] = {'configured': bool(KEEPA_API_KEY), 'found': False, 'note': 'Keepa requires UPC/barcode'}

    result['linkup'] = get_linkup_data(search_query)

    # Step 3: Margin calculation
    sell_price = None
    if result['keepa'].get('buy_box'):
        sell_price = result['keepa']['buy_box']
    elif result['keepa'].get('avg_new_90d'):
        sell_price = result['keepa']['avg_new_90d']

    if sell_price and cost_paid:
        fees = sell_price * 0.15  # ~15% platform fees estimate
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

    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
