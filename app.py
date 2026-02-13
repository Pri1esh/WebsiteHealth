import threading
import time
import requests
import pandas as pd
import os
from flask import Flask, render_template, jsonify, request
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium_stealth import stealth
import urllib3
from flask_cors import CORS  # Add this
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings()
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Global shared state
CHECK_INTERVAL = 15 * 60

monitoring_results = {
    'total': 0,
    'checked': 0,
    'failed': [],
    'last_check': None,
    'is_running': False,
    'retry_in_progress': False
}

results_lock = threading.Lock()


def load_websites_from_excel():
    """Load websites from Excel"""
    try:
        possible_paths = [
            os.path.join(os.path.dirname(__file__), 'Adani-BUWise-Websites.xlsx'),
            'Adani-BUWise-Websites.xlsx',
            'upload/Adani-BUWise-Websites.xlsx'
        ]

        df = None
        for path in possible_paths:
            if os.path.exists(path):
                df = pd.read_excel(path)
                break

        if df is None:
            return get_demo_websites()

        websites = []
        for _, row in df.iterrows():
            bu = str(row.get('BU', '')).strip()
            cell = str(row.get('Websites', '')).strip()

            if not cell or cell.lower() in ['nan', 'none']:
                continue

            cell = cell.replace('\r\n', '\n').replace('\r', '\n')
            raw_urls = []

            for part in cell.split('\n'):
                raw_urls.extend([u.strip() for u in part.split(',') if u.strip()])

            for url in raw_urls:
                if not url.startswith(('http://', 'https://')):
                    url = 'https://' + url
                url = url.replace(' ', '').rstrip('/')

                websites.append({
                    'bu': bu,
                    'url': url,
                    'name': url.replace('https://', '').replace('http://', '').replace('www.', '')
                })

        return websites
    except Exception as e:
        print("Error reading Excel:", e)
        return get_demo_websites()


