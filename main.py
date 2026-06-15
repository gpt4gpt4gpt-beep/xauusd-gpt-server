from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone
import os
import json
import urllib.parse
import urllib.request

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


def fetch_twelve_data(symbol="XAU/USD", interval="15min", outputsize=120):
    api_key = get_api_key()

    if not api_key:
        return {
            "ok": False,
            "error": "TWELVE_DATA_API_KEY is missing"
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

        if "status" in data and data.get("status") == "error":
            return {
                "ok": False,
                "error": data.get("message", "Twelve Data API error"),
                "raw": data
            }

        values = data.get("values", [])

        candles = []
        for item in reversed(values):
            candles.append({
                "time": item.get("datetime"),
                "open": float(item.get("open")),
                "high": float(item.get("high")),
                "low": float(item.get("low")),
                "close": float(item.get("close")),
                "volume": float(item.get("volume", 0) or 0)
            })

        return {
            "ok": True,
            "symbol": symbol,
            "interval": interval,
            "candles": candles,
            "meta": data.get("meta", {})
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def ema(values, length):
    if len(values) < length:
        return None

    k = 2 / (length + 1)
    ema_value = sum(values[:length]) / length

    for price in values[length:]:
        ema_value = price * k + ema_value * (1 - k)

    return round(ema_value, 3)


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

    ema_12 = ema(values, 12)
    ema_26 = ema(values, 26)

    if ema_12 is None or ema_26 is None:
        return {
            "macd": None,
            "signal": None,
            "histogram": None,
            "bias": "unknown"
        }

    macd_value = round(ema_12 - ema_26, 3)

    bias = "bullish" if macd_value > 0 else "bearish" if macd_value < 0 else "neutral"

    return {
        "macd": macd_value,
        "signal": None,
        "histogram": None,
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


def support_resistance(candles):
    if len(candles) < 30:
        return {
            "support": None,
            "resistance": None
        }

    recent = candles[-40:]
    lows = [c["low"] for c in recent]
    highs = [c["high"] for c in recent]

    support = round(min(lows), 3)
    resistance = round(max(highs), 3)

    return {
        "support": support,
        "resistance": resistance
    }


def analyze_timeframe(interval, label):
    result = fetch_twelve_data(interval=interval)

    if not result.get("ok"):
        return {
            "label": label,
            "interval": interval,
            "ok": False,
            "error": result.get("error")
        }

    candles = result["candles"]

    if len(candles) < 30:
        return {
            "label": label,
            "interval": interval,
            "ok": False,
            "error": "Not enough candle data"
        }

    closes = [c["close"] for c in candles]
    latest = candles[-1]

    ema_20 = ema(closes, 20)
    ema_50 = ema(closes, 50)
    ema_200 = ema(closes, 200) if len(closes) >= 200 else None

    current_rsi = rsi(closes)
    current_macd = macd(closes)
    current_atr = atr(candles)
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
        "support": sr["support"],
        "resistance": sr["resistance"]
    }


def build_decision(snapshot):
    confirmations_buy = 0
    confirmations_sell = 0

    for tf in snapshot["timeframes"].values():
        if not tf.get("ok"):
            continue

        if tf.get("bias") == "bullish":
            confirmations_buy += 1

        if tf.get("bias") == "bearish":
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

    decision = "WAIT"
    signal_type = "Wait"
    confidence = 0

    if confirmations_buy >= 7 and confirmations_buy > confirmations_sell + 2:
        decision = "BUY"
        signal_type = "Medium Buy"
        confidence = min(80, confirmations_buy * 8)

    elif confirmations_sell >= 7 and confirmations_sell > confirmations_buy + 2:
        decision = "SELL"
        signal_type = "Medium Sell"
        confidence = min(80, confirmations_sell * 8)

    else:
        confidence = max(confirmations_buy, confirmations_sell) * 6

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
        "message": "XAUUSD GPT Server is live"
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat()
    }


@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request):
    global latest_alert

    try:
        data = await request.json()
    except Exception:
        data = {
            "raw": await request.body()
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
        "h1": analyze_timeframe("1h", "1H"),
        "m15": analyze_timeframe("15min", "15m"),
        "m5": analyze_timeframe("5min", "5m")
    }

    snapshot = {
        "symbol": "XAUUSD",
        "data_source": "Twelve Data API",
        "server_time": datetime.now(timezone.utc).isoformat(),
        "timeframes": timeframes,
        "latest_alert": latest_alert
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
            "message": "Market data returned, but not enough valid timeframes for a reliable signal.",
            "snapshot": snapshot
        }

    decision_data = build_decision(snapshot)

    current_price = None
    if timeframes["m5"].get("ok"):
        current_price = timeframes["m5"].get("current_price")
    elif timeframes["m15"].get("ok"):
        current_price = timeframes["m15"].get("current_price")

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
