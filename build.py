"""Run on the laptop before each deploy. Writes data/games.csv and data/kalshi_t3.csv.

Only this season's games get a price 3 minutes before tip-off: they are the nights Delphi can replay,
and the history get_historical_bets looks at. That is about 450 Kalshi calls, under two minutes.
"""

import csv
import io
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from scripts import kalshi

KAGGLE_GAMES = "https://www.kaggle.com/api/v1/datasets/download/eoinamoore/historical-nba-data-and-player-box-scores/Games.csv"
DATA = Path(__file__).parent / "data"
EASTERN = ZoneInfo("America/New_York")
FIRST_DAY = "2024-10-01"
TODAY = date.today()
SEASON_START = f"{TODAY.year if TODAY.month >= 8 else TODAY.year - 1}-10-01"
GAME_COLUMNS = ["gameId", "gameDateTimeEst", "hometeamId", "awayteamId", "homeScore", "awayScore", "winner", "gameType"]
BET_COLUMNS = ["ticker", "question", "game_id", "date", "team", "opp", "home", "bid_t5", "ask_t5", "bid_t3", "ask_t3",
               "bid_t1", "ask_t1", "won"]
NBA_IDS = {str(team_id) for team_id in kalshi.TEAMS.values()}
MINUTES = (5, 3, 1)
REQUEST_GAP = 0.2


#-----Write rows to a CSV with a fixed column order----
def write_csv(path, columns, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


#-----Download Kaggle's Games.csv and keep NBA games since the 2024-25 season----
def build_games():
    response = httpx.get(KAGGLE_GAMES, follow_redirects=True, timeout=120)
    response.raise_for_status()

    rows = [{column: row[column] for column in GAME_COLUMNS} for row in csv.DictReader(io.StringIO(response.text))
            if row["gameDateTimeEst"] >= FIRST_DAY and {row["hometeamId"], row["awayteamId"]} <= NBA_IDS]
    rows.sort(key=lambda row: (row["gameDateTimeEst"], row["gameId"]))

    write_csv(DATA / "games.csv", GAME_COLUMNS, rows)
    return rows


#-----List this season's settled game markets, page by page----
def list_markets():
    markets, cursor = [], ""

    while True:
        data, error = kalshi.get("/events", {"series_ticker": kalshi.SERIES, "status": "settled",
                                             "with_nested_markets": "true", "limit": 200, "cursor": cursor}, tries=4)
        if error:
            raise SystemExit(error)

        for event in data["events"]:
            if kalshi.split_event(event["event_ticker"])[0] >= SEASON_START:
                markets += [market for market in event.get("markets") or [] if market.get("result") in ("yes", "no")]

        cursor = data.get("cursor")
        if not cursor:
            return markets


#-----Read bid and ask 5, 3 and 1 minutes before tip-off from 1-minute candles----
def prices_before(ticker, tip):
    end = int(tip.timestamp())
    data, _ = kalshi.get(f"/series/{kalshi.SERIES}/markets/{ticker}/candlesticks",
                         {"start_ts": end - 360, "end_ts": end, "period_interval": 1}, tries=4)
    candles = sorted((data or {}).get("candlesticks") or [], key=lambda candle: candle["end_period_ts"])

    prices = {}
    for minutes in MINUTES:
        before = [candle for candle in candles if candle["end_period_ts"] <= end - 60 * minutes]
        last = before[-1] if before else {}
        for side in ("bid", "ask"):
            value = (last.get(f"yes_{side}") or {}).get("close_dollars")
            prices[f"{side}_t{minutes}"] = f"{float(value):g}" if value else ""

    return prices


#-----Match each market to its Kaggle game and price it before tip-off----
def build_bets(games, markets):
    by_matchup = {(game["gameDateTimeEst"][:10], game["awayteamId"], game["hometeamId"]): game for game in games}
    rows = []

    for market in sorted(markets, key=lambda market: market["ticker"]):
        night, away, home = kalshi.split_event(market["event_ticker"])
        team = market["ticker"].rsplit("-", 1)[1]
        game = by_matchup.get((night, str(kalshi.TEAMS.get(away)), str(kalshi.TEAMS.get(home))))
        if game is None or team not in (away, home):
            continue

        started = time.monotonic()
        tip = datetime.fromisoformat(game["gameDateTimeEst"]).replace(tzinfo=EASTERN)
        rows.append({"ticker": market["ticker"], "question": market.get("title", ""), "game_id": game["gameId"],
                     "date": night, "team": team,
                     "opp": home if team == away else away, "home": int(team == home),
                     **prices_before(market["ticker"], tip), "won": int(market["result"] == "yes")})
        time.sleep(max(0.0, REQUEST_GAP - (time.monotonic() - started)))

    rows.sort(key=lambda row: (row["date"], row["game_id"], row["ticker"]))
    write_csv(DATA / "kalshi_t3.csv", BET_COLUMNS, rows)
    return rows


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    games = build_games()
    print(f"{len(games)} NBA games since {FIRST_DAY} written to data/games.csv")

    markets = list_markets()
    print(f"{len(markets)} settled markets since {SEASON_START}, pricing them (about 5 per second)", flush=True)

    rows = build_bets(games, markets)
    missing = sum(1 for row in rows if not row["ask_t3"])
    print(f"{len(rows)} markets written to data/kalshi_t3.csv, {missing} without a price 3 minutes before tip-off")
