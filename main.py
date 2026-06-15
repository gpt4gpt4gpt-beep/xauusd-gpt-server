from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone

app = FastAPI(title="XAUUSD GPT Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

latest_alert = {}

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
    data = await request.json()
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
    return {
        "symbol": "XAUUSD",
        "data_source": "server-test",
        "decision": "WAIT",
        "confidence": 0,
        "message": "Server is working. Market Data API is not connected yet.",
        "next_step": "Connect market data API and GPT Action."
    }
