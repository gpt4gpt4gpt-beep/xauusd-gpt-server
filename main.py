from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone
import os
import json
import urllib.parse
import urllib.request
import time
import copy

app = FastAPI(title="XAUUSD GPT Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

latest_alert = {}

# Simple in-memory cache.
# Note: On serverless hosting, cache persists only while the function instance stays warm.
market_cache = {}


def get_api_key():
    return os.getenv("TWELVE_DATA_API_KEY")


def safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def get_cache_ttl(interval):
    ttl_map = {
        "1day": 1800,   # 30 minutes
        "4h": 900,      # 15 minutes
        "1h": 300,      # 5 minutes
        "15min": 60,    # 1 minute
        "5min": 30      # 30 seconds
    }
    return ttl_map.get(interval, 120)


def cache_key(symbol, interval, outputsize):
    return f"{symbol}:{interval}:{outputsize}"


def get_from_cache(symbol, interval, outputsize):
    key = cache_key(symbol, interval, outputsize)
    item = market_cache.get(key)

    if not item:
        return None

    now = time.time()
    ttl = get_cache_ttl(interval)
    age = now - item["saved_at"]

    if age > ttl:
        return None

    cached_data = copy.deepcopy(item["data"])
    cached_data["cache"] = {
        "hit": True,
        "age_seconds": round(age, 2),
        "ttl_seconds": ttl,
        "cache_key": key
    }
    return cached_data


def save_to_cache(symbol, interval, outputsize, data):
    if not data.get("ok"):
        return

    key = cache_key(symbol, interval, outputsize)
    market_cache[key] = {
        "saved_at": time.time(),
        "data": copy.deepcopy(data)
    }


def fetch_twelve_data(symbol="XAU/USD", interval="1h", outputsize=120):
    cached = get_from_cache(symbol, interval, outputsize)
    if cached:
        return cached

    api_key = get_api_key()

    if not api_key:
        return {
            "ok": False,
            "error": "TWELVE_DATA_API_KEY is missing",
            "cache": {
                "hit": False
            }
        }

    base_url = "https://api.twelvedata.com/time_series"

    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": api_key,
        "format": "JSON"
    }

    url = base_url + "?" + urllib.parse.urlencode(params)

    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw)

        if data.get("status") == "error":
            return {
                "ok": False,
                "error": data.get("message", "Twelve Data API error"),
                "raw": data,
                "cache": {
                    "hit": False
                }
            }

        values = data.get("values", [])

        candles = []
        for item in reversed(values):
            candles.append({
                "time": item.get("datetime"),
                "open": safe_float(item.get("open")),
                "high": safe_float(item.get("high")),
                "low": safe_float(item.get("low")),
                "close": safe_float(item.get("close")),
                "volume": safe_float(item.get("volume", 0))
            })

        result = {
            "ok": True,
            "symbol": symbol,
            "interval": interval,
            "candles": candles,
            "meta": data.get("meta", {}),
            "cache": {
                "hit": False,
                "ttl_seconds": get_cache_ttl(interval),
                "cache_key": cache_key(symbol, interval, outputsize)
            }
        }

        save_to_cache(symbol, interval, outputsize, result)
        return result

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "cache": {
                "hit": False
            }
        }


def ema(values, length):
    if len(values) < length:
        return None

    k = 2 / (length + 1)
    ema_value = sum(values[:length]) / length

    for price in values[length:]:
        ema_value = price * k + ema_value * (1 - k)

    return round(ema_value, 3)


def ema_series(values, length):
    if len(values) < length:
        return []

    k = 2 / (length + 1)
    result = [None] * len(values)
    ema_value = sum(values[:length]) / length
    result[length - 1] = ema_value

    for i in range(length, len(values)):
        ema_value = values[i] * k + ema_value * (1 - k)
        result[i] = ema_value

    return result


