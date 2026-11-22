import csv
import io
import json
import math
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv


load_dotenv()
app = Flask(__name__, template_folder="template")

GOLDAPI_BASE = "https://www.goldapi.io/api"
CITY_RATES_BASE = "https://api.apiexpress.in/v1/commodity/gold"
FREE_BENCHMARK_URL = "https://bhavflow.com/open-data/bhavflow-daily-rates.csv"
TROY_OUNCE_GRAMS = 31.1034768
CITY_CACHE_SECONDS = 10 * 60
BENCHMARK_CACHE_SECONDS = 30 * 60
GOLD_CACHE_SECONDS = 15 * 60
HISTORY_CACHE_SECONDS = 12 * 60 * 60

POPULAR_CITIES = [
    "Delhi", "Mumbai", "Bengaluru", "Chennai", "Hyderabad",
    "Kolkata", "Pune", "Ahmedabad", "Jaipur", "Lucknow", "Kochi",
]

_lock = threading.Lock()
_city_cache = {}
_benchmark_cache = {"value": None, "cached_at": 0.0}
_gold_cache = {"value": None, "cached_at": 0.0}
_history_cache = {"prices": [], "cached_at": 0.0}


class ProviderError(Exception):
    """A concise, safe-to-display data provider error."""


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _get_json(url, headers=None, timeout=12):
    request_headers = {"Accept": "application/json", "User-Agent": "SonaIndiaGoldDesk/1.0"}
    request_headers.update(headers or {})
    req = Request(url, headers=request_headers)
    try:
        with urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ProviderError("The provider rejected the API key or access plan.") from None
        if exc.code == 429:
            raise ProviderError("The provider request limit was reached. Try again later.") from None
        raise ProviderError("The provider returned an error. Try again later.") from None
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderError("Could not reach the data provider. Check the connection and try again.") from None
    if not isinstance(payload, (dict, list)):
        raise ProviderError("The provider returned an unexpected response.")
    return payload


def _city_rates_key():
    return os.environ.get("APIEXPRESS_API_KEY", "").strip()


def _goldapi_key():
    return os.environ.get("GOLDAPI_KEY", "").strip()


def _fetch_city_quote(city):
    key = _city_rates_key()
    if not key:
        raise ProviderError("City rates need an APIEXPRESS_API_KEY.")
    url = f"{CITY_RATES_BASE}?{urlencode({'city': city})}"
    payload = _get_json(url, {"Authorization": f"Bearer {key}"})
    if not isinstance(payload, dict):
        raise ProviderError("The city-rate provider returned an unexpected response.")
    if payload.get("status") not in (None, "success"):
        raise ProviderError("The city-rate provider did not return a successful response.")
    gst_flag = payload.get("gst_included", False)
    gst_included = gst_flag is True or str(gst_flag).strip().lower() in ("true", "1", "yes")
    normalized = {}
    for label in ("24k", "22k"):
        raw = _number(payload.get(f"gold_{label}"))
        if raw is None or raw <= 0:
            continue
        final = raw if gst_included else raw * 1.03
        base = raw / 1.03 if gst_included else raw
        normalized[label] = {
            "base_per_gram": base,
            "gst_per_gram": final - base,
            "final_per_gram": final,
            "base_per_10g": base * 10,
            "final_per_10g": final * 10,
        }
    # The city feed documents only 22K and 24K. Do not invent other purities.
    for label in ("18k", "14k"):
        normalized[label] = {"base_per_gram": None, "gst_per_gram": None, "final_per_gram": None,
                             "base_per_10g": None, "final_per_10g": None}
    if normalized["22k"]["base_per_gram"] is None or normalized["24k"]["base_per_gram"] is None:
        raise ProviderError("The city-rate response did not include 24K and 22K prices.")
    return {
        "city": payload.get("city") or city,
        "timestamp": payload.get("updated"),
        "confidence": "provider-reported indicative estimate",
        "gst_included": gst_included,
        "prices": normalized,
    }


