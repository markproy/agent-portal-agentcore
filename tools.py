"""Tools shared by the stock analysis agent: web search and stock price lookup.

Both use free, keyless APIs so the only credential you need to supply is
for the LLM/agent backend itself.

Parameter descriptions use a plain string in Annotated (`Annotated[str, "..."]`)
rather than `Annotated[str, Field(description="...")]` -- this file is shared
verbatim across clouds (Azure/Agent Framework, AWS/Strands, Google/ADK), and
Strands' `@tool` decorator doesn't support pydantic.Field inside Annotated
(NotImplementedError). The plain-string form works identically with both.

Google ADK is a step further: verified directly (`FunctionTool._get_declaration()`)
that it drops the Annotated metadata entirely, and doesn't restructure a
docstring's `Args:` section into individual parameter schema fields either --
each parameter's *schema* carries just name + type, no per-arg description
field. But per Google's own ADK codelab (build-agents-with-adk, "Empowering
with Tools"), the whole docstring -- Args: section included -- reaches the
model as the tool's one description string regardless, and a `Args:`-style
docstring is exactly what Google's own guidance recommends ("the single most
important factor for the agent to use your tool correctly"). So every
function below has one, on top of Annotated -- redundant for Azure/Strands,
which already get proper per-parameter descriptions from Annotated, but
Gemini needs it, and it's free to keep for all three.
"""

import time
from datetime import datetime, timedelta
from typing import Annotated

import yfinance as yf
from ddgs import DDGS


def _retry(fn, attempts=3, delay=0.5):
    """Retry a zero-arg callable a few times with a short delay. yfinance and ddgs
    are unofficial/scraping-style clients hitting endpoints without a sanctioned
    public API; they intermittently rate-limit or block requests -- especially
    from shared datacenter IPs (e.g. a cloud-hosted agent), more than from a
    residential dev machine. A short retry smooths over most of these blips."""
    last_exc = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(delay)
    raise last_exc


def search_web(
    query: Annotated[str, "The search query to look up on the web."],
) -> str:
    """Search the web for current information such as news, company events, or general facts.

    Args:
        query: The search query to look up on the web.
    """
    try:
        results = _retry(lambda: DDGS().text(query, max_results=5))
    except Exception:
        return f"Web search is temporarily unavailable (couldn't complete search for '{query}')."
    if not results:
        return "No results found."
    return "\n".join(f"- {r['title']}: {r['body']} ({r['href']})" for r in results)


def _format_market_cap(value: float | None) -> str:
    if value is None:
        return "N/A"
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if value >= threshold:
            return f"${value / threshold:.2f}{suffix}"
    return f"${value:,.0f}"


def get_stock_price(
    ticker: Annotated[str, "The stock ticker symbol, e.g. AAPL, MSFT, TSLA."],
) -> str:
    """Get the latest price and basic daily stats for a stock ticker.

    Args:
        ticker: The stock ticker symbol, e.g. AAPL, MSFT, TSLA.
    """
    try:
        ticker_obj = yf.Ticker(ticker)
        price = _retry(lambda: ticker_obj.fast_info.get("lastPrice"))
        info = ticker_obj.fast_info
    except Exception:
        price = None
    if price is None:
        return f"Could not find price data for ticker '{ticker}'."
    return (
        f"{ticker.upper()}: last price ${price:.2f}, "
        f"day range ${info.get('dayLow', 0):.2f}-${info.get('dayHigh', 0):.2f}, "
        f"previous close ${info.get('previousClose', 0):.2f}, "
        f"market cap {_format_market_cap(info.get('marketCap'))}"
    )


def get_price_history(
    ticker: Annotated[str, "The stock ticker symbol, e.g. AAPL, MSFT, TSLA."],
    days: Annotated[
        int, "How many calendar days back to look, e.g. 14 for 'last two weeks', 30 for 'last month'."
    ],
) -> str:
    """Get historical daily closing prices and performance for a ticker over a lookback
    window ending today, including each trading day's exact closing price.

    Use this for questions about trends or performance over time (e.g. 'how has X done
    over the last N days/weeks/months', 'which stock performed better', 'give me the
    daily closing prices'), rather than guessing from web search results.

    Args:
        ticker: The stock ticker symbol, e.g. AAPL, MSFT, TSLA.
        days: How many calendar days back to look, e.g. 14 for 'last two weeks', 30 for 'last month'.
    """
    try:
        end = datetime.now()
        start = end - timedelta(days=days)
        hist = _retry(lambda: yf.Ticker(ticker).history(start=start, end=end))
    except Exception:
        hist = None
    if hist is None or hist.empty:
        return f"Could not find historical price data for ticker '{ticker}'."
    start_price = float(hist["Close"].iloc[0])
    end_price = float(hist["Close"].iloc[-1])
    pct_change = (end_price - start_price) / start_price * 100
    high = float(hist["High"].max())
    low = float(hist["Low"].min())
    summary = (
        f"{ticker.upper()} over the last {days} days: "
        f"${start_price:.2f} -> ${end_price:.2f} ({pct_change:+.2f}%), "
        f"range ${low:.2f}-${high:.2f}"
    )

    max_rows = 30
    recent = hist["Close"].tail(max_rows)
    daily_lines = "\n".join(f"  {idx.strftime('%Y-%m-%d')}: ${close:.2f}" for idx, close in recent.items())
    truncated_note = "" if len(hist) <= max_rows else f" (most recent {max_rows} of {len(hist)} trading days)"
    return f"{summary}\nDaily closes{truncated_note}:\n{daily_lines}"
