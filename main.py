from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone
import os
import json
import urllib.parse
import urllib.request
import urllib.error
import time

app = FastAPI(title="XAUUSD GPT Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

latest_alert = {}


def get_api_key():
    return os.getenv("TWELVE_DATA_API_KEY")


def get_supabase_url():
    url = os.getenv("SUPABASE_URL", "")
    return url.strip().rstrip("/")


def get_supabase_key():
    return os.getenv("SUPABASE_SERVICE_ROLE_KEY")


def supabase_enabled():
    return bool(get_supabase_url() and get_supabase_key())


def safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def now_epoch():
    return time.time()


def iso_from_epoch(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def epoch_from_iso(value):
    try:
        if not value:
            return 0
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except Exception:
        return 0


def get_cache_ttl(interval):
    ttl_map = {
        "1day": 1800,
        "4h": 900,
        "1h": 300,
        "15min": 60,
        "5min": 30,
    }
    return ttl_map.get(interval, 120)


def cache_key(symbol, interval, outputsize):
    safe_symbol = symbol.replace("/", "_")
    return f"{safe_symbol}:{interval}:{outputsize}"


def supabase_request(method, path, body=None, query=None):
    if not supabase_enabled():
        return {"ok": False, "error": "Supabase environment variables are missing"}

    base_url = get_supabase_url()
    api_key = get_supabase_key()
    url = f"{base_url}/rest/v1/{path}"
    if query:
        url += "?" + query

    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "return=representation,resolution=merge-duplicates",
    }

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    request = urllib.request.Request(url=url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            parsed = json.loads(raw) if raw else None
            return {"ok": True, "status": response.status, "data": parsed}
    except urllib.error.HTTPError as e:
        try:
            error_raw = e.read().decode("utf-8")
        except Exception:
            error_raw = str(e)
        return {"ok": False, "status": e.code, "error": error_raw}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_from_supabase_cache(symbol, interval, outputsize):
    if not supabase_enabled():
        return None

    key = cache_key(symbol, interval, outputsize)
    encoded_key = urllib.parse.quote(key, safe="")
    query = (
        f"cache_key=eq.{encoded_key}"
        "&select=cache_key,symbol,interval,outputsize,data,saved_at,expires_at"
        "&limit=1"
    )

    result = supabase_request("GET", "market_cache", query=query)
    if not result.get("ok"):
        return None

    rows = result.get("data") or []
    if not rows:
        return None

    row = rows[0]
    expires_epoch = epoch_from_iso(row.get("expires_at"))
    if expires_epoch <= now_epoch():
        return None

    cached_data = row.get("data") or {}
    saved_epoch = epoch_from_iso(row.get("saved_at"))
    age_seconds = max(0, now_epoch() - saved_epoch)

    cached_data["cache"] = {
        "hit": True,
        "type": "supabase",
        "age_seconds": round(age_seconds, 2),
        "ttl_seconds": get_cache_ttl(interval),
        "cache_key": key,
        "saved_at": row.get("saved_at"),
        "expires_at": row.get("expires_at"),
    }
    return cached_data


def save_to_supabase_cache(symbol, interval, outputsize, data):
    if not supabase_enabled() or not data.get("ok"):
        return

    key = cache_key(symbol, interval, outputsize)
    ttl = get_cache_ttl(interval)
    saved_epoch = now_epoch()
    expires_epoch = saved_epoch + ttl

    clean_data = dict(data)
    clean_data["cache"] = {
        "hit": False,
        "type": "supabase",
        "ttl_seconds": ttl,
        "cache_key": key,
    }

    payload = {
        "cache_key": key,
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "data": clean_data,
        "saved_at": iso_from_epoch(saved_epoch),
        "expires_at": iso_from_epoch(expires_epoch),
    }

    supabase_request(
        "POST",
        "market_cache",
        body=[payload],
        query="on_conflict=cache_key",
    )


def clear_expired_cache_rows():
    if not supabase_enabled():
        return {"ok": False, "error": "Supabase is not enabled"}
    current_time = urllib.parse.quote(utc_now_iso(), safe="")
    return supabase_request("DELETE", "market_cache", query=f"expires_at=lt.{current_time}")


def clear_all_cache_rows():
    if not supabase_enabled():
        return {"ok": False, "error": "Supabase is not enabled"}
    return supabase_request("DELETE", "market_cache", query="cache_key=not.is.null")


def get_cache_status_rows():
    if not supabase_enabled():
        return {"ok": False, "error": "Supabase is not enabled", "cache_items": 0, "items": []}

    result = supabase_request(
        "GET",
        "market_cache",
        query="select=cache_key,symbol,interval,outputsize,saved_at,expires_at&order=saved_at.desc",
    )

    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"), "cache_items": 0, "items": []}

    rows = result.get("data") or []
    items = []
    for row in rows:
        interval = row.get("interval")
        saved_epoch = epoch_from_iso(row.get("saved_at"))
        expires_epoch = epoch_from_iso(row.get("expires_at"))
        age_seconds = max(0, now_epoch() - saved_epoch)
        remaining_seconds = max(0, expires_epoch - now_epoch())
        items.append({
            "cache_key": row.get("cache_key"),
            "symbol": row.get("symbol"),
            "interval": interval,
            "outputsize": row.get("outputsize"),
            "age_seconds": round(age_seconds, 2),
            "remaining_seconds": round(remaining_seconds, 2),
            "ttl_seconds": get_cache_ttl(interval),
            "saved_at": row.get("saved_at"),
            "expires_at": row.get("expires_at"),
            "valid": remaining_seconds > 0,
        })
    return {"ok": True, "cache_items": len(items), "items": items}


def fetch_twelve_data(symbol="XAU/USD", interval="1h", outputsize=120):
    cached = get_from_supabase_cache(symbol, interval, outputsize)
    if cached:
        return cached

    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "error": "TWELVE_DATA_API_KEY is missing", "cache": {"hit": False}}

    base_url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": api_key,
        "format": "JSON",
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
                "cache": {"hit": False, "type": "supabase" if supabase_enabled() else "none"},
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
                "volume": safe_float(item.get("volume", 0)),
            })

        result = {
            "ok": True,
            "symbol": symbol,
            "interval": interval,
            "candles": candles,
            "meta": data.get("meta", {}),
            "cache": {
                "hit": False,
                "type": "supabase" if supabase_enabled() else "none",
                "ttl_seconds": get_cache_ttl(interval),
                "cache_key": cache_key(symbol, interval, outputsize),
            },
        }
        save_to_supabase_cache(symbol, interval, outputsize, result)
        return result
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "cache": {"hit": False, "type": "supabase" if supabase_enabled() else "none"},
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
        return {"macd": None, "signal": None, "histogram": None, "bias": "unknown"}

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
        return {"macd": macd_value, "signal": None, "histogram": None, "bias": bias}

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
        "bias": bias,
    }