def rsi(values, length=14):
    if len(values) <= length:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))

    avg_gain = sum(gains[-length:]) / length
    avg_loss = sum(losses[-length:]) / length

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def macd(values):
    if len(values) < 35:
        return {
            "macd": None,
            "signal": None,
            "histogram": None,
            "bias": "unknown"
        }

    ema_12_series = ema_series(values, 12)
    ema_26_series = ema_series(values, 26)

    macd_values = []
    for i in range(len(values)):
        if i < len(ema_12_series) and i < len(ema_26_series):
            e12 = ema_12_series[i]
            e26 = ema_26_series[i]
            if e12 is not None and e26 is not None:
                macd_values.append(e12 - e26)

    if len(macd_values) < 9:
        macd_value = round(macd_values[-1], 3) if macd_values else None

        if macd_value is None:
            bias = "unknown"
        elif macd_value > 0:
            bias = "bullish"
        elif macd_value < 0:
            bias = "bearish"
        else:
            bias = "neutral"

        return {
            "macd": macd_value,
            "signal": None,
            "histogram": None,
            "bias": bias
        }

    signal_values = ema_series(macd_values, 9)
    signal_clean = [v for v in signal_values if v is not None]

    macd_value = macd_values[-1]
    signal_value = signal_clean[-1] if signal_clean else None

    if signal_value is None:
        histogram = None
        bias = "bullish" if macd_value > 0 else "bearish" if macd_value < 0 else "neutral"
    else:
        histogram = macd_value - signal_value
        if macd_value > signal_value and histogram > 0:
            bias = "bullish"
        elif macd_value < signal_value and histogram < 0:
            bias = "bearish"
        else:
            bias = "neutral"

    return {
        "macd": round(macd_value, 3),
        "signal": round(signal_value, 3) if signal_value is not None else None,
        "histogram": round(histogram, 3) if histogram is not None else None,
        "bias": bias
    }


def atr(candles, length=14):
    if len(candles) <= length:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        true_ranges.append(tr)

    return round(sum(true_ranges[-length:]) / length, 3)


def adx(candles, length=14):
    if len(candles) <= length * 2:
        return {
            "adx": None,
            "plus_di": None,
            "minus_di": None,
            "trend_strength": "unknown",
            "direction": "unknown"
        }

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_high = candles[i - 1]["high"]
        prev_low = candles[i - 1]["low"]
        prev_close = candles[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        up_move = high - prev_high
        down_move = prev_low - low

        pdm = up_move if up_move > down_move and up_move > 0 else 0
        mdm = down_move if down_move > up_move and down_move > 0 else 0

        trs.append(tr)
        plus_dm.append(pdm)
        minus_dm.append(mdm)

    dx_values = []
    latest_plus_di = None
    latest_minus_di = None

    for end in range(length, len(trs) + 1):
        tr_sum = sum(trs[end - length:end])
        plus_sum = sum(plus_dm[end - length:end])
        minus_sum = sum(minus_dm[end - length:end])

        if tr_sum == 0:
            continue

        plus_di = 100 * plus_sum / tr_sum
        minus_di = 100 * minus_sum / tr_sum

        if plus_di + minus_di == 0:
            dx = 0
        else:
            dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)

        dx_values.append(dx)
        latest_plus_di = plus_di
        latest_minus_di = minus_di

    if not dx_values:
        return {
            "adx": None,
            "plus_di": None,
            "minus_di": None,
            "trend_strength": "unknown",
            "direction": "unknown"
        }

    recent_dx = dx_values[-length:] if len(dx_values) >= length else dx_values
    adx_value = sum(recent_dx) / len(recent_dx)

    if adx_value >= 25:
        trend_strength = "strong"
    elif adx_value >= 20:
        trend_strength = "moderate"
    else:
        trend_strength = "weak"

    if latest_plus_di is not None and latest_minus_di is not None:
        if latest_plus_di > latest_minus_di:
            direction = "bullish"
        elif latest_minus_di > latest_plus_di:
            direction = "bearish"
        else:
            direction = "neutral"
    else:
        direction = "unknown"

    return {
        "adx": round(adx_value, 2),
        "plus_di": round(latest_plus_di, 2) if latest_plus_di is not None else None,
        "minus_di": round(latest_minus_di, 2) if latest_minus_di is not None else None,
        "trend_strength": trend_strength,
        "direction": direction
    }


def bollinger_bands(values, length=20, multiplier=2):
    if len(values) < length:
        return {
            "middle": None,
            "upper": None,
            "lower": None,
            "width": None,
            "position": "unknown"
        }

    recent = values[-length:]
    middle = sum(recent) / length
    variance = sum((price - middle) ** 2 for price in recent) / length
    std_dev = variance ** 0.5

    upper = middle + multiplier * std_dev
    lower = middle - multiplier * std_dev
    current = values[-1]
    width = upper - lower

    if current > upper:
        position = "above_upper_band"
    elif current < lower:
        position = "below_lower_band"
    elif current > middle:
        position = "above_middle_band"
    elif current < middle:
        position = "below_middle_band"
    else:
        position = "at_middle_band"

    return {
        "middle": round(middle, 3),
        "upper": round(upper, 3),
        "lower": round(lower, 3),
        "width": round(width, 3),
        "position": position
    }