def check_website(site_info):
    """Check website - fast method first, Selenium fallback if blocked"""
    import requests
    import urllib3
    urllib3.disable_warnings()

    url = site_info['url']

    # Step 1: Try fast requests method
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        for attempt in range(3):  # retry 3 times internally
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=20,  # increased from 10 → 20
                    verify=False
                )
                break  # success, exit retry loop
            except requests.exceptions.Timeout:
                if attempt == 2:
                    raise
                time.sleep(2)  # small wait before retry

        # SUCCESS: 2xx or 3xx (redirects)
        if 200 <= response.status_code < 400:
            return {
                'success': True,
                'status_code': response.status_code,
                'url': url,
                'bu': site_info['bu'],
                'name': site_info['name'],
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'method': 'fast'
            }

        # CLIENT ERRORS: 4xx (except some special cases)
        # 403 Forbidden = FAIL (site is blocking us, but we can't access it)
        # 401 Unauthorized = FAIL
        # 404 Not Found = FAIL
        # 405 Method Not Allowed = try GET instead of HEAD, but still fail if persists

        if response.status_code in [403, 401, 404, 405, 406, 407, 408, 409, 410, 429]:
            return {
                'success': False,
                'status_code': response.status_code,
                'url': url,
                'bu': site_info['bu'],
                'name': site_info['name'],
                'error': f'HTTP {response.status_code} - {"Forbidden" if response.status_code == 403 else "Client Error"}',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

        # SERVER ERRORS: 5xx (site is down)
        if response.status_code >= 500:
            return {
                'success': False,
                'status_code': response.status_code,
                'url': url,
                'bu': site_info['bu'],
                'name': site_info['name'],
                'error': f'HTTP {response.status_code} - Server Error',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

    except requests.exceptions.Timeout:
        return {
            'success': False,
            'status_code': 0,
            'url': url,
            'bu': site_info['bu'],
            'name': site_info['name'],
            'error': 'Timeout',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
    except Exception as e:
        # Continue to Selenium for connection errors, SSL errors, etc.
        pass

    # Step 2: Use Selenium for sites that might need JavaScript rendering
    # BUT: We should NOT use Selenium for 403 errors - if requests got 403,
    # Selenium will likely also be blocked or get a challenge page

    print(f"   Trying Selenium for: {site_info['name']}")

    try:
        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)

        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options
        )

        stealth(driver,
                languages=["en-US", "en"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True)

        driver.set_page_load_timeout(25)
        driver.get(url)

        # Check if we hit a cloudflare/verification page
        page_title = driver.title.lower()
        page_source = driver.page_source.lower()

        # Common indicators of being blocked
        blocked_indicators = [
            'access denied', '403 forbidden', 'blocked',
            'cloudflare', 'captcha', 'verification',
            'security check', 'ddos protection'
        ]

        is_blocked = any(indicator in page_title or indicator in page_source
                         for indicator in blocked_indicators)

        if is_blocked:
            driver.quit()
            return {
                'success': False,
                'status_code': 403,
                'url': url,
                'bu': site_info['bu'],
                'name': site_info['name'],
                'error': 'Blocked by WAF/Cloudflare',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'method': 'selenium-blocked'
            }

        title = driver.title
        driver.quit()

        return {
            'success': True,
            'status_code': 200,
            'url': url,
            'bu': site_info['bu'],
            'name': site_info['name'],
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'method': 'selenium',
            'title': title[:30]
        }

    except Exception as e:
        try:
            driver.quit()
        except:
            pass

        return {
            'success': False,
            'status_code': 0,
            'url': url,
            'bu': site_info['bu'],
            'name': site_info['name'],
            'error': 'Selenium failed',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }


def get_demo_websites():
    return [{'bu': 'Demo', 'url': 'https://www.google.com', 'name': 'google.com'}]


def monitor_websites():
    """Main monitoring loop with multithreading"""
    global monitoring_results

    monitoring_results['is_running'] = True

    while monitoring_results['is_running']:
        websites = load_websites_from_excel()

        with results_lock:
            monitoring_results['total'] = len(websites)
            monitoring_results['checked'] = 0

        print(f"\n🔍 Checking {len(websites)} websites using multithreading...")

        # Use thread pool for parallel website checks
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(check_website, site): site
                for site in websites
            }

            for i, future in enumerate(as_completed(futures), start=1):
                if not monitoring_results['is_running']:
                    break

                try:
                    result = future.result()
                except Exception as e:
                    print("Thread error:", e)
                    continue

                with results_lock:
                    monitoring_results['checked'] = i

                    if not result['success']:
                        existing = next(
                            (f for f in monitoring_results['failed']
                             if f['url'] == result['url']),
                            None
                        )
                        if not existing:
                            result['retry_count'] = 0
                            monitoring_results['failed'].append(result)
                    else:
                        # Remove recovered sites from failed list
                        monitoring_results['failed'] = [
                            f for f in monitoring_results['failed']
                            if f['url'] != result['url']
                        ]

        with results_lock:
            monitoring_results['last_check'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        print(f"✅ Cycle done. Failed: {len(monitoring_results['failed'])}")

        # Wait CHECK_INTERVAL seconds before next cycle
        sleep_seconds = CHECK_INTERVAL
        while sleep_seconds > 0 and monitoring_results['is_running']:
            time.sleep(1)
            sleep_seconds -= 1

    print("🛑 Monitoring stopped")


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/start', methods=['POST'])
def start_monitoring():
    if not monitoring_results['is_running']:
        t = threading.Thread(target=monitor_websites, daemon=True)
        t.start()
        return jsonify({'status': 'started'})
    return jsonify({'status': 'already_running'})


@app.route('/api/stop', methods=['POST'])
def stop_monitoring():
    monitoring_results['is_running'] = False
    return jsonify({'status': 'stopped'})


@app.route('/api/status')
def status():
    """Return current status - same for all users"""
    with results_lock:
        return jsonify({
            'total': monitoring_results['total'],
            'checked': monitoring_results['checked'],
            'failed': [f.copy() for f in monitoring_results['failed']],
            'last_check': monitoring_results['last_check'],
            'is_running': monitoring_results['is_running']
        })


@app.route('/api/retry', methods=['POST'])
def retry_website():
    """Retry single website"""
    global monitoring_results

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No JSON data'}), 400

    url = data.get('url')
    if not url:
        return jsonify({'success': False, 'error': 'No URL provided'}), 400

    with results_lock:
        site_index = None
        site_info = None

        for i, site in enumerate(monitoring_results['failed']):
            if site['url'] == url:
                site_index = i
                site_info = site
                break

        if site_index is None:
            return jsonify({'success': False, 'error': 'Site not found'}), 404

        retry_count = site_info.get('retry_count', 0)

    # Perform retry outside lock
    print(f"🔄 Retrying: {url} (attempt {retry_count + 1})")
    result = check_website(site_info)

    with results_lock:
        monitoring_results['retry_in_progress'] = False

        if result['success']:
            monitoring_results['failed'].pop(site_index)
            print(f"   ✅ Success! Removed from failed list.")
            return jsonify({
                'success': True,
                'message': 'Website is accessible',
                'failed_count': len(monitoring_results['failed'])
            })
        else:
            site_info['retry_count'] = retry_count + 1
            site_info['last_retry'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            site_info['last_error'] = result.get('error', 'Unknown')
            print(f"   ❌ Failed. Count: {site_info['retry_count']}")
            return jsonify({
                'success': False,
                'error': result.get('error', 'Check failed'),
                'retry_count': site_info['retry_count'],
            })


@app.route('/api/retry-all', methods=['POST'])
def retry_all_failed():
    """Retry all failed websites"""
    global monitoring_results

    with results_lock:
        failed_sites = [f.copy() for f in monitoring_results['failed']]
        monitoring_results['retry_in_progress'] = True

    if not failed_sites:
        with results_lock:
            monitoring_results['retry_in_progress'] = False
        return jsonify({'success': True, 'message': 'No failed sites', 'results': []})

    results = []

    for site in failed_sites:
        retry_count = site.get('retry_count', 0)

        result = check_website(site)

        with results_lock:
            if result['success']:
                monitoring_results['failed'] = [f for f in monitoring_results['failed'] if f['url'] != site['url']]
                results.append({'url': site['url'], 'success': True})
            else:
                for f in monitoring_results['failed']:
                    if f['url'] == site['url']:
                        f['retry_count'] = retry_count + 1
                        f['last_retry'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        break
                results.append({'url': site['url'], 'success': False})

        time.sleep(0.5)

    with results_lock:
        monitoring_results['retry_in_progress'] = False

    successful = sum(1 for r in results if r.get('success'))

    return jsonify({
        'success': True,
        'total': len(failed_sites),
        'successful': successful,
        'failed': len(failed_sites) - successful,
        'remaining_failed': len(monitoring_results['failed'])
    })


if __name__ == '__main__':
    print("=" * 60)
    print("Adani Website Health Monitor")
    print("=" * 60)

    # Auto-start monitoring
    if not monitoring_results['is_running']:
        t = threading.Thread(target=monitor_websites, daemon=True)
        t.start()
        print("🚀 Auto-started monitoring")

    # Run with threading enabled
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)