def atr(candles, length=14):
    if len(candles) <= length:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    return round(sum(true_ranges[-length:]) / length, 3)


def adx(candles, length=14):
    if len(candles) <= length * 2:
        return {"adx": None, "plus_di": None, "minus_di": None, "trend_strength": "unknown", "direction": "unknown"}

    trs = []
    plus_dm = []
    minus_dm = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_high = candles[i - 1]["high"]
        prev_low = candles[i - 1]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
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
        dx = 0 if plus_di + minus_di == 0 else 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        dx_values.append(dx)
        latest_plus_di = plus_di
        latest_minus_di = minus_di

    if not dx_values:
        return {"adx": None, "plus_di": None, "minus_di": None, "trend_strength": "unknown", "direction": "unknown"}

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
        "direction": direction,
    }


def bollinger_bands(values, length=20, multiplier=2):
    if len(values) < length:
        return {"middle": None, "upper": None, "lower": None, "width": None, "position": "unknown"}
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
        "position": position,
    }


def market_structure(candles, lookback=20):
    if len(candles) < lookback:
        return {"structure": "unknown", "last_high": None, "last_low": None, "note": "Not enough candles"}
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
        "previous_low": round(first_low, 3),
    }


def support_resistance(candles):
    if len(candles) < 30:
        return {"support": None, "resistance": None}
    recent = candles[-40:]
    lows = [c["low"] for c in recent]
    highs = [c["high"] for c in recent]
    return {"support": round(min(lows), 3), "resistance": round(max(highs), 3)}


