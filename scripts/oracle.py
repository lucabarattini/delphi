"""The oracle: a Planner sends one Game Analyst per NBA game, and a Verdict Writer turns their findings into
the answer and Delphi's pick. The four data tools live in tools.py."""

import asyncio
import json
import time

import litellm
from agents import Agent, ModelSettings, RunContextWrapper, Runner, function_tool, set_tracing_disabled
from agents.exceptions import MaxTurnsExceeded
from agents.extensions.models.litellm_model import LitellmModel
from openai.types.shared import Reasoning

from scripts.context_panel import PanelHooks
from scripts.tools import (BETS, Run, frozen_at, get_current_games, get_historical_bets, get_historical_records, logos,
                           resolve, simulate_order, tool_error)

set_tracing_disabled(True)
litellm.suppress_debug_info = True

MODEL = "vertex_ai/gemini-4-argon"
REASONING_EFFORT = "low"
ARGON = LitellmModel(model=MODEL, api_key="unused")
SETTINGS = ModelSettings(reasoning=Reasoning(effort=REASONING_EFFORT),
                         extra_args={"vertex_location": "global", "timeout": 30, "num_retries": 1})
WRITER_SETTINGS = ModelSettings(reasoning=Reasoning(effort=REASONING_EFFORT),
                                extra_args={"vertex_location": "global", "timeout": 75, "num_retries": 1})

MAX_ANALYSTS = 15
ANALYST_SLOTS = asyncio.Semaphore(8)
RUN_SECONDS = 240

sessions: dict[str, dict] = {}


class Trail(PanelHooks):
    """The professor's PanelHooks, which stream every agent's context to the timeline panel, plus each
    agent's time and every tool call with the agent that made it, for /chat."""

    #-----Keep the panel's contexts and stream callback, the tool calls and each agent's start time----
    def __init__(self, contexts, emit):
        super().__init__(contexts, emit)
        self.calls = []
        self.clock = {}

    #-----Note when an agent starts----
    async def on_agent_start(self, context, agent):
        self.clock[agent.name] = time.monotonic()
        await super().on_agent_start(context, agent)

    #-----Log how long the agent took----
    async def on_agent_end(self, context, agent, output):
        seconds = time.monotonic() - self.clock.get(agent.name, time.monotonic())
        self.activity(agent.name, f"finished in {seconds:.1f} s")

    #-----Record a finished tool call with its agent, arguments and result----
    async def on_tool_end(self, context, agent, tool, result):
        raw = getattr(context, "tool_arguments", "") or "{}"
        try:
            args = json.loads(raw)
        except ValueError:
            args = raw

        call = {"agent": agent.name, "name": tool.name, "args": args, "result": str(result)}
        self.calls.append(call)
        self.emit({"type": "tool_call", **call})


#-----Tool: send one Game Analyst to one game, at most 8 at a time----
@function_tool(failure_error_function=tool_error)
async def analyze_game(ctx: RunContextWrapper[Run], event_ticker: str) -> str:
    """Send one Game Analyst to study one game. Returns its findings with sources.

    Args:
        event_ticker: An event ticker from get_current_games, e.g. 'KXNBAGAME-26NOV10MILNYK'.
    """
    run = ctx.context
    if run.analysts >= MAX_ANALYSTS:
        return "No Game Analysts left. Reply DONE."
    game = run.slate.get(event_ticker.strip().upper())
    if game is None:
        return json.dumps({"error": "Unknown game. Use an event_ticker from get_current_games."})

    run.analysts += 1
    run.started.append(game["event_ticker"])
    agent = game_analyst.clone(name=f"Game Analyst {run.analysts}")
    async with ANALYST_SLOTS:
        result = await Runner.run(agent, json.dumps({"question": run.question, "game": game}), context=run,
                                  hooks=run.hooks, max_turns=4)

    run.findings[game["event_ticker"]] = str(result.final_output)
    return str(result.final_output)


