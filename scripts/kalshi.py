"""Public Kalshi data for the KXNBAGAME series. No keys, no orders."""

import math
import time
from datetime import datetime

import httpx

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = "KXNBAGAME"
FEE_RATE = 0.07
TEAMS = {"ATL": 1610612737, "BOS": 1610612738, "CLE": 1610612739, "NOP": 1610612740, "CHI": 1610612741,
         "DAL": 1610612742, "DEN": 1610612743, "GSW": 1610612744, "HOU": 1610612745, "LAC": 1610612746,
         "LAL": 1610612747, "MIA": 1610612748, "MIL": 1610612749, "MIN": 1610612750, "BKN": 1610612751,
         "NYK": 1610612752, "ORL": 1610612753, "IND": 1610612754, "PHI": 1610612755, "PHX": 1610612756,
         "POR": 1610612757, "SAC": 1610612758, "SAS": 1610612759, "OKC": 1610612760, "TOR": 1610612761,
         "UTA": 1610612762, "MEM": 1610612763, "WAS": 1610612764, "DET": 1610612765, "CHA": 1610612766}


#-----Request public Kalshi data, retrying rate limits and dropped connections----
def get(path, params=None, tries=2):
    error = "Kalshi did not answer. Try again in a minute."

    for attempt in range(tries):
        if attempt:
            time.sleep(attempt)
        try:
            response = httpx.get(BASE + path, params=params, timeout=10)
        except httpx.HTTPError:
            error = "Kalshi is unreachable right now. Try again in a minute."
            continue

        if response.status_code == 200:
            return response.json(), None
        if response.status_code == 404:
            return None, "Kalshi has nothing at that ticker. Use a ticker from get_current_games."
        error = f"Kalshi returned HTTP {response.status_code}. Try again in a minute."
        if response.status_code != 429 and response.status_code < 500:
            break

    return None, error


#-----Split an event ticker into its night, away team and home team----
def split_event(event_ticker):
    code = event_ticker.split("-")[1]
    night = datetime.strptime(code[:7], "%y%b%d").date().isoformat()
    return night, code[7:10], code[10:13]


#-----Build the ticker tag of one night, e.g. 2026-11-10 gives 26NOV10----
def night_tag(night):
    return datetime.strptime(night, "%Y-%m-%d").strftime("%y%b%d").upper()


#-----Taker fee per contract before rounding: 7% of price times (1 - price)----
def fee_per_contract(price):
    return FEE_RATE * price * (1 - price)


#-----Fill a dollar amount from the cheapest live offers, level by level (from The Big Short)----
def walk_book(orderbook_fp, side, dollars):
    opposite = (orderbook_fp or {}).get("no_dollars" if side == "yes" else "yes_dollars") or []
    asks = sorted((round(1 - float(p), 4), float(q)) for p, q in opposite
                  if 0 < float(p) < 1 and math.isfinite(float(q)) and float(q) > 0)
    spent, contracts, levels = 0.0, 0.0, []

    for price, size in asks:
        take = min(size, max(dollars - spent, 0) / price)
        contracts += take
        spent += take * price
        levels.append({"price": price, "size": round(size, 2), "taken": round(take, 2)})

    used = [level for level in levels if level["taken"] > 0]
    fee = math.ceil(round(sum(fee_per_contract(level["price"]) * level["taken"] for level in used) * 100, 6)) / 100

    return {
        "best_ask": asks[0][0] if asks else None,
        "avg_price": round(spent / contracts, 4) if contracts else None,
        "worst_price": used[-1]["price"] if used else None, "contracts": round(contracts, 2),
        "levels_used": len(used), "spent": round(spent, 2), "fee": fee,
        "fully_filled": spent >= dollars - 0.000001,
        "levels": levels[:max(len(used), 10)],
    }
