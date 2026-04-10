# Winning the Paradigm Prediction Market Challenge with Claude Code

**1st Place** on the [Paradigm Prediction Market Challenge](https://github.com/danrobinson/prediction-market-challenge) (April 9, 2026 Hackathon).

## Result

Final standings (rescored with fresh random seeds):

| Rank | Author | Strategy | Mean Edge |
|------|--------|----------|-----------|
| **#1** | **@ryanli** | **AskSurf** | **$42.32** |
| #2 | @octavicristea | Predictor | $41.09 |
| #3 | @zhimao_liu | AskSurf | $40.81 |
| #4 | @onurakpolat | Binary | $24.87 |
| #5 | @ChinesePowered | chinese combine | $23.90 |

- **Starting Point**: -$15.83 (dead last)
- **Leaderboard Score** (one seed): $52.03
- **Final Score** (median of 3 seeds): $42.32
- **Strategies Explored**: 1,039 variants across 980+ parallel agent runs

## The Strategy

The winning strategy is [`strategy.py`](strategy.py) (~900 lines). See [`WRITEUP.md`](WRITEUP.md) for the full story of how it was built using parallel Claude Code agents.

## Setup

Requires the challenge repo and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
# Clone the challenge
git clone https://github.com/danrobinson/prediction-market-challenge
cd prediction-market-challenge
uv sync

# Copy the winning strategy in
cp /path/to/this/repo/strategy.py research/strategies/

# Run it
uv run orderbook-pm run research/strategies/strategy.py --simulations 200 --workers 4
```

## How It Works

See [`WRITEUP.md`](WRITEUP.md) for the detailed approach. The short version:

1. **Regime detection** — classify competitor spread (cs=2/3/4), skip cs=1 entirely
2. **Arb-risk decomposition** — size orders based on per-side Gaussian arb probability, not crude heuristics
3. **None-handling** — quote at tick 1/99 when the competitor book is empty (the single biggest edge discovery)
4. **Floor exploitation** — aggressive asks at prices <5% where retail sell quantity is capped
5. **Marginal stacking** — time-varying aggressiveness, inventory skew, jump suppression, high-prob throttling

Built with [Claude Code](https://claude.ai/code) using parallel agent swarms as an autoresearch engine.
