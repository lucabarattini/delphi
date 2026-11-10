- Never use the em dash '--' especialy when writing text on charts, body text narratives ecc. 
- Never change the readme.md or write the readme.md without my consent. Your writing is AI-Slop.
- At every checkpoint, update this 'agents.md' file with a log update of what we have done. Stack all the logs s.t. even when the context window gets compacted design choices remain. 
- Write always the import at the beginnings
- Never use inline comments.
- Write the #-----Scope of the function being defined---- for all functions in each script.
- When writing code in a function leave a breakline space between the various important parts of it. Like here
```python
def stage(row, folder, log, reuse_dir=None):
    path = folder / row["filename"]
    partial = path.with_suffix(path.suffix + ".partial")
    source = reuse_dir / row["filename"] if reuse_dir else None

    if source is not None:
        command = ["reuse_verified_local_copy", str(source), str(partial)]
        shutil.copyfile(source, partial)
    else:
        command = ["gcloud", "storage", "cp", "--no-clobber",
                    row["gcs_uri"] + "#" + row["generation"], str(partial)]
        subprocess.run(command, check=True, stdout=log, stderr=log, timeout=1800)

    md5, sha = hashlib.md5(), hashlib.sha256()

    with partial.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            md5.update(block)
            sha.update(block)
    if (partial.stat().st_size != int(row["size_bytes"]) or
            base64.b64encode(md5.digest()).decode() != row["md5_base64"]):
        raise ValueError(f"Staged compressed checksum or size mismatch: {path.name}")

    partial.rename(path)
    path.chmod(0o444)
```
- At the same time, try to do not repeat the same functions over and over and rather try to simplify them by storing them under a scripts/ folder and then importing them. Like from scripts import gcloud_bucket.
- When you reach the end of a python script ask yourself could I cut the complexity of this script by calling other functions already coded, removing useless parts and thinking in the greater picture of the repo?
- Inspect the repository tree, Git status, current code, tests, and the relevant project documents.

## Log

### 2026-11-10: Delphi built from the plan (Deep Research path)
- Started from the professor's Wikipedia deep-research starter. Removed `wikipedia.py`, `context_panel.py` and `context-panel.js`: the plan's right panel (slate plus colored agent steps) replaces the context timeline.
- Removed every raw Kaggle file from `data/` (1.8 GB: play-by-play, player stats, schedules, team stats). `build.py` downloads only `Games.csv` from Kaggle's public API (no CLI, no credentials), so no raw download is kept. `data/` now holds only the two committed CSVs, and `.gitignore` no longer ignores it.
- `Agents.md` renamed to `AGENTS.md`. `CLAUDE.md` imports it with `@AGENTS.md`, so every Claude Code session loads it first.
- First model setup: Planner and Game Analysts on `vertex_ai/gemini-3.8-flash`, Verdict Writer (the oracle's voice) on `vertex_ai/gemini-4-argon`, all at reasoning effort `high`. Replaced the same day, see the next entry.
- LiteLLM is locked at 1.108.0 (a lock bump, no new dependency). 1.83 sends Argon a thinking budget, which Argon rejects (it wants a thinking level), and 1.106.x caps `openai` below 3, which `openai-agents` needs.
- Shared code lives in `scripts/`: `kalshi.py` (client with retries, team codes, ticker parsing, fee, `walk_book` from The Big Short with levels for the SVG) and `calibration.py` (Wilson interval, midpoint, 10-cent buckets, `historical_bets`). `build.py` and `app.py` both import them, and the README calibration table is printed by the same `historical_bets` the agents call.
- `build.py` reuses rows already priced in `data/kalshi_t3.csv`, so a rerun before a deploy only fetches the new nights.
- Citations are markdown links: the public Kalshi API URLs (kalshi.com blocks scripted requests) and the committed CSVs on GitHub.
- `get_historical_bets` also returns the team's T-3 record by season and `favorite_won`, so README query 2 (Thunder, 2025-26) needs no arithmetic from the model.
- Cards are built in code from the tool results stored in the run context. The Replay reveal reads results only after the answer is done.
- Local runs need `VERTEXAI_PROJECT=agentic-ai-for-data-science`: the active gcloud configuration points at a project where Vertex AI is disabled.

### 2026-11-10: all agents on Gemini 4 Argon, checks passed
- Measured why high effort did not fit the plan's time caps: output tokens set the pace (Vertex `global` gives about 40-60 tokens/s), and high effort multiplies them (a Game Analyst's JSON turn: 4,172 tokens at high, 835 at low; the Writer on 7 games: 11,468 tokens at high on Argon). An 8-game night took 322 s at high effort and the Writer timed out.
- Gemini 3.8 Flash was also queueing on Vertex that day: one 85-token call took 1.5 s to 62 s, while Argon took 4-7 s. On Flash at low effort a 15-game night took 263-273 s with 6 to 9 analysts timing out.
- Luca chose: every agent on `vertex_ai/gemini-4-argon` (`MODEL`), reasoning effort per agent in `REASONING_EFFORT`: Planner low, Game Analyst low, Verdict Writer medium. Call timeouts in `CALL_SECONDS` (30, 30, 90). Switching back is a two-line change.
- Fewer tokens per run: sources are short links (`[NBA results](/data/games.csv)`, `[Kalshi T-3 history](/data/kalshi_t3.csv)`), with the app serving `data/` at `/data`. Kalshi sources stay absolute API links. Game Analysts reply with one-line JSON and at most four facts. The Verdict Writer writes four short lines per game.
- Each Game Analyst now gets the user's question with its game (`{"question", "game"}`), so it picks the facts that answer it. Without this, README query 2 (Thunder in 2025-26) came back as "the data does not isolate 2025-26".
- Checks: build 3,328 markets, 1 missing `ask_t3`; `/games` live and replay correct; Replay 2026-11-09 gives 8 cards, all 170 numbers in the report found in tool results, reveal matches both CSVs 8 of 8; CLARIFY and out-of-scope ANSWER work; every `/chat` tool call has its agent; `/simulate` contracts times average equals the amount; the 15-game night 2026-04-12 finished in 124.6 s locally with 15 of 15 analysts done. Cloud Run timing still to check after deploy.
- The rule BID only when the Wilson low end beats price plus fee fires today only for favorites at 90-91 cents (102 of 105 won in that bucket). Tonight's Knicks at 0.90 showed BID.
- README written with Luca's consent, in the style of The Big Short's README. `assets/readme-banner.png` was rendered once from `assets/banner.jpg` with Pillow in a scratch folder (not a project dependency), with Cormorant Garamond and Inter, the same fonts as the page.
- UI checked in headless Chrome: Live slate, Replay run with reveal, order book SVG, phone width without horizontal scroll. NBA logos fail only in headless Chrome (the CDN answers 200 to normal clients); a broken logo now falls back to the team code.