def market_structure(candles, lookback=20):
    if len(candles) < lookback:
        return {
            "structure": "unknown",
            "last_high": None,
            "last_low": None,
            "note": "Not enough candles"
        }

    recent = candles[-lookback:]
    first_half = recent[:lookback // 2]
    second_half = recent[lookback // 2:]

    first_high = max(c["high"] for c in first_half)
    first_low = min(c["low"] for c in first_half)
    second_high = max(c["high"] for c in second_half)
    second_low = min(c["low"] for c in second_half)

    if second_high > first_high and second_low > first_low:
        structure = "bullish_structure"
    elif second_high < first_high and second_low < first_low:
        structure = "bearish_structure"
    else:
        structure = "range_structure"

    return {
        "structure": structure,
        "last_high": round(second_high, 3),
        "last_low": round(second_low, 3),
        "previous_high": round(first_high, 3),
        "previous_low": round(first_low, 3)
    }


def support_resistance(candles):
    if len(candles) < 30:
        return {
            "support": None,
            "resistance": None
        }

    recent = candles[-40:]
    lows = [c["low"] for c in recent]
    highs = [c["high"] for c in recent]

    return {
        "support": round(min(lows), 3),
        "resistance": round(max(highs), 3)
    }


def analyze_timeframe(interval, label):
    result = fetch_twelve_data(interval=interval)

    if not result.get("ok"):
        return {
            "label": label,
            "interval": interval,
            "ok": False,
            "error": result.get("error"),
            "cache": result.get("cache", {})
        }

    candles = result.get("candles", [])

    if len(candles) < 30:
        return {
            "label": label,
            "interval": interval,
            "ok": False,
            "error": "Not enough candle data",
            "cache": result.get("cache", {})
        }

    closes = [c["close"] for c in candles]
    latest = candles[-1]

    ema_20 = ema(closes, 20)
    ema_50 = ema(closes, 50)
    ema_200 = ema(closes, 200) if len(closes) >= 200 else None

    current_rsi = rsi(closes)
    current_macd = macd(closes)
    current_atr = atr(candles)
    current_adx = adx(candles)
    current_bb = bollinger_bands(closes)
    current_structure = market_structure(candles)
    sr = support_resistance(candles)

    bias = "neutral"

    if ema_20 and ema_50:
        if latest["close"] > ema_20 > ema_50:
            bias = "bullish"
        elif latest["close"] < ema_20 < ema_50:
            bias = "bearish"

    return {
        "label": label,
        "interval": interval,
        "ok": True,
        "latest_time": latest["time"],
        "current_price": latest["close"],
        "bias": bias,
        "ema": {
            "ema_20": ema_20,
            "ema_50": ema_50,
            "ema_200": ema_200
        },
        "rsi": current_rsi,
        "macd": current_macd,
        "atr": current_atr,
        "adx": current_adx,
        "bollinger_bands": current_bb,
        "market_structure": current_structure,
        "support": sr["support"],
        "resistance": sr["resistance"],
        "cache": result.get("cache", {})
    }


def build_decision(snapshot):
    confirmations_buy = 0
    confirmations_sell = 0

    for tf in snapshot["timeframes"].values():
        if not tf.get("ok"):
            continue

        if tf.get("bias") == "bullish":
            confirmations_buy += 1
        elif tf.get("bias") == "bearish":
            confirmations_sell += 1

        if tf.get("rsi") is not None:
            if tf["rsi"] > 55:
                confirmations_buy += 1
            elif tf["rsi"] < 45:
                confirmations_sell += 1

        macd_bias = tf.get("macd", {}).get("bias")
        if macd_bias == "bullish":
            confirmations_buy += 1
        elif macd_bias == "bearish":
            confirmations_sell += 1

        adx_data = tf.get("adx", {})
        if adx_data.get("trend_strength") in ["moderate", "strong"]:
            if adx_data.get("direction") == "bullish":
                confirmations_buy += 1
            elif adx_data.get("direction") == "bearish":
                confirmations_sell += 1

        structure = tf.get("market_structure", {}).get("structure")
        if structure == "bullish_structure":
            confirmations_buy += 1
        elif structure == "bearish_structure":
            confirmations_sell += 1

        bb_position = tf.get("bollinger_bands", {}).get("position")
        if bb_position in ["above_middle_band", "above_upper_band"]:
            confirmations_buy += 1
        elif bb_position in ["below_middle_band", "below_lower_band"]:
            confirmations_sell += 1

    decision = "WAIT"
    signal_type = "Wait"
    confidence = max(confirmations_buy, confirmations_sell) * 5

    if confirmations_buy >= 9 and confirmations_buy > confirmations_sell + 3:
        decision = "BUY"
        signal_type = "Medium Buy"
        confidence = min(85, confirmations_buy * 6)

    elif confirmations_sell >= 9 and confirmations_sell > confirmations_buy + 3:
        decision = "SELL"
        signal_type = "Medium Sell"
        confidence = min(85, confirmations_sell * 6)

    return {
        "decision": decision,
        "signal_type": signal_type,
        "confidence": confidence,
        "buy_confirmations": confirmations_buy,
        "sell_confirmations": confirmations_sell
    }


@app.get("/")
def home():
    return {
        "status": "running",
        "message": "XAUUSD GPT Server is live",
        "version": "cache_v1_adx_bollinger_market_structure"
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "cache_items": len(market_cache)
    }


@app.get("/cache/status")
def cache_status():
    now = time.time()
    items = []

    for key, item in market_cache.items():
        data = item.get("data", {})
        interval = data.get("interval", "unknown")
        ttl = get_cache_ttl(interval)
        age = now - item["saved_at"]

        items.append({
            "cache_key": key,
            "interval": interval,
            "age_seconds": round(age, 2),
            "ttl_seconds": ttl,
            "valid": age <= ttl
        })

    return {
        "cache_items": len(market_cache),
        "items": items
    }


@app.post("/cache/clear")
def cache_clear():
    market_cache.clear()
    return {
        "status": "cleared",
        "cache_items": len(market_cache)
    }


@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request):
    global latest_alert

    try:
        data = await request.json()
    except Exception:
        body = await request.body()
        data = {
            "raw": body.decode("utf-8", errors="ignore")
        }

    latest_alert = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "data": data
    }

    return {
        "status": "received",
        "alert": latest_alert
    }


