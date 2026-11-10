"""The four tools Delphi's agents call, and the data they read: public Kalshi prices and NBA results."""

import csv
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from agents import RunContextWrapper, function_tool

from scripts import kalshi

DATA = Path(__file__).parent.parent / "data"
EASTERN = ZoneInfo("America/New_York")
LOGO = "https://cdn.nba.com/logos/nba/{}/primary/L/logo.svg"
DATA_URL = "/data/"
SKIPPED_GAME_TYPES = {"Preseason", "All-Star Game"}

GAMES = list(csv.DictReader((DATA / "games.csv").open()))
BETS = list(csv.DictReader((DATA / "kalshi_t3.csv").open()))
TIPS = {game["gameId"]: game["gameDateTimeEst"] for game in GAMES}
NIGHTS = sorted({row["date"] for row in BETS})
SEASON_START = NIGHTS[0]
CODES = {str(team_id): code for code, team_id in kalshi.TEAMS.items()}


@dataclass
class Run:
    """One consultation's state, shared by every agent and tool in it. The model never sees it."""

    question: str
    mode: str
    night: str
    as_of: str
    hooks: object
    slate: dict = field(default_factory=dict)
    started: list = field(default_factory=list)
    findings: dict = field(default_factory=dict)
    records: dict = field(default_factory=dict)
    history: dict = field(default_factory=dict)
    orders: list = field(default_factory=list)
    analysts: int = 0


#-----Tonight's date in Eastern time----
def today():
    return datetime.now(EASTERN).date().isoformat()


#-----Pick the night a request is about: tonight in Live, a night of this season in Replay----
def resolve(mode, date):
    if mode != "replay":
        return "live", today()

    night = date or NIGHTS[-1]
    if not SEASON_START <= night < today():
        raise ValueError(f"Replay covers this season: pick a night from {SEASON_START} to yesterday.")
    return "replay", night