planner = Agent(
    name="Planner",
    instructions="""\
<role>You plan the research for Delphi, which covers who wins each NBA game on Kalshi. You never answer with
prices or verdicts yourself.</role>
<input>{"question": "...", "mode": "live|replay", "date": "YYYY-MM-DD", "last_answer": "...",
"last_games": [{"game": "MIL at NYK", "event_ticker": "KXNBAGAME-26NOV10MILNYK",
"tickers": {"MIL": "KXNBAGAME-26NOV10MILNYK-MIL", "NYK": "KXNBAGAME-26NOV10MILNYK-NYK"}}]}</input>
<steps>
1. Vague question (no game, team or night, and nothing in last_answer)? Or an ambiguous team ("New York":
   Knicks or Nets, "Los Angeles": Lakers or Clippers)? Reply exactly: CLARIFY: <one short question>.
2. Something Delphi has no data for (player stats, injuries, futures, season awards, other sports)? Reply
   exactly: ANSWER: <what Delphi cannot answer, and what it can>.
3. A follow-up with a dollar amount on a game already discussed? Call simulate_order with that team's ticker
   from last_games and side "yes", then reply ANSWER: <the fill in plain words, ending with "Delphi
   simulates, it never places trades.">.
4. Otherwise call get_current_games, then analyze_game once per game the question is about, all in the same
   turn. A question about the night, the slate or "each game" is about every game.
5. If a finding reports an error or a missing price, call analyze_game again for that game, once.
6. Reply exactly: DONE.
</steps>
<examples>
{"question": "is New York a good bet?", "reply": "CLARIFY: The Knicks or the Nets?"}
{"question": "will Giannis score 30?", "reply": "ANSWER: Delphi only covers who wins each game: the Kalshi price before tip-off, how often teams at that price won, and what an order would get you. It has no player stats."}
{"question": "who will win the 2027 NBA title?", "reply": "ANSWER: Delphi only covers single games on the night you pick, not futures. Ask it about tonight's games, or replay a past night."}
</examples>""",
    model=ARGON,
    model_settings=SETTINGS,
    tools=[get_current_games, analyze_game, simulate_order],
)

game_analyst = Agent(
    name="Game Analyst",
    instructions="""\
<role>You study one NBA game and report evidence. You never give advice.</role>
<input>{"question": "the user's question", "game": {both teams, their market tickers, bid and ask, tip time,
provisional flag, the crowd's favorite and the source of the prices}}</input>
<steps>
1. In one turn, call for each team: get_historical_records (with the other team as opponent) and
   get_historical_bets (with that team's ask). Four calls in all. Skip get_historical_bets for a team whose
   ask is null, and say so in gaps.
2. Reply with this JSON on one line, without code fences, and nothing else. Copy every number from the input
   and the tools; compute nothing. Copy each source exactly as the tool gave it. At most four facts, each
   under 20 words: first the ones that answer the question, then how often Kalshi's favorite won in each
   team's games this season (kalshi_called_right), then records and the last meetings.
</steps>
<output>
{"game": "MIL at NYK", "crowd": {"favorite": "NYK", "implied": 0.905, "provisional": true, "source": "[Kalshi live](...)"}, "history": {"MIL": {"ask": 0.10, "games": 22, "won": 3, "win_rate": 0.136, "source": "[Kalshi T-3 history](/data/kalshi_t3.csv)"}, "NYK": {"ask": 0.91, "games": 4, "won": 4, "win_rate": 1.0, "source": "[Kalshi T-3 history](/data/kalshi_t3.csv)"}}, "facts": ["NYK 9-3 this season [NBA results](/data/games.csv)", "..."], "gaps": ["what the data could not answer"]}
</output>""",
    model=ARGON,
    model_settings=SETTINGS,
    tools=[get_historical_records, get_historical_bets],
)