def _load_city_quote(city, force=False):
    city_key = city.strip()
    cache_key = city_key.casefold()
    cached = _city_cache.get(cache_key)
    if cached and not force and time.time() - cached["cached_at"] < CITY_CACHE_SECONDS:
        return cached["value"]
    with _lock:
        cached = _city_cache.get(cache_key)
        if cached and not force and time.time() - cached["cached_at"] < CITY_CACHE_SECONDS:
            return cached["value"]
        value = _fetch_city_quote(city_key)
        _city_cache[cache_key] = {"value": value, "cached_at": time.time()}
        return value


def _fetch_free_benchmark():
    req = Request(FREE_BENCHMARK_URL, headers={
        "Accept": "text/csv",
        "User-Agent": "SonaIndiaGoldDesk/1.0 (Bhavflow CC BY 4.0 attribution)",
    })
    try:
        with urlopen(req, timeout=12) as response:
            raw_csv = response.read().decode("utf-8-sig")
    except HTTPError as exc:
        if exc.code == 429:
            raise ProviderError("The free national-rate source is busy. Try again later.") from None
        raise ProviderError("The free national-rate source returned an error.") from None
    except (URLError, TimeoutError, UnicodeDecodeError):
        raise ProviderError("Could not reach the free national-rate source.") from None

    rows = []
    for row in csv.DictReader(io.StringIO(raw_csv)):
        try:
            row_date = date.fromisoformat(row.get("date", "").strip())
        except ValueError:
            continue
        rates = {
            "999": _number(row.get("gold24_inr_per_10g")),
            "916": _number(row.get("gold22_inr_per_10g")),
            "750": _number(row.get("gold18_inr_per_10g")),
        }
        if all(rate and rate > 0 for rate in rates.values()):
            rows.append((row_date, rates, row.get("basis", "unknown").strip()))
    if not rows:
        raise ProviderError("No usable national benchmark row was returned.")

    latest_date, rates, basis = max(rows, key=lambda item: item[0])
    return {
        "date": latest_date.isoformat(),
        "session": "Daily indicative",
        "unit": "INR per 10 g",
        "rates": rates,
        "source": "Bhavflow",
        "basis": basis,
        "gst_included": False,
        "license": "CC BY 4.0",
    }


def _load_benchmark(force=False):
    if _benchmark_cache["value"] and not force and time.time() - _benchmark_cache["cached_at"] < BENCHMARK_CACHE_SECONDS:
        return _benchmark_cache["value"]
    with _lock:
        if _benchmark_cache["value"] and not force and time.time() - _benchmark_cache["cached_at"] < BENCHMARK_CACHE_SECONDS:
            return _benchmark_cache["value"]
        value = _fetch_free_benchmark()
        _benchmark_cache.update(value=value, cached_at=time.time())
        return value


def _goldapi_get(path, query=None):
    key = _goldapi_key()
    if not key:
        raise ProviderError("Add GOLDAPI_KEY to enable the global spot and forecast feed.")
    url = f"{GOLDAPI_BASE}/{path}"
    if query:
        url += f"?{urlencode(query)}"
    return _get_json(url, {"x-access-token": key})


def _load_history(now_utc):
    if time.time() - _history_cache["cached_at"] < HISTORY_CACHE_SECONDS:
        return _history_cache["prices"]
    end = now_utc.date()
    start = end - timedelta(days=60)
    data = _goldapi_get("history/XAU/INR", {"from": start.isoformat(), "to": end.isoformat()})
    rows = data.get("prices", [])
    prices = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                price = _number(row.get("price"))
                if price and price > 0 and row.get("date"):
                    prices.append({"date": str(row["date"]), "price": price})
    prices.sort(key=lambda item: item["date"])
    _history_cache.update(prices=prices, cached_at=time.time())
    return prices