#-----The moment the data is frozen at: now in Live, the night's first tip minus 3 minutes in Replay----
def frozen_at(mode, night):
    if mode == "live":
        return datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M ET")

    tips = [TIPS[row["game_id"]] for row in BETS if row["date"] == night]
    first = datetime.fromisoformat(min(tips)) if tips else datetime.fromisoformat(night)
    return (first - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M ET")


#-----A Kalshi dollar string as a number, or None when there is no price----
def price(value):
    return float(value) if value not in (None, "") else None


#-----Midpoint of a bid and an ask, the chance the crowd gives a team----
def mid(bid, ask):
    return round((bid + ask) / 2, 3) if bid is not None and ask is not None else None


#-----Ten-cent bucket of a price: 0.91 falls in bucket 9----
def bucket_of(value):
    return int(round(float(value) * 100) // 10)


#-----Kalshi's favorite 3 minutes before tip-off in every game of this season: the side with the higher midpoint----
def favorites(rows):
    best = {}

    for row in rows:
        middle = mid(price(row["bid_t3"]), price(row["ask_t3"]))
        if middle is not None and middle > best.get(row["game_id"], ("", -1.0))[1]:
            best[row["game_id"]] = (row["team"], middle)

    return {game_id: team for game_id, (team, _) in best.items()}


FAVORITES = favorites(BETS)


#-----NBA logo URLs for both teams of a game----
def logos(game):
    return {code: LOGO.format(kalshi.TEAMS[code]) for code in (game["away"], game["home"])}


#-----Describe one game for the agents: both markets, the crowd's favorite and the source----
def game_entry(event_ticker, tip, markets, source, **extra):
    _, away, home = kalshi.split_event(event_ticker)
    mids = {code: mid(market["bid"], market["ask"]) for code, market in markets.items()}
    priced = {code: value for code, value in mids.items() if value is not None}
    favorite = max(priced, key=priced.get) if priced else None

    return {"event_ticker": event_ticker, "game": f"{away} at {home}", "away": away, "home": home,
            "tip": tip.strftime("%Y-%m-%d %H:%M ET"), **extra, "markets": markets,
            "crowd": {"favorite": favorite, "implied": priced.get(favorite)}, "source": source}


#-----Tonight's games and open bets, straight from Kalshi----
def live_slate(night):
    data, error = kalshi.get("/events", {"series_ticker": kalshi.SERIES, "status": "open",
                                         "with_nested_markets": "true", "limit": 200})
    if error:
        return [], error

    now = datetime.now(EASTERN)
    games = []
    for event in data.get("events", []):
        if f"-{kalshi.night_tag(night)}" not in event["event_ticker"] or len(event.get("markets") or []) != 2:
            continue

        markets = {market["ticker"].rsplit("-", 1)[1]: market for market in event["markets"]}
        occurrence = datetime.fromisoformat(event["markets"][0]["occurrence_datetime"].replace("Z", "+00:00"))
        tip = (occurrence - timedelta(hours=3)).astimezone(EASTERN)
        minutes = round((tip - now).total_seconds() / 60)
        prices = {code: {"ticker": market["ticker"], "question": market.get("title", ""), "bid": price(market.get("yes_bid_dollars")),
                         "ask": price(market.get("yes_ask_dollars")), "volume": round(float(market.get("volume_fp") or 0))}
                  for code, market in markets.items()}

        source = f"[Kalshi live]({kalshi.BASE}/events/{event['event_ticker']})"
        games.append(game_entry(event["event_ticker"], tip, prices, source, minutes_to_tip=minutes,
                                provisional=not 0 <= minutes <= 5))

    return sorted(games, key=lambda game: (game["tip"], game["event_ticker"])), None


#-----One past night's games, priced 3 minutes before tip-off, without results----
def replay_slate(night):
    events = {}
    for row in BETS:
        if row["date"] == night:
            events.setdefault(row["ticker"].rsplit("-", 1)[0], {})[row["team"]] = row

    games = []
    for event_ticker, rows in events.items():
        tip = datetime.fromisoformat(TIPS[next(iter(rows.values()))["game_id"]])
        markets = {code: {"ticker": row["ticker"], "question": row["question"], "bid": price(row["bid_t3"]),
                          "ask": price(row["ask_t3"])} for code, row in rows.items()}
        source = f"[Kalshi T-3 prices]({DATA_URL}kalshi_t3.csv)"
        games.append(game_entry(event_ticker, tip, markets, source, price_at="3 minutes before tip-off",
                                provisional=False))

    return sorted(games, key=lambda game: (game["tip"], game["event_ticker"])), None


#-----The slate in either mode----
def slate(mode, night):
    return live_slate(night) if mode == "live" else replay_slate(night)


#-----Wins and losses from one True or False per game, e.g. 9-3----
def record(results):
    results = list(results)
    return f"{sum(results)}-{len(results) - sum(results)}"


#-----One past game seen from one team: date, opponent, result, score, and whether Kalshi's favorite won----
def result_of(game, team_id):
    home = game["hometeamId"] == team_id
    favorite = FAVORITES.get(game["gameId"])

    return {"date": game["gameDateTimeEst"][:10], "opponent": CODES[game["awayteamId" if home else "hometeamId"]],
            "home": home, "won": game["winner"] == team_id,
            "score": f"{CODES[game['awayteamId']]} {int(float(game['awayScore']))} at "
                     f"{CODES[game['hometeamId']]} {int(float(game['homeScore']))}",
            "kalshi_favorite": favorite, "kalshi_right": favorite == CODES[game["winner"]] if favorite else None}


#-----One team's results before a night: season record, last 10, home, away, every game this season with Kalshi's call, and the last 3 against the opponent----
def team_records(team, opponent, night):
    team_id, opponent_id = str(kalshi.TEAMS[team]), str(kalshi.TEAMS[opponent])
    played = [game for game in GAMES if game["gameDateTimeEst"][:10] < night and game["winner"]
              and game["gameType"] not in SKIPPED_GAME_TYPES and team_id in (game["hometeamId"], game["awayteamId"])]
    season = [result_of(game, team_id) for game in played if game["gameDateTimeEst"] >= SEASON_START]
    called = [game["kalshi_right"] for game in season if game["kalshi_right"] is not None]
    against = [game for game in played if opponent_id in (game["hometeamId"], game["awayteamId"])][-3:]

    return {"team": team, "season": record(game["won"] for game in season),
            "last_10": record(game["winner"] == team_id for game in played[-10:]),
            "home": record(game["won"] for game in season if game["home"]),
            "away": record(game["won"] for game in season if not game["home"]),
            "kalshi_called_right": f"{sum(called)} of {len(called)}",
            "this_season": season,
            "last_3_vs_opponent": [result_of(game, team_id) for game in against],
            "source": f"[NBA results]({DATA_URL}games.csv)"}


#-----How often teams priced in the same 10-cent bucket won, this season before the night----
def price_history(team, ask, night):
    rows = [row for row in BETS if row["date"] < night and row["ask_t3"] and bucket_of(row["ask_t3"]) == bucket_of(ask)]
    wins = sum(int(row["won"]) for row in rows)
    start = bucket_of(ask) / 10

    return {"team": team, "ask": ask, "bucket": f"{start:.1f} to {start + 0.1:.1f}", "games": len(rows), "won": wins,
            "win_rate": round(wins / len(rows), 3) if rows else None,
            "source": f"[Kalshi T-3 history]({DATA_URL}kalshi_t3.csv)"}


#-----Walk one market's live order book for a dollar amount, with fee, payout and loss----
def order(ticker, side, dollars):
    if side not in ("yes", "no") or isinstance(dollars, bool) or not isinstance(dollars, (int, float)) \
            or not math.isfinite(dollars) or dollars <= 0:
        return {"error": "Use side 'yes' or 'no' and an amount in dollars greater than 0."}
    if not ticker.startswith(kalshi.SERIES + "-"):
        return {"error": "Use a team's market ticker from get_current_games, e.g. KXNBAGAME-26NOV10MILNYK-NYK."}

    data, error = kalshi.get(f"/markets/{quote(ticker, safe='')}/orderbook")
    if error:
        return {"error": error}
    fill = kalshi.walk_book(data.get("orderbook_fp"), side, dollars)
    if not fill["contracts"]:
        return {"error": f"Nobody is selling {side} on {ticker} right now, so there is no fill to show."}

    cost = round(fill["spent"] + fill["fee"], 2)
    return {"ticker": ticker, "side": side, "dollars": dollars, **fill, "total_cost": cost,
            "payout_if_right": fill["contracts"], "profit_if_right": round(fill["contracts"] - cost, 2),
            "loss_if_wrong": cost, "unspent": round(dollars - fill["spent"], 2),
            "as_of": datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S ET"),
            "source": f"[Kalshi order book]({kalshi.BASE}/markets/{ticker}/orderbook)",
            "note": "A snapshot of the order book, with the taker fee on top. Delphi simulates, it never places trades."}


#-----Hand the model an error it can act on instead of a stack trace----
def tool_error(ctx, error):
    return json.dumps({"error": f"{type(error).__name__}: {str(error)[:300]}. Check the arguments and try once more."})


#-----Tool: the night's games and their prices, stored in the run for the analysts and the cards----
@function_tool(failure_error_function=tool_error)
def get_current_games(ctx: RunContextWrapper[Run]) -> str:
    """Get the night's NBA games on Kalshi: both teams, each team's market ticker with bid and ask, tip-off
    time, and the crowd's favorite. Live prices are provisional until 5 minutes before tip-off, and a negative
    minutes_to_tip means the game has started. Replay prices are from 3 minutes before tip-off.
    """
    run = ctx.context
    games, error = slate(run.mode, run.night)
    if error:
        return json.dumps({"error": error})

    run.slate = {game["event_ticker"]: game for game in games}
    note = None if games else "Kalshi lists no NBA games for this night."
    return json.dumps({"mode": run.mode, "night": run.night, "as_of": run.as_of, "games": games, "note": note})


#-----Tool: one team's results before the night----
@function_tool(failure_error_function=tool_error)
def get_historical_records(ctx: RunContextWrapper[Run], team: str, opponent: str) -> str:
    """Get one team's results before tonight from NBA box scores: season record, last 10, home and away
    records, every game this season with Kalshi's favorite and whether it won, how many of this team's games
    Kalshi called right, and the last 3 games against tonight's opponent.

    Args:
        team: The team's 3-letter Kalshi code, e.g. 'NYK'.
        opponent: Tonight's opponent's 3-letter Kalshi code, e.g. 'MIL'.
    """
    team, opponent = team.strip().upper(), opponent.strip().upper()
    if team not in kalshi.TEAMS or opponent not in kalshi.TEAMS or team == opponent:
        return json.dumps({"error": "Use two different 3-letter team codes from the game, e.g. team 'NYK', opponent 'MIL'."})

    result = team_records(team, opponent, ctx.context.night)
    ctx.context.records[team] = result
    return json.dumps(result)


#-----Tool: how often teams at this price won, this season before the night----
@function_tool(failure_error_function=tool_error)
def get_historical_bets(ctx: RunContextWrapper[Run], team: str, ask: float) -> str:
    """How often teams priced like this one, 3 minutes before tip-off, actually won this season before
    tonight: the 10-cent price bucket, how many games, how many won, and the win rate.

    Args:
        team: The team's 3-letter Kalshi code, e.g. 'NYK'.
        ask: That team's ask from the game, in dollars between 0 and 1, e.g. 0.91.
    """
    team = team.strip().upper()
    if team not in kalshi.TEAMS or not 0 < ask < 1:
        return json.dumps({"error": "Use a 3-letter team code and that team's ask in dollars between 0 and 1, e.g. 0.91."})

    result = price_history(team, ask, ctx.context.night)
    ctx.context.history[team] = result
    return json.dumps(result)


#-----Tool: walk the live order book for an amount, never in Replay----
@function_tool(failure_error_function=tool_error)
def simulate_order(ctx: RunContextWrapper[Run], ticker: str, side: str, dollars: float) -> str:
    """Simulate spending a dollar amount on one market's live order book, cheapest offers first: contracts,
    average and worst price, fee, payout if right and loss if wrong. Live mode only. Never places a trade.

    Args:
        ticker: A team's market ticker, e.g. 'KXNBAGAME-26NOV10MILNYK-NYK'. YES on it means that team wins.
        side: 'yes' to back the team in the ticker, 'no' to back its opponent.
        dollars: Amount in US dollars, greater than 0.
    """
    if ctx.context.mode == "replay":
        return json.dumps({"error": "Kalshi keeps no past order books, so a Replay night cannot be simulated. "
                                    "Switch to Live for tonight's games."})

    result = order(ticker.strip().upper(), side.strip().lower(), dollars)
    if "error" not in result:
        ctx.context.orders.append(result)
    return json.dumps(result)
