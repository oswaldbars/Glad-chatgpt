"""Render-ready TradingView Strong-Buy Scraper + Telegram Notifier (JSON API only)

- Fixed configuration (hardcoded): TradingView URL, Telegram bot token, chat id, 5-minute interval.
- Lightweight scraping using requests + BeautifulSoup only (no Selenium).
- APScheduler runs the scrape every 5 minutes.
- Endpoints:
  /        -> status summary
  /results -> JSON list of last scan (array of found Strong-Buy items)
  /health  -> returns OK (200)
"""

import time
import logging
import re
from datetime import datetime
from typing import List, Dict
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------- Fixed Configuration ----------------------
TELEGRAM_BOT_TOKEN = '8195507774:AAGceiXafAcNrzjs9o8j8wr9B-amR4cJX-g'
TELEGRAM_CHAT_ID = '1735382824'
TRADINGVIEW_URL = 'https://www.tradingview.com/cex-screener/'
SCRAPE_INTERVAL_SECONDS = 300  # 5 minutes

TELEGRAM_API_URL = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'

# ---------------------- Logging ----------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger('tv-scraper-render')

# ---------------------- State ----------------------
last_sent = set()  # avoid duplicate notifications (symbol|date)
last_run_results: List[Dict] = []
last_run_time: float = 0.0

app = Flask(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/115.0 Safari/537.36'
}

def send_telegram_message(text: str) -> bool:
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'HTML'
    }
    try:
        resp = requests.post(TELEGRAM_API_URL, data=payload, timeout=15)
        resp.raise_for_status()
        logger.info('Sent Telegram message')
        return True
    except Exception as e:
        logger.exception('Failed to send Telegram message: %s', e)
        return False

def parse_rows_from_soup(soup: BeautifulSoup):
    """Find rows or containers that include 'Strong Buy' and extract symbol, 24h change and 24h volume when possible."""
    results = []

    # Look for any text nodes containing 'Strong Buy'
    candidates = soup.find_all(string=re.compile(r'Strong\s*Buy', re.I))
    for node in candidates:
        # climb to a reasonable parent that may contain the row's data
        parent = node
        for _ in range(5):
            if parent is None:
                break
            if parent.name in ('tr', 'div', 'li', 'section'):
                break
            parent = parent.parent
        if parent is None:
            continue

        text = parent.get_text(separator='|', strip=True)
        # try to extract a symbol-like token (e.g., BTCUSDT or BTC/USDT)
        m_sym = re.search(r'([A-Z0-9_\-]{2,20}\/?USDT|[A-Z0-9_\-]{2,20})', text)
        symbol = m_sym.group(0) if m_sym else (text.split('|')[0] if text else 'unknown')

        # try to extract percent change (first %-looking token)
        m_change = re.search(r'(-?\d{1,3}(?:[\.,]\d+)?\s*%)', text)
        change = m_change.group(1) if m_change else ''

        # try to extract volume-like token (K, M, B or raw number)
        m_vol = re.search(r'(\d+[\.,]?\d*\s*(?:K|M|B)?)', text[::-1])  # reverse search fallback
        # a simpler search for volume: take last numeric token that looks like volume
        vols = re.findall(r'\d+[\.,]?\d*\s*(?:K|M|B)?', text)
        volume = vols[-1] if vols else ''

        results.append({
            'symbol': symbol,
            'change_24h': change,
            'volume_24h': volume,
            'context': text
        })

    # deduplicate by symbol, keep first occurrence
    seen = {}
    for r in results:
        k = r.get('symbol', '') or r.get('context', '')[:30]
        if k not in seen:
            seen[k] = r
    return list(seen.values())

def lightweight_scrape(url: str):
    logger.info('Scraping: %s', url)
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    html = resp.text
    soup = BeautifulSoup(html, 'lxml')
    return parse_rows_from_soup(soup)

def scrape_tradingview_and_notify():
    global last_sent, last_run_results, last_run_time
    logger.info('Scheduled scrape running...')
    try:
        items = []
        try:
            items = lightweight_scrape(TRADINGVIEW_URL)
        except Exception as e:
            logger.exception('Lightweight scrape failed: %s', e)
            items = []

        # defensive filter: ensure 'Strong Buy' present in context
        filtered = [it for it in items if re.search(r'Strong\s*Buy', it.get('context',''), re.I)]

        now_date = datetime.utcnow().date().isoformat()
        new_notified = []
        for it in filtered:
            symbol = it.get('symbol', 'unknown')
            key = f"{symbol}|{now_date}"
            if key in last_sent:
                continue
            change = it.get('change_24h', '')
            vol = it.get('volume_24h', '')
            msg = f"<b>{symbol}</b> — <i>Strong Buy</i>\n24h Change: {change}\n24h Volume: {vol}\nSource: {TRADINGVIEW_URL}"
            ok = send_telegram_message(msg)
            if ok:
                last_sent.add(key)
                new_notified.append(it)

        last_run_results = filtered
        last_run_time = time.time()
        logger.info('Scrape complete: %d found, %d new notified', len(filtered), len(new_notified))
    except Exception as e:
        logger.exception('Error during scheduled scrape: %s', e)

# ---------------------- Scheduler ----------------------
scheduler = BackgroundScheduler()
scheduler.add_job(scrape_tradingview_and_notify, 'interval', seconds=SCRAPE_INTERVAL_SECONDS)
scheduler.start()

# run immediate first scrape asynchronously-ish
import threading
threading.Thread(target=scrape_tradingview_and_notify, daemon=True).start()

# ---------------------- Flask endpoints ----------------------
@app.route('/')
def index():
    return jsonify({
        'status': 'ok',
        'last_run': datetime.utcfromtimestamp(last_run_time).isoformat() + 'Z' if last_run_time else None,
        'found_count': len(last_run_results)
    })

@app.route('/results')
def results():
    return jsonify({
        'last_run': datetime.utcfromtimestamp(last_run_time).isoformat() + 'Z' if last_run_time else None,
        'results': last_run_results
    })

@app.route('/health')
def health():
    return 'OK', 200

if __name__ == '__main__':
    try:
        app.run(host='0.0.0.0', port=int(5000))
    finally:
        scheduler.shutdown()