def _forecast(current_price, history):
    series = [row["price"] for row in history if row["price"] > 0]
    if len(series) < 10:
        return None
    returns = [math.log(later / earlier) for earlier, later in zip(series, series[1:])]
    if len(returns) < 8:
        return None
    drift = sum(returns) / len(returns)
    variance = sum((value - drift) ** 2 for value in returns) / (len(returns) - 1)
    volatility = math.sqrt(variance)
    output = {}
    for label, sessions in (("1d", 1), ("1w", 5), ("1m", 21)):
        expected_move = drift * sessions
        band = 1.96 * volatility * math.sqrt(sessions)
        output[label] = {
            "price": current_price * math.exp(expected_move),
            "low": current_price * math.exp(expected_move - band),
            "high": current_price * math.exp(expected_move + band),
            "change_pct": (math.exp(expected_move) - 1) * 100,
            "sessions": sessions,
        }
    return output


def _fetch_global_market():
    quote = _goldapi_get("price/XAU/INR", {"melt_price": "true"})
    price = _number(quote.get("price"))
    timestamp = _number(quote.get("timestamp"))
    if not price or not timestamp:
        raise ProviderError("GoldAPI returned an incomplete global spot quote.")
    melt = quote.get("melt_price_per_gram") or {}
    per_gram = _number(melt.get("24k")) or price / TROY_OUNCE_GRAMS
    fx = _goldapi_get("rates/USD/INR")
    now_utc = datetime.fromtimestamp(timestamp, timezone.utc)
    history = []
    try:
        history = _load_history(now_utc)
    except ProviderError:
        pass
    today = now_utc.date().isoformat()
    if history and history[-1]["date"] == today:
        history[-1] = {"date": today, "price": price}
    elif not history or history[-1]["date"] < today:
        history.append({"date": today, "price": price})
    return {
        "spot_inr_10g": per_gram * 10,
        "usd_inr": _number(fx.get("price")),
        "change_pct": _number(quote.get("change_percent", quote.get("chp"))),
        "timestamp": int(timestamp),
        "history": history[-30:],
        "forecast": _forecast(price, history),
        "source": "GoldAPI.io · XAU/INR spot",
    }


def _load_global_market(force=False):
    if _gold_cache["value"] and not force and time.time() - _gold_cache["cached_at"] < GOLD_CACHE_SECONDS:
        return _gold_cache["value"]
    with _lock:
        if _gold_cache["value"] and not force and time.time() - _gold_cache["cached_at"] < GOLD_CACHE_SECONDS:
            return _gold_cache["value"]
        value = _fetch_global_market()
        _gold_cache.update(value=value, cached_at=time.time())
        return value


@app.get("/")
def home():
    providers = {
        "city": bool(_city_rates_key()),
        "benchmark": True,
        "global": bool(_goldapi_key()),
    }
    city = request.args.get("city", "Delhi").strip()[:80] or "Delhi"
    return render_template("index.html", providers=providers, popular_cities=POPULAR_CITIES, selected_city=city)


@app.get("/api/market")
def market_data():
    city = request.args.get("city", "Delhi").strip()[:80] or "Delhi"
    force = request.args.get("refresh") == "1"
    output = {
        "city": {"value": None, "error": None},
        "benchmark": {"value": None, "error": None},
        "global": {"value": None, "error": None},
        "configured": {
            "city": bool(_city_rates_key()),
            "benchmark": True,
            "global": bool(_goldapi_key()),
        },
    }
    loaders = (
        ("city", lambda: _load_city_quote(city, force)),
        ("benchmark", lambda: _load_benchmark(force)),
        ("global", lambda: _load_global_market(force)),
    )
    for name, loader in loaders:
        try:
            output[name]["value"] = loader()
        except ProviderError as exc:
            output[name]["error"] = str(exc)
    city_value = output["city"]["value"]
    benchmark_value = output["benchmark"]["value"]
    if city_value and benchmark_value:
        city_22 = city_value["prices"]["22k"]["base_per_10g"]
        national_22k = benchmark_value["rates"].get("916")
        output["comparison"] = {
            "city_22k_base": city_22,
            "national_22k": national_22k,
            "difference": city_22 - national_22k if national_22k is not None else None,
            "difference_pct": (city_22 / national_22k - 1) * 100 if national_22k else None,
        }
    else:
        output["comparison"] = None
    return jsonify(output)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")))