@app.get("/latest-alert")
def get_latest_alert():
    return {
        "latest_alert": latest_alert
    }


@app.get("/xauusd/analysis")
def xauusd_analysis():
    timeframes = {
        "daily": analyze_timeframe("1day", "Daily"),
        "h4": analyze_timeframe("4h", "4H"),
        "h1": analyze_timeframe("1h", "1H")
    }

    snapshot = {
        "symbol": "XAUUSD",
        "data_source": "Twelve Data API",
        "server_time": datetime.now(timezone.utc).isoformat(),
        "timeframes": timeframes,
        "latest_alert": latest_alert,
        "cache_summary": {
            "cache_items": len(market_cache)
        }
    }

    valid_timeframes = [
        tf for tf in timeframes.values()
        if tf.get("ok")
    ]

    if len(valid_timeframes) < 3:
        return {
            "symbol": "XAUUSD",
            "data_source": "Twelve Data API",
            "decision": "WAIT",
            "signal_type": "Wait",
            "confidence": 0,
            "current_price": None,
            "buy_confirmations": 0,
            "sell_confirmations": 0,
            "message": "Market data returned, but not enough valid timeframes for a reliable signal.",
            "risk_note": "Educational analysis only. Do not trade without confirmation and risk management.",
            "snapshot": snapshot
        }

    decision_data = build_decision(snapshot)

    current_price = None
    if timeframes["h1"].get("ok"):
        current_price = timeframes["h1"].get("current_price")
    elif timeframes["h4"].get("ok"):
        current_price = timeframes["h4"].get("current_price")
    elif timeframes["daily"].get("ok"):
        current_price = timeframes["daily"].get("current_price")

    return {
        "symbol": "XAUUSD",
        "data_source": "Twelve Data API",
        "decision": decision_data["decision"],
        "signal_type": decision_data["signal_type"],
        "confidence": decision_data["confidence"],
        "current_price": current_price,
        "buy_confirmations": decision_data["buy_confirmations"],
        "sell_confirmations": decision_data["sell_confirmations"],
        "risk_note": "Educational analysis only. Do not trade without confirmation and risk management.",
        "snapshot": snapshot
    }