def analyze_timeframe(interval, label):
    result = fetch_twelve_data(interval=interval)
    if not result.get("ok"):
        return {"label": label, "interval": interval, "ok": False, "error": result.get("error"), "cache": result.get("cache", {})}

    candles = result.get("candles", [])
    if len(candles) < 30:
        return {"label": label, "interval": interval, "ok": False, "error": "Not enough candle data", "cache": result.get("cache", {})}

    closes = [c["close"] for c in candles]
    latest = candles[-1]
    ema_20 = ema(closes, 20)
    ema_50 = ema(closes, 50)
    ema_200 = ema(closes, 200) if len(closes) >= 200 else None
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
        "ema": {"ema_20": ema_20, "ema_50": ema_50, "ema_200": ema_200},
        "rsi": rsi(closes),
        "macd": macd(closes),
        "atr": atr(candles),
        "adx": adx(candles),
        "bollinger_bands": bollinger_bands(closes),
        "market_structure": market_structure(candles),
        "support": sr["support"],
        "resistance": sr["resistance"],
        "cache": result.get("cache", {}),
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
        "sell_confirmations": confirmations_sell,
    }


@app.get("/")
def home():
    return {
        "status": "running",
        "message": "XAUUSD GPT Server is live",
        "version": "supabase_cache_v2_scalp_endpoint",
        "supabase_cache_enabled": supabase_enabled(),
    }


@app.get("/health")
def health():
    return {"status": "ok", "time": utc_now_iso(), "supabase_cache_enabled": supabase_enabled()}


@app.get("/cache/status")
def cache_status():
    return get_cache_status_rows()


@app.post("/cache/clear")
def cache_clear():
    result = clear_all_cache_rows()
    return {"status": "cleared" if result.get("ok") else "error", "result": result}


@app.post("/cache/clear-expired")
def cache_clear_expired():
    result = clear_expired_cache_rows()
    return {"status": "cleared_expired" if result.get("ok") else "error", "result": result}


@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request):
    global latest_alert
    try:
        data = await request.json()
    except Exception:
        body = await request.body()
        data = {"raw": body.decode("utf-8", errors="ignore")}
    latest_alert = {"received_at": utc_now_iso(), "data": data}
    return {"status": "received", "alert": latest_alert}


@app.get("/latest-alert")
def get_latest_alert():
    return {"latest_alert": latest_alert}


@app.get("/xauusd/analysis")
def xauusd_analysis():
    timeframes = {
        "daily": analyze_timeframe("1day", "Daily"),
        "h4": analyze_timeframe("4h", "4H"),
        "h1": analyze_timeframe("1h", "1H"),
    }

    snapshot = {
        "symbol": "XAUUSD",
        "data_source": "Twelve Data API",
        "server_time": utc_now_iso(),
        "timeframes": timeframes,
        "latest_alert": latest_alert,
        "cache_summary": {"type": "supabase", "enabled": supabase_enabled()},
    }

    valid_timeframes = [tf for tf in timeframes.values() if tf.get("ok")]
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
            "snapshot": snapshot,
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
        "snapshot": snapshot,
    }

# =========================
# Scalp analysis: 1H + 15m + 5m
# =========================

def add_score(reason_list, label, side, points, reason):
    reason_list.append({
        "timeframe": label,
        "side": side,
        "points": points,
        "reason": reason
    })