verdict_writer = Agent(
    name="Verdict Writer",
    instructions="""\
<role>You are Delphi's voice. You write the answer from the question and the Game Analysts' JSON. You never invent
a number.</role>
<input>The question, the mode and night, the night's games, one JSON finding per analyzed game, and the games
that did not finish in time.</input>
<format>
Exactly two lines, with no headings, no list and no section per game: the page shows every game under your answer.
1. One short sentence, at most 25 words, that answers the question. Do not list the games or repeat their numbers.
   If a game did not finish, say so here.
2. One sentence, at most 35 words, that starts with "Delphi's pick:" and names the one team to back and its price,
   with the reason: a high crowd price, together with Kalshi's favorite often winning in both teams' games this
   season (kalshi_called_right). In Replay mode, start with "Delphi's pick would have been:" instead.
</format>
<citations>End each sentence with the markdown links of its sources, each link once, exactly as given.</citations>
<style>Plain words, no hype, no long dashes. In Live mode the prices are provisional. Never say who actually won.</style>""",
    model=ARGON,
    model_settings=WRITER_SETTINGS,
)


#-----Turn the findings into the cited answer, or fall back to the cards alone----
async def write(message, run):
    unfinished = [run.slate[ticker]["game"] for ticker in dict.fromkeys(run.started) if ticker not in run.findings]
    night = {"mode": run.mode, "night": run.night, "as_of": run.as_of,
             "games": [game["game"] for game in run.slate.values()], "unfinished_games": unfinished}
    prompt = (f"<question>{message}</question>\n<night>{json.dumps(night)}</night>\n"
              f"<findings>\n" + "\n\n".join(run.findings.values()) + "\n</findings>")

    try:
        result = await Runner.run(verdict_writer, prompt, context=run, hooks=run.hooks, max_turns=1)
        return str(result.final_output)
    except Exception as error:
        run.hooks.activity("Verdict Writer", f"failed ({type(error).__name__}: {str(error)[:200]}), showing the cards only")
        return ("The Verdict Writer did not finish, so this answer is only the cards above, built from the tool "
                "results. Delphi simulates, it never places trades.")


#-----Build one card per analyzed game from the tool results, never from the prose----
def cards(run):
    built = []

    for ticker in dict.fromkeys(run.started):
        game = run.slate[ticker]
        teams = (game["away"], game["home"])
        built.append({**game, "logos": logos(game), "mode": run.mode,
                      "history": {code: run.history.get(code) for code in teams},
                      "records": {code: run.records.get(code) for code in teams}})

    return built


#-----After the answer, read the night's results and say on each card whether the crowd was right----
def reveal(night, built):
    results = {row["ticker"]: row["won"] for row in BETS if row["date"] == night}
    games = []

    for card in built:
        winner = next((code for code, market in card["markets"].items() if results.get(market["ticker"]) == "1"), None)
        games.append({"event_ticker": card["event_ticker"], "winner": winner,
                      "crowd_right": winner == card["crowd"]["favorite"]})

    return games


#-----Answer one message: plan, fan out, write, and build the cards----
async def consult(message, session_id, mode, date, hooks):
    mode, night = resolve(mode, date)
    session = sessions.setdefault(session_id, {"last_answer": "", "last_games": []})
    run = Run(question=message, mode=mode, night=night, as_of=frozen_at(mode, night), hooks=hooks)
    question = json.dumps({"question": message, "mode": mode, "date": night, **session})

    reply = "DONE"
    try:
        result = await asyncio.wait_for(Runner.run(planner, question, context=run, hooks=run.hooks, max_turns=6),
                                        RUN_SECONDS)
        reply = str(result.final_output).strip()
    except (asyncio.TimeoutError, MaxTurnsExceeded) as error:
        run.hooks.activity("Planner", f"stopped ({type(error).__name__}), writing with the findings so far")

    if reply.startswith(("CLARIFY:", "ANSWER:")):
        markdown = reply.split(":", 1)[1].strip()
    else:
        markdown = await write(message, run)

    session["last_answer"] = markdown
    if run.started:
        session["last_games"] = [{"game": run.slate[ticker]["game"], "event_ticker": ticker,
                                  "tickers": {code: market["ticker"] for code, market in run.slate[ticker]["markets"].items()}}
                                 for ticker in dict.fromkeys(run.started)]

    return {"markdown": markdown, "cards": cards(run), "orders": run.orders, "mode": mode, "night": night,
            "tool_calls": run.hooks.calls}