### 2026-11-10: simplified to a small MVP, following Luca's README
- Luca rewrote README.md as the spec. The second question is now "what would happen if I place an order?" (simulate_order). Removed the BID/PASS verdict, the Wilson interval, EV after fees and the calibration table. `scripts/calibration.py` is gone.
- `kalshi_t3.csv` covers only this season (from Oct 1): the nights Delphi can replay and the history it compares against. 440 markets, 34 nights from 2026-10-03, 40 KB. `build.py` uses only Kalshi's live tier (this season is after the historical cutoff), rebuilds from scratch in about 1.5 minutes, and no longer caches rows or prints a table.
- Replay is limited to this season's nights (`resolve` checks it; the date picker gets min and max from `/games`).
- `get_historical_bets` now only says how often teams priced in the same 10-cent bucket won this season before the night. `get_historical_records` dropped the point margin and added `this_season` (every game with opponent, result and score) for the W/L strips on the cards.
- The four data tools and the data they read moved to `tools.py`, so the page can say "The agent uses four tools, all defined in tools.py" (as in The Big Short). `app.py` keeps the agents, `analyze_game` (it launches a sub-agent, not a data tool) and the endpoints. Added `POST /clear` for New chat.
- One reasoning effort for all agents (`low`), all on Argon. Two settings constants: `SETTINGS` (30 s call timeout, one retry) and `WRITER_SETTINGS` (90 s, no retry). A single 90 s timeout for everyone made a stalled call cost 90 s and pushed an 8-game night to the 240 s cap.
- New page on The Big Short's layout, in blue and white: rounded banner with New chat, one intro paragraph on the Delphi oracle and the T-3 assumption (Luca's words), a chat log, and two basketball-shaped starter buttons ("Which games are being played tonight?" and "Which games were played on <night>?" with a date picker). Each answer shows its agent steps, one card per game (crowd pick, how often that price won this season, W/L strip per team, simulate order with the order book SVG, reveal in Replay) and the cited report.
- README banner re-rendered: "NBA GAMES ON KALSHI", "Delphi", "The crowd is the oracle. Shall we trust the crowd?".
- Checked: 15-game night 2026-11-02 in 115.6 s with 15 of 15 cards; clarify and out-of-scope answers; tool_calls carry their agent; $50 follow-up uses simulate_order; headless Chrome walk of both buttons (Replay with reveal, Live with an order), no page errors, no overflow at phone width. Vertex latency varies a lot between runs (the same Live question took 82 s and 195 s).