def build_scalp_decision(snapshot):
    timeframes = snapshot.get("timeframes", {})
    h1 = timeframes.get("h1", {})
    m15 = timeframes.get("m15", {})
    m5 = timeframes.get("m5", {})

    buy_score = 0
    sell_score = 0
    reasons = []

    weights = {
        "h1": 2,
        "m15": 2,
        "m5": 1
    }

    for key, tf in timeframes.items():
        if not tf.get("ok"):
            continue

        label = tf.get("label", key)
        weight = weights.get(key, 1)

        bias = tf.get("bias")
        if bias == "bullish":
            buy_score += 2 * weight
            add_score(reasons, label, "buy", 2 * weight, "bullish EMA/bias alignment")
        elif bias == "bearish":
            sell_score += 2 * weight
            add_score(reasons, label, "sell", 2 * weight, "bearish EMA/bias alignment")

        rsi_value = tf.get("rsi")
        if rsi_value is not None:
            if rsi_value > 55:
                buy_score += 1 * weight
                add_score(reasons, label, "buy", 1 * weight, "RSI above 55")
            elif rsi_value < 45:
                sell_score += 1 * weight
                add_score(reasons, label, "sell", 1 * weight, "RSI below 45")

        macd_bias = tf.get("macd", {}).get("bias")
        if macd_bias == "bullish":
            buy_score += 1 * weight
            add_score(reasons, label, "buy", 1 * weight, "MACD bullish")
        elif macd_bias == "bearish":
            sell_score += 1 * weight
            add_score(reasons, label, "sell", 1 * weight, "MACD bearish")

        adx_data = tf.get("adx", {})
        if adx_data.get("trend_strength") in ["moderate", "strong"]:
            if adx_data.get("direction") == "bullish":
                buy_score += 1 * weight
                add_score(reasons, label, "buy", 1 * weight, "ADX supports bullish direction")
            elif adx_data.get("direction") == "bearish":
                sell_score += 1 * weight
                add_score(reasons, label, "sell", 1 * weight, "ADX supports bearish direction")

        structure = tf.get("market_structure", {}).get("structure")
        if structure == "bullish_structure":
            buy_score += 1 * weight
            add_score(reasons, label, "buy", 1 * weight, "bullish market structure")
        elif structure == "bearish_structure":
            sell_score += 1 * weight
            add_score(reasons, label, "sell", 1 * weight, "bearish market structure")

        bb_position = tf.get("bollinger_bands", {}).get("position")
        if bb_position in ["above_middle_band", "above_upper_band"]:
            buy_score += 1 * weight
            add_score(reasons, label, "buy", 1 * weight, "Bollinger position above middle/upper band")
        elif bb_position in ["below_middle_band", "below_lower_band"]:
            sell_score += 1 * weight
            add_score(reasons, label, "sell", 1 * weight, "Bollinger position below middle/lower band")

    decision = "WAIT"
    signal_type = "Scalp Wait"
    confidence = min(90, max(buy_score, sell_score) * 4)

    h1_direction = h1.get("bias", "neutral") if h1.get("ok") else "unknown"
    m15_direction = m15.get("bias", "neutral") if m15.get("ok") else "unknown"
    m5_direction = m5.get("bias", "neutral") if m5.get("ok") else "unknown"

    # Conservative scalp rules:
    # 1H is filter, 15m is confirmation, 5m is execution.
    if (
        buy_score >= 14
        and buy_score >= sell_score + 5
        and h1_direction != "bearish"
        and m15_direction != "bearish"
    ):
        decision = "BUY"
        signal_type = "Scalp Buy"
        confidence = min(90, buy_score * 4)
    elif (
        sell_score >= 14
        and sell_score >= buy_score + 5
        and h1_direction != "bullish"
        and m15_direction != "bullish"
    ):
        decision = "SELL"
        signal_type = "Scalp Sell"
        confidence = min(90, sell_score * 4)

    if abs(buy_score - sell_score) <= 4:
        decision = "WAIT"
        signal_type = "Scalp Wait - Mixed Signals"
        confidence = min(confidence, 50)

    key_levels = {
        "h1_support": h1.get("support") if h1.get("ok") else None,
        "h1_resistance": h1.get("resistance") if h1.get("ok") else None,
        "m15_support": m15.get("support") if m15.get("ok") else None,
        "m15_resistance": m15.get("resistance") if m15.get("ok") else None,
        "m5_support": m5.get("support") if m5.get("ok") else None,
        "m5_resistance": m5.get("resistance") if m5.get("ok") else None,
    }

    current_price = None
    if m5.get("ok"):
        current_price = m5.get("current_price")
    elif m15.get("ok"):
        current_price = m15.get("current_price")
    elif h1.get("ok"):
        current_price = h1.get("current_price")

    plan = {
        "execution_timeframe": "5m",
        "confirmation_timeframe": "15m",
        "trend_filter_timeframe": "1H",
        "entry_zone": None,
        "stop_loss": None,
        "take_profit_1": None,
        "take_profit_2": None,
        "invalidation_condition": None,
        "note": "No trade levels are provided unless the scalp decision is BUY or SELL."
    }

    if decision == "BUY":
        plan["entry_zone"] = "Use 5m execution only after price holds above nearby 5m/15m support or breaks resistance with confirmation."
        plan["stop_loss"] = "Below nearest confirmed 5m/15m swing low or ATR-based invalidation."
        plan["take_profit_1"] = "Nearest 5m/15m resistance."
        plan["take_profit_2"] = "Next 1H resistance if momentum continues."
        plan["invalidation_condition"] = "5m closes back below the trigger zone or 15m momentum turns bearish."
        plan["note"] = "Educational scalp idea only. Do not enter without live confirmation and risk management."
    elif decision == "SELL":
        plan["entry_zone"] = "Use 5m execution only after price rejects nearby resistance or breaks support with confirmation."
        plan["stop_loss"] = "Above nearest confirmed 5m/15m swing high or ATR-based invalidation."
        plan["take_profit_1"] = "Nearest 5m/15m support."
        plan["take_profit_2"] = "Next 1H support if momentum continues."
        plan["invalidation_condition"] = "5m closes back above the trigger zone or 15m momentum turns bullish."
        plan["note"] = "Educational scalp idea only. Do not enter without live confirmation and risk management."

    return {
        "decision": decision,
        "signal_type": signal_type,
        "confidence": confidence,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "current_price": current_price,
        "key_levels": key_levels,
        "scalp_plan": plan,
        "scoring_reasons": reasons[:30]
    }


