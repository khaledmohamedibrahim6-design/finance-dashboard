"""
Finance Dashboard - Backend Server
Fetches exchange rates, gold prices, and Austrian Mint product prices.
"""

import json
import time
import threading
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request, send_from_directory

app = Flask(__name__, static_folder="static", template_folder="templates")

# --- Cache ---
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 60  # 1 minute - prices refresh on nearly every page open


def _get_cached(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["ts"]) < CACHE_TTL:
            return entry["data"]
    return None


def _set_cached(key, data):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time()}


# --- Headers for scraping ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


# --- Data fetchers ---


def fetch_exchange_rates():
    """Fetch EUR/EGP and USD/EGP from multiple free API sources."""
    cached = _get_cached("exchange_rates")
    if cached:
        return cached

    rates = {}

    # Try primary source: Frankfurter API (ECB data, free, no key)
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "EGP,EUR"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if "rates" in data:
                usd_to_egp = data["rates"].get("EGP")
                usd_to_eur = data["rates"].get("EUR")
                if usd_to_egp:
                    rates["usd_egp"] = round(usd_to_egp, 4)
                if usd_to_eur and usd_to_egp:
                    # EUR to EGP = (1/USD_to_EUR) * USD_to_EGP
                    rates["eur_egp"] = round(usd_to_egp / usd_to_eur, 4)
                rates["exchange_source"] = "European Central Bank"
    except Exception as e:
        print(f"Frankfurter API error: {e}")

    # Fallback: ExchangeRate-API (free tier)
    if "usd_egp" not in rates:
        try:
            resp = requests.get(
                "https://open.er-api.com/v6/latest/USD", timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("result") == "success":
                    egp = data["rates"].get("EGP")
                    eur = data["rates"].get("EUR")
                    if egp:
                        rates["usd_egp"] = round(egp, 4)
                    if eur and egp:
                        rates["eur_egp"] = round(egp / eur, 4)
                    rates["exchange_source"] = "ExchangeRate-API"
        except Exception as e:
            print(f"ExchangeRate-API error: {e}")

    # Also try to get EUR->EGP directly
    if "eur_egp" not in rates:
        try:
            resp = requests.get(
                "https://api.frankfurter.app/latest",
                params={"from": "EUR", "to": "EGP"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if "rates" in data:
                    rates["eur_egp"] = round(data["rates"].get("EGP", 0), 4)
        except Exception as e:
            print(f"EUR/EGP direct fetch error: {e}")

    rates["updated_at"] = datetime.now(timezone.utc).isoformat()

    if rates.get("usd_egp") or rates.get("eur_egp"):
        _set_cached("exchange_rates", rates)
    return rates


def fetch_gold_price():
    """Fetch gold spot price from goldprice.org JSON API."""
    cached = _get_cached("gold_price")
    if cached:
        return cached

    gold_data = {}

    # Primary: goldprice.org direct JSON API (best source, structured data)
    try:
        resp = requests.get(
            "https://data-asg.goldprice.org/dbXRates/USD",
            headers={
                **HEADERS,
                "Referer": "https://goldprice.org/",
                "Origin": "https://goldprice.org",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if "items" in data and data["items"]:
                item = data["items"][0]
                if "xauPrice" in item:
                    gold_data["usd_oz"] = round(item["xauPrice"], 2)
                    gold_data["usd_gram"] = round(item["xauPrice"] / 31.1035, 2)
                    gold_data["change_usd"] = round(item.get("chgXau", 0), 2)
                    gold_data["change_pct"] = round(item.get("pcXau", 0), 2)
                    gold_data["prev_close"] = round(item.get("xauClose", 0), 2)
                    # Silver data as bonus
                    if "xagPrice" in item:
                        gold_data["silver_usd_oz"] = round(item["xagPrice"], 2)
                        gold_data["silver_change_pct"] = round(
                            item.get("pcXag", 0), 2
                        )
                if "date" in data:
                    gold_data["market_time"] = data["date"]
    except Exception as e:
        print(f"goldprice.org API error: {e}")

    # Also fetch EUR gold price for direct comparison
    if "usd_oz" in gold_data:
        try:
            resp = requests.get(
                "https://data-asg.goldprice.org/dbXRates/EUR",
                headers={
                    **HEADERS,
                    "Referer": "https://goldprice.org/",
                    "Origin": "https://goldprice.org",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if "items" in data and data["items"]:
                    item = data["items"][0]
                    if "xauPrice" in item:
                        gold_data["eur_oz"] = round(item["xauPrice"], 2)
                        gold_data["eur_gram"] = round(
                            item["xauPrice"] / 31.1035, 2
                        )
        except Exception as e:
            print(f"goldprice.org EUR API error: {e}")

    # Fallback: free exchange rate API for metals
    if "usd_oz" not in gold_data:
        try:
            resp = requests.get(
                "https://api.metalpriceapi.com/v1/latest",
                params={"api_key": "demo", "base": "USD", "currencies": "XAU"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and "rates" in data:
                    xau = data["rates"].get("XAU") or data["rates"].get("USDXAU")
                    if xau and xau > 0:
                        gold_data["usd_oz"] = round(1.0 / xau, 2)
                        gold_data["usd_gram"] = round(gold_data["usd_oz"] / 31.1035, 2)
        except Exception as e:
            print(f"metalpriceapi fallback error: {e}")

    gold_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    gold_data["source"] = "goldprice.org"

    if gold_data.get("usd_oz"):
        _set_cached("gold_price", gold_data)
    return gold_data


def fetch_mint_20g_bar():
    """Fetch price of 20g gold bar from Austrian Mint."""
    cached = _get_cached("mint_20g")
    if cached:
        return cached

    product = {
        "name": "20 Gram Gold Bar (Austrian Mint)",
        "weight": "20g",
        "purity": "999.9",
        "currency": "EUR",
        "url": "https://www.muenzeoesterreich.com/en/products/20-gram-gold-muenze-oesterreich-minted-bar",
    }

    try:
        resp = requests.get(product["url"], headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")

            # Look for price elements
            price_el = soup.select_one(
                '.product-price, .price, [class*="price"], [data-price]'
            )
            if price_el:
                import re
                price_text = price_el.get_text(strip=True)
                # Extract numeric price
                price_match = re.search(r'[\d.,]+', price_text.replace(",", ""))
                if price_match:
                    product["price"] = price_match.group()
                product["price_text"] = price_text

            # Try meta tags
            meta_price = soup.find("meta", {"property": "product:price:amount"})
            if meta_price:
                product["price"] = meta_price.get("content", "")

            meta_currency = soup.find("meta", {"property": "product:price:currency"})
            if meta_currency:
                product["currency"] = meta_currency.get("content", "EUR")

            # Look for structured data (JSON-LD)
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    ld_data = json.loads(script.string)
                    if isinstance(ld_data, dict):
                        offers = ld_data.get("offers", {})
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        if "price" in offers:
                            product["price"] = offers["price"]
                            product["currency"] = offers.get(
                                "priceCurrency", "EUR"
                            )
                    elif isinstance(ld_data, list):
                        for item in ld_data:
                            offers = item.get("offers", {})
                            if isinstance(offers, list):
                                offers = offers[0] if offers else {}
                            if "price" in offers:
                                product["price"] = offers["price"]
                                product["currency"] = offers.get(
                                    "priceCurrency", "EUR"
                                )
                                break
                except (json.JSONDecodeError, TypeError):
                    pass

            # Also look for any price in the page text
            if "price" not in product:
                import re
                body_text = soup.get_text()
                eur_prices = re.findall(r'€\s*([\d.,]+)', body_text)
                if not eur_prices:
                    eur_prices = re.findall(r'([\d.,]+)\s*€', body_text)
                if not eur_prices:
                    eur_prices = re.findall(r'EUR\s*([\d.,]+)', body_text)
                if eur_prices:
                    # Take the most likely product price (usually the largest)
                    prices = []
                    for p in eur_prices:
                        try:
                            # Handle European number format (1.234,56)
                            clean = p.replace(".", "").replace(",", ".")
                            prices.append(float(clean))
                        except ValueError:
                            pass
                    if prices:
                        product["price"] = str(max(prices))
                        product["currency"] = "EUR"

    except Exception as e:
        print(f"Mint 20g bar error: {e}")
        product["error"] = str(e)

    product["updated_at"] = datetime.now(timezone.utc).isoformat()

    if product.get("price"):
        _set_cached("mint_20g", product)
    return product


def fetch_mint_philharmoniker():
    """Fetch price of 1oz Vienna Philharmoniker from Austrian Mint."""
    cached = _get_cached("mint_phil")
    if cached:
        return cached

    product = {
        "name": "1 oz Vienna Philharmoniker Gold",
        "weight": "1 oz (31.1g)",
        "purity": "999.9",
        "url": "https://www.muenzeoesterreich.at/produkte/1-unze-gold-wiener-philharmoniker",
    }

    try:
        resp = requests.get(product["url"], headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")

            # Look for price elements
            price_el = soup.select_one(
                '.product-price, .price, [class*="price"], [data-price]'
            )
            if price_el:
                import re
                price_text = price_el.get_text(strip=True)
                product["price_text"] = price_text

            # Try meta tags
            meta_price = soup.find("meta", {"property": "product:price:amount"})
            if meta_price:
                product["price"] = meta_price.get("content", "")

            meta_currency = soup.find("meta", {"property": "product:price:currency"})
            if meta_currency:
                product["currency"] = meta_currency.get("content", "EUR")

            # JSON-LD structured data
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    ld_data = json.loads(script.string)
                    if isinstance(ld_data, dict):
                        offers = ld_data.get("offers", {})
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        if "price" in offers:
                            product["price"] = offers["price"]
                            product["currency"] = offers.get(
                                "priceCurrency", "EUR"
                            )
                    elif isinstance(ld_data, list):
                        for item in ld_data:
                            offers = item.get("offers", {})
                            if isinstance(offers, list):
                                offers = offers[0] if offers else {}
                            if "price" in offers:
                                product["price"] = offers["price"]
                                product["currency"] = offers.get(
                                    "priceCurrency", "EUR"
                                )
                                break
                except (json.JSONDecodeError, TypeError):
                    pass

            # Fallback: regex in body
            if "price" not in product:
                import re
                body_text = soup.get_text()
                eur_prices = re.findall(r'€\s*([\d.,]+)', body_text)
                if not eur_prices:
                    eur_prices = re.findall(r'([\d.,]+)\s*€', body_text)
                if not eur_prices:
                    eur_prices = re.findall(r'EUR\s*([\d.,]+)', body_text)
                if eur_prices:
                    prices = []
                    for p in eur_prices:
                        try:
                            clean = p.replace(".", "").replace(",", ".")
                            prices.append(float(clean))
                        except ValueError:
                            pass
                    if prices:
                        product["price"] = str(max(prices))
                        product["currency"] = "EUR"

    except Exception as e:
        print(f"Mint Philharmoniker error: {e}")
        product["error"] = str(e)

    product["updated_at"] = datetime.now(timezone.utc).isoformat()

    if product.get("price"):
        _set_cached("mint_phil", product)
    return product


def _fetch_philoro_api_skus(skus, headers):
    """Fetch buy and sell prices for one or more Philoro AT SKUs in a single API call."""
    results = {}
    try:
        sku_str = ",".join(skus) if isinstance(skus, (list, tuple)) else str(skus)
        api_url = f"https://api.prod-eu.philoro.ch/api/prices/products?skus={sku_str}&country=AT&currency=EUR"
        resp = requests.get(api_url, headers=headers, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            for prod in data.get("products", []):
                sku = str(prod.get("sku") or prod.get("id") or "")
                entry = {}
                for p in prod.get("prices", []):
                    if p.get("country") == "AT" and p.get("type") == "buy":
                        val = round(int(p["centAmount"]) / 100.0, 2)
                        entry["price"] = str(val)
                        entry["buy_price"] = str(val)
                    elif p.get("country") == "AT" and p.get("type") == "sell":
                        val = round(int(p["centAmount"]) / 100.0, 2)
                        entry["sell_price"] = str(val)
                if sku and entry:
                    results[sku] = entry
    except Exception as e:
        print(f"Philoro API error (SKUs {skus}): {e}")
    return results


def fetch_philoro_20g_bar():
    """Fetch live real-time price of 20g Philoro Gold Bar (Origin) & Münze Österreich (SKU 2005) in one batch call."""
    cached = _get_cached("philoro_20g")
    if cached:
        return cached

    product = {
        "name": "20g Goldbarren philoro Origin",
        "weight": "20g",
        "purity": "999.9",
        "currency": "EUR",
        "url": "https://philoro.at/produkt/goldbarren-20-g-philoro-origin-2445v",
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://philoro.at",
        "Referer": "https://philoro.at/",
        "Accept": "application/json, text/plain, */*",
    }

    # Primary: Direct dynamic price API (batch both 2445 and 2005 in one request)
    batch = _fetch_philoro_api_skus(["2445", "2005"], headers)
    if "2445" in batch:
        product.update(batch["2445"])
    if "2005" in batch:
        mint_p = {
            "name": "Goldbarren 20 g - Münze Österreich (philoro)",
            "weight": "20g",
            "purity": "999.9",
            "currency": "EUR",
            "url": "https://philoro.at/produkt/goldbarren-20-g-muenze-oesterreich-2005v",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **batch["2005"],
        }
        _set_cached("philoro_mint_20g", mint_p)

    # Fallback 1: Extract from page HTML regex
    if "price" not in product:
        try:
            resp = requests.get(product["url"], headers=headers, timeout=10)
            if resp.status_code == 200:
                import re
                matches = re.findall(r"(\d{1,3}(?:\.\d{3})+,\d{2})\s*€", resp.text)
                if matches:
                    def parse_eur(s):
                        return float(s.replace(".", "").replace(",", "."))
                    product["price"] = str(parse_eur(matches[0]))
                    product["buy_price"] = str(parse_eur(matches[0]))
                    if len(matches) > 1:
                        product["sell_price"] = str(parse_eur(matches[1]))
        except Exception as e:
            print(f"Philoro HTML scrape fallback error: {e}")

    # Fallback 2: Nuxt data script
    if "price" not in product:
        try:
            resp = requests.get(product["url"], headers=headers, timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                script = soup.find("script", id="__NUXT_DATA__")
                if script and script.string:
                    data = json.loads(script.string)
                    for item in data:
                        if isinstance(item, dict) and "price" in item and "priceCurrency" in item:
                            price_idx = item["price"]
                            curr_idx = item["priceCurrency"]
                            price_val = data[price_idx] if isinstance(price_idx, int) and price_idx < len(data) else None
                            curr_val = data[curr_idx] if isinstance(curr_idx, int) and curr_idx < len(data) else "EUR"
                            if isinstance(price_val, (int, float)) and price_val > 500:
                                product["price"] = str(round(price_val, 2))
                                product["currency"] = str(curr_val)
                                break
        except Exception as e:
            print(f"Philoro Nuxt fallback error: {e}")

    product["updated_at"] = datetime.now(timezone.utc).isoformat()
    if product.get("price"):
        _set_cached("philoro_20g", product)
    return product


def fetch_philoro_mint_20g_bar():
    """Fetch live Philoro price for the 20g Münze Österreich Gold Bar (SKU 2005)."""
    cached = _get_cached("philoro_mint_20g")
    if cached:
        return cached

    product = {
        "name": "Goldbarren 20 g - Münze Österreich (philoro)",
        "weight": "20g",
        "purity": "999.9",
        "currency": "EUR",
        "url": "https://philoro.at/produkt/goldbarren-20-g-muenze-oesterreich-2005v",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://philoro.at",
        "Referer": "https://philoro.at/",
        "Accept": "application/json, text/plain, */*",
    }
    batch = _fetch_philoro_api_skus(["2005"], headers)
    if "2005" in batch:
        product.update(batch["2005"])
    product["updated_at"] = datetime.now(timezone.utc).isoformat()
    if product.get("price"):
        _set_cached("philoro_mint_20g", product)
    return product


def fetch_sparklines():
    """Fetch 7-day historical closing prices for sparklines."""
    cached = _get_cached("sparklines")
    if cached:
        return cached

    history = {}
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

    # Helper for Yahoo chart endpoint
    def get_closes(symbol):
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=7d",
                headers=headers,
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                quotes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                return [round(p, 2) for p in quotes if p is not None]
        except Exception as e:
            print(f"Error fetching sparkline for {symbol}: {e}")
        return []

    gold_closes = get_closes("GC=F")
    if gold_closes:
        history["gold"] = gold_closes
    usd_closes = get_closes("USDEGP=X")
    if usd_closes:
        history["usd_egp"] = usd_closes
    eur_closes = get_closes("EUREGP=X")
    if eur_closes:
        history["eur_egp"] = eur_closes

    if history:
        _set_cached("sparklines", history)
    return history


def fetch_history(asset="gold", period="30d"):
    """Fetch historical daily series (7d, 30d, 90d, 1y) for interactive charts."""
    period_map = {"7d": "7d", "30d": "1mo", "90d": "3mo", "1y": "1y"}
    yf_range = period_map.get(period, "1mo")
    cache_key = f"history_{asset}_{period}"
    cached = _get_cached(cache_key)
    if cached:
        return cached

    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

    def get_series(symbol):
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range={yf_range}",
            headers=headers,
            timeout=6,
        )
        if r.status_code != 200:
            return {}
        res = r.json()["chart"]["result"][0]
        timestamps = res.get("timestamp", [])
        closes = res["indicators"]["quote"][0].get("close", [])
        series = {}
        for ts, val in zip(timestamps, closes):
            if val is not None:
                date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                series[date_str] = float(val)
        return series

    try:
        dates = []
        values = []

        if asset == "gold":
            s = get_series("GC=F")
            dates = sorted(s.keys())
            values = [round(s[d], 2) for d in dates]
        elif asset == "usd_egp":
            s = get_series("USDEGP=X")
            dates = sorted(s.keys())
            values = [round(s[d], 4) for d in dates]
        elif asset == "eur_egp":
            s = get_series("EUREGP=X")
            dates = sorted(s.keys())
            values = [round(s[d], 4) for d in dates]
        elif asset == "eur_usd":
            s = get_series("EURUSD=X")
            dates = sorted(s.keys())
            values = [round(s[d], 4) for d in dates]
        elif asset in ("mint_20g", "philoro_20g", "mint_phil"):
            gold_s = get_series("GC=F")
            fx_s = get_series("EURUSD=X")
            last_fx = 1.08
            for d in sorted(gold_s.keys()):
                if d in fx_s and fx_s[d] > 0:
                    last_fx = fx_s[d]
                oz_eur = gold_s[d] / last_fx
                val = oz_eur if asset == "mint_phil" else (oz_eur / 31.1035) * 20
                dates.append(d)
                values.append(round(val, 2))
        elif asset == "gold_egp":
            gold_s = get_series("GC=F")
            usd_s = get_series("USDEGP=X")
            last_usd = 50.0
            if usd_s:
                last_usd = list(usd_s.values())[0]
            for d in sorted(gold_s.keys()):
                if d in usd_s and usd_s[d] > 0:
                    last_usd = usd_s[d]
                dates.append(d)
                values.append(round((gold_s[d] / 31.1035) * last_usd, 2))

        if not values:
            return {"asset": asset, "period": period, "labels": [], "values": []}

        first_v, last_v = values[0], values[-1]
        change_pct = ((last_v - first_v) / first_v * 100) if first_v else 0.0
        payload = {
            "asset": asset,
            "period": period,
            "labels": dates,
            "values": values,
            "min": min(values),
            "max": max(values),
            "avg": round(sum(values) / len(values), 4 if asset in ("usd_egp", "eur_egp", "eur_usd") else 2),
            "change_pct": round(change_pct, 2),
        }
        _set_cached(cache_key, payload)
        return payload
    except Exception as e:
        print(f"Error fetching history for {asset} ({period}): {e}")
        return {"asset": asset, "period": period, "labels": [], "values": [], "error": str(e)}


def fetch_all_data():
    """Fetch all data sources concurrently using threads."""
    results = {}
    errors = []

    def run(key, func):
        try:
            results[key] = func()
        except Exception as e:
            errors.append(f"{key}: {e}")
            results[key] = {"error": str(e)}

    threads = [
        threading.Thread(target=run, args=("exchange_rates", fetch_exchange_rates)),
        threading.Thread(target=run, args=("gold_price", fetch_gold_price)),
        threading.Thread(target=run, args=("mint_20g_bar", fetch_mint_20g_bar)),
        threading.Thread(
            target=run, args=("mint_philharmoniker", fetch_mint_philharmoniker)
        ),
        threading.Thread(target=run, args=("philoro_20g_bar", fetch_philoro_20g_bar)),
        threading.Thread(target=run, args=("sparklines", fetch_sparklines)),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    results["philoro_mint_20g_bar"] = fetch_philoro_mint_20g_bar()
    results["fetched_at"] = datetime.now(timezone.utc).isoformat()
    if errors:
        results["errors"] = errors

    return results


# --- Routes ---


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    data = fetch_all_data()
    return jsonify(data)


@app.route("/api/refresh")
def api_refresh():
    # Clear cache
    with _cache_lock:
        _cache.clear()
    data = fetch_all_data()
    return jsonify(data)


@app.route("/api/history")
def api_history():
    asset = request.args.get("asset", "gold")
    period = request.args.get("period", "30d")
    return jsonify(fetch_history(asset=asset, period=period))


@app.route("/health")
def health():
    """Lightweight health check for uptime monitors without triggering data scrapes."""
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


if __name__ == "__main__":
    print("🚀 Finance Dashboard starting on http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=True)