### 2026-11-10: three panes, the professor's context panel back, basketball colors
- The "Provider List: https://docs.litellm.ai/docs/providers" lines were noise, not failures: LiteLLM 1.108 checks `supports_reasoning("gemini-4-argon")` without the `vertex_ai/` prefix, fails to find a provider, prints that line, catches its own error and goes on. Silenced with `litellm.suppress_debug_info = True` in `app.py`.
- The Verdict Writer now retries once (75 s timeout) and logs the error message when it fails, not just its type.
- Restored the professor's `context_panel.py` and `context-panel.js` unchanged (his inline comments included; his self-check passes). `Trail` now extends his `PanelHooks`, so every agent's context streams to the timeline, including the JSON each Game Analyst returns and the findings the Verdict Writer reads. Each `/run` gets a `run_id` and its own `AgentContexts` in `contexts`; `add_context_panel(app, contexts)` serves `/context` and the script.
- After the first question the page splits in three: chat on the left, the professor's timeline in the middle, the game cards on the far right (`#cards-pane`). His panel is positioned from `index.html` with more specific selectors, not by editing his file. On phones the three stack.
- Cards now show the raw Kalshi question of each market ("Cleveland wins", YES costs 75 cents), the crowd's pick, the reveal in Replay, how often the price won in plain words ("Teams priced 70 to 79 cents won 30 of 44 games (68%)"), and "Was Kalshi right about these teams?": one box per game this season, W or L, orange when Kalshi's T-3 favorite won and black when it lost, plus "8 of 10 right".
- Data: `kalshi_t3.csv` gained a `question` column (the market title). `tools.py` builds `FAVORITES` (Kalshi's T-3 favorite per game) and `get_historical_records` returns `kalshi_called_right` and, per game, `kalshi_favorite` and `kalshi_right`.
- Palette is a basketball's: orange #c8622d, leather #8c3a12, seam black #22150e, cream. The intro is Luca's text with light grammar fixes, in a smaller font across the full width. The starter balls are 112 px, the input bar sticks to the bottom of the window, and sending a question scrolls the banner out of view.
- Testing note: my test runs share the server Luca uses on port 8000, and two runs at once share the 8 analyst slots and Vertex, so one of my Live checks ran past its wait. Do not restart or load the server on 8000 while Luca is using it.

### 2026-11-10: guided paths, orb buttons, black and white boxes
- The chat is a set of fixed paths, each step offered as orb buttons in the chat: "Which games are being played tonight?" or "Which games were played on <night>?" show the slate on the right straight from `/games` (no agents, about 0.3 s); "Give me the full odds" runs the Planner, Game Analysts and Verdict Writer; then Live offers "What would $100 on each favorite get me?" (calls `/simulate` for every favorite, no model, and draws each order book on its card) and "Start over", while Replay reveals the results, adds "The crowd was right in X of Y games", and offers "Replay another night" and "Tonight's games". A small free-text box stays at the bottom as a fallback.
- The orbs port Luca's GradientOrb idea (React, three.js, shadcn) to about 60 lines of plain WebGL in `index.html`: the same GLSL fragment shader on a full-screen triangle, recolored to basketball orange, leather and amber on a dark disc, with faint seams. No React, three.js or Tailwind, so the starter stays a single HTML page. Orbs animate only while visible (IntersectionObserver), stay still under prefers-reduced-motion, and release their WebGL context when their step is chosen.
- Cards: removed "How often this price won this season" and the legend. The "Was Kalshi right about these teams?" boxes are black when Kalshi's favorite won and white with a black border when it lost; the letter is the team's W or L, and "8 of 10 right" sits next to each row.
- The order book chart is sized for the cards pane: gray bars, black for the part your order takes, a blue dashed average line.
- The Verdict Writer writes three to five sentences with no per-game sections; below them the page builds one row per game in code (logos, the crowd's pick, and a bar of how often Kalshi was right about each team). Game Analysts now put `kalshi_called_right` among their facts.
- Tested on a separate server on port 8001 so Luca's server on 8000 was not touched: slate 0.3 s, $100 on 6 favorites, Replay full odds in 100 s with 8 rows, 8 reveals and the next paths, no page errors.

### 2026-11-10: two-line answer with Delphi's pick, README agents section, folders
- The Verdict Writer writes exactly two lines: one short sentence (at most 25 words, no list of games) and "Delphi's pick:" (Replay: "Delphi's pick would have been:"), the one team to back with its price and the reason (high crowd price plus kalshi_called_right for both teams). Each sentence ends with its source links once. The page highlights the pick line and shows the per-game rows below it. The disclaimer lives in the page footer.
- Rewrote the README's "The three agents" section with Luca's consent: a Mermaid flow, a table of each agent's job and hand-off, why cards can't show invented numbers, the time limits, and the tools table.
- Folders: `app.py` (web server, still `uv run app.py`) and `build.py` stay at the root; `scripts/oracle.py` holds the three agents, `analyze_game`, `consult`, `cards`, `reveal` and the session memory; `scripts/tools.py` the four data tools; `scripts/kalshi.py` the Kalshi client; `scripts/context_panel.py` and `scripts/context-panel.js` the professor's pair, unchanged; `web/` the page and `web/assets/` the images; `data/` the two CSVs. The page now says the tools are in `scripts/tools.py`, and the README banner path is `web/assets/readme-banner.png`.
- Checked after the move: every endpoint answers 200, `/simulate`, CLARIFY, and a full Replay of Nov 9 in 84 s with 8 cards and the reveal.