@app.get("/xauusd/scalp")
def xauusd_scalp():
    timeframes = {
        "h1": analyze_timeframe("1h", "1H"),
        "m15": analyze_timeframe("15min", "15m"),
        "m5": analyze_timeframe("5min", "5m"),
    }

    snapshot = {
        "symbol": "XAUUSD",
        "analysis_type": "scalp",
        "data_source": "Twelve Data API",
        "server_time": utc_now_iso(),
        "timeframes": timeframes,
        "latest_alert": latest_alert,
        "cache_summary": {"type": "supabase", "enabled": supabase_enabled()},
        "method": {
            "h1": "trend filter",
            "m15": "setup confirmation",
            "m5": "execution timing"
        }
    }

    valid_timeframes = [tf for tf in timeframes.values() if tf.get("ok")]
    if len(valid_timeframes) < 3:
        return {
            "symbol": "XAUUSD",
            "analysis_type": "scalp",
            "data_source": "Twelve Data API",
            "decision": "WAIT",
            "signal_type": "Scalp Wait",
            "confidence": 0,
            "current_price": None,
            "message": "Not enough valid scalp timeframes for a reliable scalp signal.",
            "risk_note": "Educational analysis only. Do not trade without confirmation and risk management.",
            "snapshot": snapshot,
        }

    scalp_data = build_scalp_decision(snapshot)

    return {
        "symbol": "XAUUSD",
        "analysis_type": "scalp",
        "data_source": "Twelve Data API",
        "decision": scalp_data["decision"],
        "signal_type": scalp_data["signal_type"],
        "confidence": scalp_data["confidence"],
        "current_price": scalp_data["current_price"],
        "buy_score": scalp_data["buy_score"],
        "sell_score": scalp_data["sell_score"],
        "execution_timeframe": "5m",
        "confirmation_timeframe": "15m",
        "trend_filter_timeframe": "1H",
        "key_levels": scalp_data["key_levels"],
        "scalp_plan": scalp_data["scalp_plan"],
        "scoring_reasons": scalp_data["scoring_reasons"],
        "risk_note": "Educational analysis only. Do not trade without confirmation and risk management.",
        "snapshot": snapshot,
    }

