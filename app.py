"""Personlig aksje- og fondsdashboard — første MVP."""
import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf
import feedparser
import os
import json
import html
import sqlite3
from pathlib import Path
try:
    from streamlit_autorefresh import st_autorefresh
    AUTO_REFRESH_AVAILABLE = True
except ModuleNotFoundError:
    AUTO_REFRESH_AVAILABLE = False
from datetime import datetime
import re
import logging
from urllib.parse import quote


RSS_FEEDS = {
    "E24": "https://e24.no/rss",
    "NewsWeb": "https://newsweb.oslobors.no/rss/",
}
POSITIVE = {"vekst", "avtale", "kontrakt", "opp", "løft", "overskudd", "bedre", "kjøp", "utbytte", "rekord"}
NEGATIVE = {"fall", "ned", "tap", "krise", "svak", "kutt", "salg", "advarsel", "skuffelse", "konkurs"}
SECTORS = {"EQNR.OL": "Energi", "DNB.OL": "Finans", "YAR.OL": "Materialer", "NHY.OL": "Materialer",
           "AAPL": "Teknologi", "MSFT": "Teknologi", "NVDA": "Teknologi", "TSLA": "Forbruker",
           "AMZN": "Forbruker", "GOOGL": "Kommunikasjon", "META": "Kommunikasjon"}
NEWS_ALIASES = {"DNB.OL": ["dnb", "dnb bank"], "EQNR.OL": ["equinor", "eqnr"], "YAR.OL": ["yara", "yar"],
                "NHY.OL": ["norsk hydro", "hydro", "nhy"], "AAPL": ["apple"], "MSFT": ["microsoft"],
                "NVDA": ["nvidia"], "TSLA": ["tesla"], "AMZN": ["amazon"], "GOOGL": ["google", "alphabet"],
                "META": ["meta", "facebook"]}
# Kan overstyres i Streamlit Secrets/miljøvariabler dersom databasen skal ligge
# på et persistent volum. Lokal SQLite-lagring på Streamlit Cloud er ellers
# midlertidig og kan forsvinne ved omstart/redeploy.
DB_PATH = os.getenv("NORDLYS_DB_PATH", "investeringer.db")
logging.basicConfig(filename="nordlys.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("nordlys")


def init_db() -> None:
    db_parent = Path(DB_PATH).expanduser().parent
    if str(db_parent) not in ("", "."):
        db_parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        con.execute("PRAGMA busy_timeout = 5000")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY, symbol TEXT, recorded_at TEXT, score INTEGER,
            signal TEXT, close REAL, return_1m REAL, return_3m REAL, volatility REAL,
            total_score REAL, recommendation TEXT
        );
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY, symbol TEXT, title TEXT, source TEXT, link TEXT,
            published TEXT, score INTEGER, explanation TEXT, recorded_at TEXT,
            UNIQUE(symbol, title)
        );
        """)
        columns = {row[1] for row in con.execute("PRAGMA table_info(analyses)")}
        if "total_score" not in columns:
            con.execute("ALTER TABLE analyses ADD COLUMN total_score REAL")
        if "recommendation" not in columns:
            con.execute("ALTER TABLE analyses ADD COLUMN recommendation TEXT")


def save_analysis(symbol: str, result: dict) -> None:
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        con.execute("INSERT INTO analyses(symbol,recorded_at,score,signal,close,return_1m,return_3m,volatility,total_score,recommendation) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (symbol, datetime.now().isoformat(timespec="seconds"), result["score"], result["signal"],
                     result["close"], result["return_1m"], result["return_3m"], result["volatility"],
                     result.get("total_score", result["score"]), result.get("recommendation", "")))


def save_news(symbol: str, frame: pd.DataFrame) -> None:
    if frame.empty:
        return
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        for _, item in frame.iterrows():
            con.execute("""
                INSERT INTO news(symbol,title,source,link,published,score,explanation,recorded_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, title) DO UPDATE SET
                    score=excluded.score,
                    explanation=excluded.explanation,
                    recorded_at=excluded.recorded_at
            """,
                        (symbol, item.Tittel, item.Kilde, item.Lenke, item.Dato, int(item.Score),
                         item.get("Forklaring", ""), datetime.now().isoformat(timespec="seconds")))


def saved_ai_news(symbol: str) -> dict:
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        rows = con.execute("SELECT title, score, explanation FROM news WHERE symbol = ? AND explanation != ''", (symbol,)).fetchall()
    return {title: (score, explanation) for title, score, explanation in rows}


init_db()


@st.cache_data(ttl=900, show_spinner=False)
def fetch_news(symbol: str, limit: int = 10) -> pd.DataFrame:
    query = quote(symbol.replace(".OL", ""))
    rows = []
    for source, url in RSS_FEEDS.items():
        feed_url = url if source == "NewsWeb" else f"https://news.google.com/rss/search?q={query}%20finance&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:limit]:
            title = re.sub(r"\s+", " ", entry.get("title", "").strip())
            link = entry.get("link", "")
            if not link.startswith(("http://", "https://")):
                link = ""
            aliases = NEWS_ALIASES.get(symbol, [symbol.replace(".OL", "").lower()])
            relevant = any(alias in title.lower() for alias in aliases)
            if not relevant and source != "NewsWeb":
                continue
            words = {w.strip(".,:;()[]\"'").lower() for w in title.split()}
            score = max(-5, min(5, len(words & POSITIVE) - len(words & NEGATIVE)))
            category = "Børsmelding" if source == "NewsWeb" else ("Resultat" if any(w in title.lower() for w in ["result", "quarter", "earnings"]) else "Marked")
            rows.append({"Kilde": source, "Tittel": title, "Score": score,
                         "Lenke": link, "Dato": entry.get("published", ""), "Type": category,
                         "Relevans": "Høy" if relevant else "Usikker"})
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["Nøkkel"] = frame["Tittel"].str.lower().str.replace(r"[^a-z0-9æøå ]", "", regex=True).str[:120]
    frame = frame.drop_duplicates(subset=["Nøkkel"]).drop(columns=["Nøkkel"])
    parsed = pd.to_datetime(frame["Dato"], errors="coerce", utc=True)
    now = pd.Timestamp.now(tz="UTC")
    age = ((now - parsed).dt.total_seconds() / 86400).fillna(30).clip(lower=0)
    frame["Alder_dager"] = age.round(1)
    frame["VektetScore"] = frame["Score"] * (0.25 + 0.75 * np.exp(-age / 45))
    return frame.sort_values(["Relevans", "Alder_dager"], ascending=[True, True]).head(limit)


def ai_sentiment(title: str) -> tuple[int, str]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return 0, "AI ikke konfigurert — lokal reserveanalyse brukes."
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        response = client.responses.create(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), input=(
            "Analyser finansnyheten på norsk. Returner kun JSON med score (-5 til 5) og "
            "forklaring (maks 18 ord). Nyhet: " + title))
        raw = response.output_text.strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if not match:
                raise
            data = json.loads(match.group(0))
        score = int(data.get("score", 0))
        explanation = str(data.get("forklaring", "AI-analyse fullført."))
        logger.info("AI-sentiment fullført")
        return max(-5, min(5, score)), explanation
    except Exception as exc:
        logger.error("AI-sentiment feilet: %s", type(exc).__name__)
        return 0, f"AI utilgjengelig: {type(exc).__name__}"


@st.cache_data(ttl=900, show_spinner=False)
def load_prices(symbol: str, period: str = "2y") -> pd.DataFrame:
    prices = yf.download(symbol, period=period, interval="1d", auto_adjust=True, progress=False)
    if prices.empty:
        return prices
    if isinstance(prices.columns, pd.MultiIndex):
        prices.columns = prices.columns.get_level_values(0)
    prices.columns = [str(c).title() for c in prices.columns]
    index = pd.to_datetime(prices.index)
    prices.index = index.tz_localize(None) if index.tz is not None else index
    return prices.dropna(subset=["Close"]).sort_index()


def database_bytes() -> bytes:
    path = Path(DB_PATH)
    return path.read_bytes() if path.exists() else b""


def analyse(prices: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    df = prices.copy()
    df["SMA20"] = df["Close"].rolling(20).mean()
    df["SMA50"] = df["Close"].rolling(50).mean()
    df["SMA200"] = df["Close"].rolling(200).mean()
    df["Return_1m"] = df["Close"].pct_change(21) * 100
    df["Return_3m"] = df["Close"].pct_change(63) * 100
    df["Volatility"] = df["Close"].pct_change().rolling(21).std() * np.sqrt(252) * 100
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    df["RSI"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["BB_mid"] = df["Close"].rolling(20).mean()
    bb_std = df["Close"].rolling(20).std()
    df["BB_upper"] = df["BB_mid"] + 2 * bb_std
    df["BB_lower"] = df["BB_mid"] - 2 * bb_std
    if "Volume" in df:
        df["Volume_ratio"] = df["Volume"] / df["Volume"].rolling(20).mean()
    else:
        df["Volume_ratio"] = 1.0
    true_range = pd.concat([df["High"] - df["Low"], (df["High"] - df["Close"].shift()).abs(), (df["Low"] - df["Close"].shift()).abs()], axis=1).max(axis=1)
    df["ATR"] = true_range.rolling(14).mean()
    df["Support"] = df["Low"].rolling(20).min()
    df["Resistance"] = df["High"].rolling(20).max()
    df["Trend_strength"] = (df["SMA20"] - df["SMA50"]).abs() / df["ATR"].replace(0, np.nan)
    usable = df.dropna()
    if usable.empty:
        raise ValueError("For lite eller ufullstendig kursdata til å beregne indikatorene.")
    last = usable.iloc[-1]
    score = sum([2 if last.Close > last.SMA50 else -2, 2 if last.SMA20 > last.SMA50 else -1,
                 2 if last.SMA50 > last.SMA200 else -1, 2 if last.Return_1m > 0 else -1,
                 2 if last.Return_3m > 0 else -1, 1 if 50 <= last.RSI <= 70 else -1,
                 1 if last.MACD > last.MACD_signal else -1, 1 if last.Volume_ratio > 1 else 0,
                 1 if last.Trend_strength >= 1 else 0])
    signal = "Sterk" if score >= 7 else "Positiv" if score >= 4 else "Nøytral" if score >= 1 else "Svak"
    return df, {"score": score, "signal": signal, "close": last.Close, "return_1m": last.Return_1m,
                "return_3m": last.Return_3m, "volatility": last.Volatility, "rsi": last.RSI,
                "macd_positive": last.MACD > last.MACD_signal, "volume_ratio": last.Volume_ratio, "atr": last.ATR,
                "support": last.Support, "resistance": last.Resistance, "trend_strength": last.Trend_strength,
                "updated": df.index[-1].date()}


def backtest(df: pd.DataFrame, fee_pct: float = 0.15, benchmark: pd.DataFrame | None = None, profile: str = "Balansert") -> dict:
    test = df.dropna(subset=["SMA50"]).copy()
    test = test.dropna(subset=["RSI", "MACD", "MACD_signal"])
    if test.empty:
        raise ValueError("For lite historikk til å kjøre backtest.")
    if profile == "Konservativ":
        rsi_low, rsi_high, require_macd, trend_column = 50, 65, True, "SMA200"
    elif profile == "Offensiv":
        rsi_low, rsi_high, require_macd, trend_column = 30, 80, False, "SMA20"
    else:
        rsi_low, rsi_high, require_macd, trend_column = 40, 75, True, "SMA50"
    macd_rule = test["MACD"] > test["MACD_signal"] if require_macd else pd.Series(True, index=test.index)
    trend_rule = test["Close"] > test[trend_column]
    test["position"] = (trend_rule & test["RSI"].between(rsi_low, rsi_high) & macd_rule).astype(int)
    test["market_return"] = test["Close"].pct_change().fillna(0)
    test["strategy_return"] = test["position"].shift(1).fillna(0) * test["market_return"]
    trades = test["position"].diff().abs().fillna(0)
    test["strategy_return"] -= trades * (fee_pct / 100)
    equity = (1 + test["strategy_return"]).cumprod()
    drawdown = equity / equity.cummax() - 1
    trade_returns = test.loc[test["position"].diff() == -1, "strategy_return"]
    daily_vol = test["strategy_return"].std()
    annualized = equity.iloc[-1] ** (252 / max(len(test), 1)) - 1
    benchmark_return = (1 + test["market_return"]).prod() * 100 - 100
    if benchmark is not None and not benchmark.empty:
        # Sammenlign kun samme datoer som strategien, ellers blir benchmarken
        # feil dersom den har en annen historikkperiode enn aksjen.
        benchmark_series = benchmark["Close"].pct_change().reindex(test.index).fillna(0)
        benchmark_return = (1 + benchmark_series).prod() * 100 - 100
    strategy_return_pct = (equity.iloc[-1] - 1) * 100
    return {"strategy": strategy_return_pct, "market": benchmark_return,
            "excess": strategy_return_pct - benchmark_return,
            "drawdown": drawdown.min() * 100, "days": len(test), "equity": equity, "strategy_series": test["strategy_return"],
            "trades": int(trades.sum()), "exposure": test["position"].mean() * 100,
            "win_rate": (trade_returns > 0).mean() * 100 if len(trade_returns) else 0,
            "annualized": annualized * 100, "sharpe": (test["strategy_return"].mean() / daily_vol * np.sqrt(252)) if daily_vol else 0}


def correlation_data(symbols: list[str], period: str = "1y") -> pd.DataFrame:
    series = {}
    for symbol in symbols:
        prices = load_prices(symbol, period)
        if not prices.empty:
            series[symbol] = prices["Close"].pct_change()
    return pd.DataFrame(series).corr().round(2) if series else pd.DataFrame()


def walk_forward(df: pd.DataFrame, fee_pct: float, profile: str, test_days: int = 63) -> pd.DataFrame:
    rows = []
    start = 252
    while start + test_days <= len(df):
        window = df.iloc[max(0, start - 252):start + test_days]
        result = backtest(window, fee_pct=fee_pct, profile=profile)
        oos = result["strategy_series"].iloc[-test_days:]
        oos_equity = (1 + oos).cumprod()
        oos_drawdown = (oos_equity / oos_equity.cummax() - 1).min() * 100
        rows.append({"Testperiode": f"{df.index[start].date()} – {df.index[start + test_days - 1].date()}",
                     "Out-of-sample %": (oos_equity.iloc[-1] - 1) * 100, "Største fall %": oos_drawdown,
                     "Treffprosent %": (oos > 0).mean() * 100, "Handler": int((oos != 0).sum())})
        start += test_days
    return pd.DataFrame(rows)


def validate_model(symbols: list[str], periods: list[str], fee_pct: float, profile: str) -> pd.DataFrame:
    rows = []
    for symbol in symbols:
        for period_name in periods:
            prices = load_prices(symbol, period_name)
            if prices.empty:
                continue
            result = backtest(prices, fee_pct=fee_pct, profile=profile)
            rows.append({"Symbol": symbol, "Periode": period_name, "Strategi %": result["strategy"],
                         "Kjøp-og-hold %": result["market"], "Største fall %": result["drawdown"],
                         "Treffprosent %": result["win_rate"], "Handler": result["trades"], "Sharpe": result["sharpe"]})
    return pd.DataFrame(rows)


st.set_page_config(page_title="Nordlys Invest", page_icon="✦", layout="wide")
st.markdown("""
<style>
.stApp {background:radial-gradient(ellipse at 8% 16%,#203b5c 0,transparent 28%),radial-gradient(ellipse at 26% 78%,#182d4b 0,transparent 24%),radial-gradient(ellipse at 55% 108%,#154b4b 0,transparent 32%),radial-gradient(ellipse at 87% 12%,#45264f 0,transparent 26%),radial-gradient(ellipse at 70% 48%,#291d3c 0,transparent 22%),linear-gradient(108deg,transparent 22%,rgba(36,210,155,.18) 34%,rgba(83,255,194,.42) 42%,rgba(52,220,169,.25) 48%,rgba(137,93,237,.26) 57%,transparent 70%),linear-gradient(120deg,transparent 38%,rgba(63,239,178,.24) 47%,rgba(110,255,207,.34) 54%,transparent 66%),#0d1420; position:relative; overflow:hidden;}
.stApp:before {content:''; position:fixed; inset:0; pointer-events:none; z-index:0; opacity:.22; background-image:linear-gradient(rgba(120,240,189,.065) 1px,transparent 1px),linear-gradient(90deg,rgba(120,240,189,.065) 1px,transparent 1px); background-size:42px 42px; mask-image:linear-gradient(to bottom,black,transparent 82%);}
.stApp:after {content:''; position:fixed; width:1100px; height:620px; left:22%; top:0; pointer-events:none; z-index:0; background:radial-gradient(ellipse at 20% 48%,rgba(47,255,173,.42),transparent 35%),radial-gradient(ellipse at 42% 38%,rgba(91,255,205,.38),transparent 34%),radial-gradient(ellipse at 64% 44%,rgba(93,232,187,.34),transparent 33%),radial-gradient(ellipse at 82% 36%,rgba(153,105,255,.34),transparent 32%); filter:blur(30px); border-radius:48% 52% 65% 35%; transform:rotate(-15deg) skewX(-12deg); opacity:1;}
.stApp > div {position:relative; z-index:10;}
.stApp [data-testid="stHeader"], .stApp [data-testid="stSidebar"] {z-index:2;}
html {scrollbar-width:auto; scrollbar-color:#4de0ad #162235;}
html::-webkit-scrollbar, body::-webkit-scrollbar {width:12px;}
html::-webkit-scrollbar-track, body::-webkit-scrollbar-track {background:#162235;}
html::-webkit-scrollbar-thumb, body::-webkit-scrollbar-thumb {background:#4de0ad; border-radius:8px; border:2px solid #162235;}
.stAppViewContainer, [data-testid="stAppViewContainer"], [data-testid="stAppViewContainer"] > .main, [data-testid="stMain"] {scrollbar-width:auto; scrollbar-color:#4de0ad #162235;}
[data-testid="stAppViewContainer"]::-webkit-scrollbar, [data-testid="stAppViewContainer"] > .main::-webkit-scrollbar, [data-testid="stMain"]::-webkit-scrollbar {width:12px; display:block;}
[data-testid="stAppViewContainer"]::-webkit-scrollbar-track, [data-testid="stAppViewContainer"] > .main::-webkit-scrollbar-track, [data-testid="stMain"]::-webkit-scrollbar-track {background:#162235;}
[data-testid="stAppViewContainer"]::-webkit-scrollbar-thumb, [data-testid="stAppViewContainer"] > .main::-webkit-scrollbar-thumb, [data-testid="stMain"]::-webkit-scrollbar-thumb {background:#4de0ad; border-radius:8px; border:2px solid #162235;}
.block-container {padding-top:2.8rem; max-width:1500px;}
h1 {letter-spacing:-.06em; font-weight:800; background:linear-gradient(90deg,#e7fff6,#78f0bd 45%,#a78bfa); -webkit-background-clip:text; -webkit-text-fill-color:transparent;}
h2, h3 {letter-spacing:-.035em;}
[data-testid="stMetric"] {background:rgba(20,29,43,.78); border:1px solid rgba(139,161,190,.18); padding:16px; border-radius:16px; box-shadow:0 10px 32px rgba(0,0,0,.18);}
[data-testid="stMetricLabel"] {color:#91a4bb;}
[data-testid="stMetricValue"] {color:#f2f7ff;}
.signal {padding:22px 26px; border-radius:20px; background:linear-gradient(120deg,rgba(23,65,59,.92),rgba(25,27,56,.92)); border:1px solid rgba(102,240,190,.38); box-shadow:0 14px 40px rgba(0,0,0,.25); margin:10px 0 22px; position:relative; overflow:hidden;}
.signal:after {content:''; position:absolute; width:180px; height:180px; right:-40px; top:-80px; background:#8b5cf6; opacity:.16; filter:blur(26px); border-radius:50%;}
.signal:before {content:'◆'; position:absolute; right:24px; bottom:10px; color:rgba(114,242,194,.18); font-size:5rem; transform:rotate(18deg);}
.signal h2 {margin:4px 0; color:#72f2c2; font-size:2rem;}
.muted {color:#93a4b8; font-size:.9rem;}
div[data-baseweb="tab-list"] {gap:8px;}
button[data-baseweb="tab"] {border-radius:10px 10px 0 0; padding:10px 18px;}
div[data-testid="stDataFrame"] {border-radius:16px; overflow:hidden; border:1px solid rgba(139,161,190,.16);}
section[data-testid="stSidebar"] {background:linear-gradient(180deg,#101827,#0b1018); border-right:1px solid #202d40;}
section[data-testid="stSidebar"] > div:first-child {width:100%; overflow-y:scroll !important; overflow-x:hidden; scrollbar-gutter:stable;}
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {height:100vh; overflow-y:scroll !important; overflow-x:hidden; scrollbar-width:auto; scrollbar-color:#4de0ad #162235; padding-right:8px;}
section[data-testid="stSidebar"] > div:first-child::-webkit-scrollbar, section[data-testid="stSidebar"] [data-testid="stSidebarContent"]::-webkit-scrollbar {width:12px; display:block;}
section[data-testid="stSidebar"] > div:first-child::-webkit-scrollbar-track, section[data-testid="stSidebar"] [data-testid="stSidebarContent"]::-webkit-scrollbar-track {background:#162235;}
section[data-testid="stSidebar"] > div:first-child::-webkit-scrollbar-thumb, section[data-testid="stSidebar"] [data-testid="stSidebarContent"]::-webkit-scrollbar-thumb {background:#4de0ad; border-radius:8px; border:2px solid #162235;}
section[data-testid="stSidebar"] [data-testid="stSidebarContent"]::-webkit-scrollbar {width:8px;}
section[data-testid="stSidebar"] [data-testid="stSidebarContent"]::-webkit-scrollbar-track {background:rgba(18,28,42,.35);}
section[data-testid="stSidebar"] [data-testid="stSidebarContent"]::-webkit-scrollbar-thumb {background:linear-gradient(#4de0ad,#7657d9); border-radius:8px;}
.stButton>button {border:1px solid #4de0ad; background:linear-gradient(100deg,#1d8067,#5b3db1); color:white; border-radius:11px; font-weight:700; box-shadow:0 0 22px rgba(77,224,173,.15); transition:.2s ease;}
.stButton>button:hover {border-color:#a7f3d0; transform:translateY(-1px); box-shadow:0 0 30px rgba(77,224,173,.3);}
[data-testid="stExpander"] {background:rgba(16,24,37,.64); border:1px solid rgba(139,161,190,.16); border-radius:14px;}
@media (max-width:1100px) {
  .block-container {padding:1.5rem 1.2rem 2rem; max-width:100%;}
  h1 {font-size:2.15rem;}
  [data-testid="stHorizontalBlock"] {gap:.65rem; flex-wrap:wrap;}
  [data-testid="stHorizontalBlock"] > div {min-width:180px; flex:1 1 180px;}
  [data-testid="stDataFrame"] {font-size:.85rem;}
  section[data-testid="stSidebar"] {min-width:280px; max-width:320px;}
}
@media (max-width:700px) {
  .block-container {padding:.9rem .7rem 1.5rem;}
  h1 {font-size:1.75rem;}
  h2 {font-size:1.35rem;}
  [data-testid="stHorizontalBlock"] > div {min-width:100%;}
  [data-testid="stMetric"] {padding:11px;}
  .signal {padding:16px;}
  .signal h2 {font-size:1.45rem;}
  section[data-testid="stSidebar"] {min-width:85vw; max-width:90vw;}
}
</style>
""", unsafe_allow_html=True)
st.title("Nordlys Invest")
st.caption("MARKEDSOVERSIKT  /  MOMENTUM  /  NYHETER  /  RISIKO")
st.caption(f"Sist oppdatert: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
st.info("Dette er et analyseverktøy, ikke personlig investeringsrådgivning. Historiske resultater og modellsignaler garanterer ikke fremtidig avkastning.")
with st.sidebar:
    st.header("Analyse")
    raw = st.text_area("Watchlist (Yahoo-symboler)", "EQNR.OL\nDNB.OL\nAAPL\nMSFT")
    period = st.selectbox("Historikk", ["1y", "2y", "5y"], index=1)
    symbols = [x.strip().upper() for x in raw.splitlines() if x.strip()]
    run = st.button("Oppdater analyse", type="primary")
    st.markdown("#### Beholdninger")
    holdings_raw = st.text_area("Symbol, antall, kjøpspris, stop-loss %", "EQNR.OL,10,250,8\nDNB.OL,5,190,8", help="Én beholdning per linje. Stop-loss oppgis som prosent under kjøpspris.")
    capital = st.number_input("Investeringskapital", min_value=0.0, value=10000.0, step=1000.0)
    max_position_pct = st.slider("Maks én posisjon (%)", 5, 50, 25)
    risk_per_trade_pct = st.slider("Maks risiko per handel (%)", 0.5, 5.0, 2.0, step=0.5)
    fee_pct = st.number_input("Kostnad per kjøp/salg (%)", min_value=0.0, max_value=2.0, value=0.15, step=0.05)
    benchmark_symbol = st.text_input("Benchmark", "^OSEBX", help="Yahoo-symbol for sammenligning, f.eks. ^OSEBX for Oslo Børs eller SPY for USA")
    strategy_profile = st.selectbox("Strategiprofil", ["Konservativ", "Balansert", "Offensiv"], index=1)
    technical_weight = st.slider("Teknisk vekt (%)", 0, 100, 70)
    news_weight = 100 - technical_weight
    st.caption(f"Nyhetsvekt: {news_weight}%")
    min_confidence = st.selectbox("Minimum konfidens i rangering", ["Alle", "Middels", "Høy"], index=0)
    auto_update = st.checkbox("Automatisk oppdatering", value=False)
    update_minutes = st.selectbox("Oppdateringsintervall", [5, 15, 30, 60], index=1)
    if auto_update:
        if AUTO_REFRESH_AVAILABLE:
            st_autorefresh(interval=update_minutes * 60 * 1000, key="market_refresh")
        else:
            st.warning("Installer streamlit-autorefresh for å aktivere automatisk oppdatering.")
    st.download_button("Last ned databasebackup", database_bytes(), "investeringer-backup.db", "application/octet-stream")
    analyze_all = st.button("Analyser hele watchlisten med AI", disabled=not bool(os.getenv("OPENAI_API_KEY")))
    st.caption("AI-status: aktiv" if os.getenv("OPENAI_API_KEY") else "AI-status: ikke konfigurert")
    run_validation = st.button("Kjør modellvalidering")

if run or "results" not in st.session_state:
    results, errors = [], []
    for symbol in symbols:
        try:
            prices = load_prices(symbol, period)
            if prices.empty:
                errors.append(f"Ingen data: {symbol}")
                continue
            _, result = analyse(prices)
            result["symbol"] = symbol
            quick_news = fetch_news(symbol, limit=8)
            cached_ai = saved_ai_news(symbol)
            if cached_ai and not quick_news.empty:
                cached_scores = [cached_ai[item] [0] for item in quick_news["Tittel"] if item in cached_ai]
                result["news_sentiment"] = sum(cached_scores) / len(cached_scores) if cached_scores else quick_news["VektetScore"].mean()
            else:
                result["news_sentiment"] = quick_news["VektetScore"].mean() if not quick_news.empty else 0.0
            result["news_count"] = len(quick_news)
            result["total_score"] = round((technical_weight / 70) * result["score"] + (news_weight / 30) * result["news_sentiment"])
            technical_direction = 1 if result["score"] > 0 else -1 if result["score"] < 0 else 0
            news_direction = 1 if result["news_sentiment"] > 0 else -1 if result["news_sentiment"] < 0 else 0
            result["confidence"] = "Høy" if technical_direction == news_direction and technical_direction != 0 else "Middels" if technical_direction == 0 or news_direction == 0 else "Lav"
            result["recommendation"] = ("POSITIVT SIGNAL" if result["total_score"] >= 7 else "FØLG MED" if result["total_score"] >= 3 else "NØYTRALT SIGNAL" if result["total_score"] >= 0 else "NEGATIVT SIGNAL")
            save_analysis(symbol, result)
            results.append(result)
        except Exception as exc:
            errors.append(f"{symbol}: {exc}")
    st.session_state.results, st.session_state.errors = results, errors

for error in st.session_state.get("errors", []):
    st.warning(error)
results = st.session_state.get("results", [])
if not results:
    st.info("Legg inn minst ett Yahoo-symbol og trykk «Oppdater analyse».")
    st.stop()

if analyze_all:
    with st.spinner("Analyserer nye nyheter for hele watchlisten …"):
        for symbol in symbols:
            news_batch = fetch_news(symbol, limit=8)
            cached = saved_ai_news(symbol)
            if news_batch.empty:
                continue
            ai_rows = []
            for _, item in news_batch.iterrows():
                result = cached.get(item["Tittel"]) or ai_sentiment(item["Tittel"])
                ai_rows.append({**item.to_dict(), "Score": result[0], "Forklaring": result[1]})
            save_news(symbol, pd.DataFrame(ai_rows))
    st.success("Nye AI-resultater er lagret. Trykk Oppdater analyse for å oppdatere rangeringen.")
if not results:
    st.error("Ingen analyser kunne hentes akkurat nå. Kontroller internettilkobling og Yahoo-symbolene.")
    st.stop()
table = pd.DataFrame(results)
confidence_order = {"Lav": 0, "Middels": 1, "Høy": 2}
if min_confidence != "Alle":
    table = table[table["confidence"].map(confidence_order) >= confidence_order[min_confidence]]
if table.empty:
    st.warning("Ingen aksjer oppfyller valgt minimumskonfidens.")
    st.stop()
table = table.sort_values("total_score", ascending=False)
st.subheader("Rangering")
st.dataframe(table[["symbol", "recommendation", "confidence", "signal", "score", "news_sentiment", "news_count", "total_score", "close", "return_1m", "return_3m", "volatility", "updated"]], use_container_width=True, hide_index=True)
strong = table[table["recommendation"] == "POSITIVT SIGNAL"]["symbol"].tolist()
weak_news = table[table["news_sentiment"] < 0]["symbol"].tolist()
if strong:
    st.success("Sterke kandidater: " + ", ".join(strong))
if weak_news:
    st.warning("Negativ nyhetsbalanse: " + ", ".join(weak_news))
csv_data = table.to_csv(index=False).encode("utf-8-sig")
st.download_button("Last ned analyse som CSV", csv_data, "investeringsanalyse.csv", "text/csv")
selected = st.sidebar.selectbox("Velg aksje for detaljer", table["symbol"].tolist())
st.caption(f"Valgt aksje: {selected} · Se detaljer og nyheter nedenfor")
df, detail = analyse(load_prices(selected, period))
st.subheader(selected)
selected_row = table[table["symbol"] == selected].iloc[0]
st.markdown(f"<div class='signal'><div class='muted'>SAMLET MODELLSIGNAL</div><h2>{selected_row['recommendation']} · {selected_row['total_score']} poeng</h2><div class='muted'>Konfidens: {selected_row['confidence']} · Teknisk: {detail['score']} · Nyhet: {selected_row['news_sentiment']:+.1f} · {int(selected_row['news_count'])} nyheter</div></div>", unsafe_allow_html=True)
c1, c2, c3, c4 = st.columns(4)
c1.metric("Signal", detail["signal"], f"{detail['score']} poeng")
c2.metric("Siste kurs", f"{detail['close']:.2f}")
c3.metric("1 måned", f"{detail['return_1m']:.1f}%")
c4.metric("3 måneder", f"{detail['return_3m']:.1f}%")
st.line_chart(df[["Close", "SMA20", "SMA50", "SMA200"]].dropna())
with st.expander("Vis Bollinger-bånd"):
    st.line_chart(df[["Close", "BB_upper", "BB_mid", "BB_lower"]].dropna())
st.caption(f"Sist tilgjengelige kursdag: {detail['updated']}.")

tab_overview, tab_news, tab_portfolio = st.tabs(["Oversikt", "Nyheter", "Portefølje"])
with tab_overview:
    st.markdown("#### Trendindikatorer")
    a, b, c, d = st.columns(4)
    a.metric("Over 50-dagers snitt", "Ja" if detail["close"] > df["SMA50"].iloc[-1] else "Nei")
    b.metric("Årlig volatilitet", f"{detail['volatility']:.1f}%")
    c.metric("Datagrunnlag", f"{len(df)} handelsdager")
    d.metric("RSI (14)", f"{detail['rsi']:.1f}")
    e, f, g = st.columns(3)
    e.metric("Trendstyrke", f"{detail['trend_strength']:.1f} ATR")
    f.metric("Volumstyrke", f"{detail['volume_ratio']:.2f}x")
    g.metric("ATR", f"{detail['atr']:.2f}")
    st.caption(f"Støtte: {detail['support']:.2f} · Motstand: {detail['resistance']:.2f}")
    st.markdown("#### Tekniske indikatorer")
    st.line_chart(df[["MACD", "MACD_signal"]].dropna())
    with sqlite3.connect(DB_PATH) as con:
        history = pd.read_sql_query("SELECT recorded_at AS Tidspunkt, COALESCE(total_score, score) AS Poeng, recommendation AS Anbefaling, close AS Kurs FROM analyses WHERE symbol = ? ORDER BY recorded_at DESC LIMIT 20", con, params=(selected,))
    if not history.empty:
        st.markdown("#### Lokal signalhistorikk")
        st.dataframe(history, use_container_width=True, hide_index=True)
        history_chart = history.sort_values("Tidspunkt").set_index("Tidspunkt")[["Poeng"]]
        st.line_chart(history_chart)
    st.markdown("#### Historisk simulering")
    benchmark_prices = load_prices(benchmark_symbol, period) if benchmark_symbol else None
    bt = backtest(df, fee_pct=fee_pct, benchmark=benchmark_prices, profile=strategy_profile)
    x, y, z, q = st.columns(4)
    x.metric("Strategiavkastning", f"{bt['strategy']:+.1f}%")
    y.metric(f"Benchmark ({benchmark_symbol})", f"{bt['market']:+.1f}%")
    z.metric("Største fall", f"{bt['drawdown']:.1f}%")
    q.metric("Treffprosent", f"{bt['win_rate']:.1f}%")
    x2, y2, z2 = st.columns(3)
    x2.metric("Antall posisjonsendringer", str(bt["trades"]))
    y2.metric("Annualisert", f"{bt['annualized']:+.1f}%")
    z2.metric("Sharpe-lignende", f"{bt['sharpe']:.2f}")
    st.caption(f"Strategien var investert {bt['exposure']:.1f}% av handelsdagene. Benchmark: {benchmark_symbol}.")
    if bt["excess"] < 0:
        st.warning(f"Strategien ligger {abs(bt['excess']):.1f} prosentpoeng under benchmark i denne historiske perioden.")
    else:
        st.success(f"Strategien ligger {bt['excess']:.1f} prosentpoeng over benchmark i denne historiske perioden.")
    st.line_chart(bt["equity"].rename("Simulert verdi (start = 1,00)"))
    st.caption(f"Profil: {strategy_profile}. Backtesten bruker {fee_pct:.2f}% kostnad per posisjonsendring. Skatt og ekstra slippage er ikke medregnet.")
    st.markdown("#### Sammenligning av strategiprofiler")
    profile_rows = []
    for profile_name in ["Konservativ", "Balansert", "Offensiv"]:
        profile_result = backtest(df, fee_pct=fee_pct, benchmark=benchmark_prices, profile=profile_name)
        profile_rows.append({"Profil": profile_name, "Strategi %": profile_result["strategy"],
                             "Benchmark %": profile_result["market"], "Største fall %": profile_result["drawdown"],
                             "Treffprosent %": profile_result["win_rate"], "Handler": profile_result["trades"],
                             "Sharpe": profile_result["sharpe"]})
    profile_table = pd.DataFrame(profile_rows)
    st.dataframe(profile_table.style.format({"Strategi %":"{:+.1f}", "Benchmark %":"{:+.1f}", "Største fall %":"{:.1f}", "Treffprosent %":"{:.1f}", "Sharpe":"{:.2f}"}), use_container_width=True, hide_index=True)
    st.markdown("#### Følsomhet for handelskostnad")
    fee_rows = []
    for fee_test in [0.0, 0.15, 0.30, 0.50]:
        fee_result = backtest(df, fee_pct=fee_test, benchmark=benchmark_prices, profile=strategy_profile)
        fee_rows.append({"Kostnad per endring %": fee_test, "Strategi %": fee_result["strategy"], "Handler": fee_result["trades"]})
    fee_table = pd.DataFrame(fee_rows)
    st.dataframe(fee_table.style.format({"Kostnad per endring %":"{:.2f}", "Strategi %":"{:+.1f}"}), use_container_width=True, hide_index=True)
    st.markdown("#### Walk-forward-test")
    wf = walk_forward(df, fee_pct, strategy_profile)
    if wf.empty:
        st.info("Det trengs minst omtrent ett års historikk for walk-forward-test.")
    else:
        st.dataframe(wf.style.format({"Out-of-sample %":"{:+.1f}", "Største fall %":"{:.1f}", "Treffprosent %":"{:.1f}"}), use_container_width=True, hide_index=True)
    if run_validation:
        with st.spinner("Tester watchlisten på flere perioder …"):
            validation = validate_model(symbols, ["1y", "2y", "5y"], fee_pct, strategy_profile)
        if validation.empty:
            st.warning("Ingen valideringsdata kunne hentes.")
        else:
            st.markdown("#### Modellvalidering")
            st.dataframe(validation.style.format({"Strategi %":"{:+.1f}", "Kjøp-og-hold %":"{:+.1f}", "Største fall %":"{:.1f}", "Treffprosent %":"{:.1f}", "Sharpe":"{:.2f}"}), use_container_width=True, hide_index=True)
with tab_news:
    st.markdown("#### Nyhetsstrøm")
    news = fetch_news(selected)
    if news.empty:
        st.info("Fant ingen nyheter for dette symbolet.")
    else:
        use_ai = st.toggle("Bruk AI-sentiment", value=bool(os.getenv("OPENAI_API_KEY")))
        force_ai = st.button("Analyser nyhetene på nytt")
        if use_ai and os.getenv("OPENAI_API_KEY"):
            with st.spinner("Analyserer nyheter …"):
                cached = {} if force_ai else saved_ai_news(selected)
                ai_results = []
                for title in news["Tittel"]:
                    if title in cached:
                        ai_results.append(cached[title])
                    else:
                        ai_results.append(ai_sentiment(title))
            news["Score"] = [result[0] for result in ai_results]
            news["Forklaring"] = [result[1] for result in ai_results]
        elif use_ai:
            st.info("Sett miljøvariabelen OPENAI_API_KEY for å aktivere AI-sentiment.")
        save_news(selected, news)
        st.metric("Gjennomsnittlig nyhetsscore", f"{news['Score'].mean():+.1f} / 5")
        mode_text = "AI-basert sentiment er aktivert." if use_ai and os.getenv("OPENAI_API_KEY") else "Score er foreløpig regelbasert."
        st.caption(f"Nyeste nyheter prioriteres i visningen. {mode_text}")
        for _, item in news.iterrows():
            label = "Positiv" if item.Score > 0 else "Negativ" if item.Score < 0 else "Nøytral"
            explanation = f"  \n_{html.escape(str(item.Forklaring))}_" if "Forklaring" in item else ""
            safe_title = html.escape(str(item.Tittel))
            safe_type = html.escape(str(item.Type))
            safe_relevance = html.escape(str(item.Relevans))
            safe_source = html.escape(str(item.Kilde))
            safe_date = html.escape(str(item.Dato))
            safe_link = html.escape(str(item.Lenke), quote=True)
            link_part = f"[{safe_title}]({safe_link})" if safe_link else safe_title
            st.markdown(f"**{label} ({int(item.Score):+d}) · {safe_type} · Relevans: {safe_relevance}** {link_part}  \n<span class='muted'>{safe_source} · {safe_date} · {item.Alder_dager:.0f} dager gammel</span>{explanation}", unsafe_allow_html=True)
        with sqlite3.connect(DB_PATH) as con:
            news_history = pd.read_sql_query("SELECT published AS Dato, AVG(score) AS Score FROM news WHERE symbol = ? GROUP BY published ORDER BY published", con, params=(selected,))
        if len(news_history) > 1:
            st.markdown("#### Nyhetsscore over tid")
            st.line_chart(news_history.set_index("Dato")["Score"])
with tab_portfolio:
    st.markdown("#### Portefølje")
    portfolio = []
    for line in holdings_raw.splitlines():
        try:
            parts = [part.strip() for part in line.split(",")]
            symbol, shares, buy_price = parts[:3]
            stop_pct = float(parts[3]) if len(parts) > 3 else 8.0
            match = next((item for item in results if item["symbol"] == symbol.upper()), None)
            if match:
                current = float(match["close"]); shares = float(shares); buy_price = float(buy_price)
                cost = shares * buy_price; value = shares * current; stop_price = buy_price * (1 - stop_pct / 100)
                risk_status = "UTLØST" if current <= stop_price else "NÆR" if current <= stop_price * 1.05 else "OK"
                portfolio.append({"Symbol": symbol.upper(), "Antall": shares, "Kjøpspris": buy_price,
                                  "Siste": current, "Verdi": value, "Gevinst": value - cost,
                                  "Avkastning %": (value / cost - 1) * 100, "Stop-loss": stop_price,
                                  "Risiko": risk_status, "Sektor": SECTORS.get(symbol.upper(), "Ukjent"),
                                  "Volatilitet %": match["volatility"]})
        except ValueError:
            continue
        except Exception:
            st.warning(f"Kunne ikke tolke beholdningen: {line}")
    if portfolio:
        pf = pd.DataFrame(portfolio)
        max_position_value = capital * max_position_pct / 100
        pf["Andel %"] = pf["Verdi"] / capital * 100 if capital else 0
        pf["Foreslått maks"] = max_position_value
        p1, p2, p3 = st.columns(3)
        p1.metric("Porteføljeverdi", f"{pf['Verdi'].sum():,.0f}")
        p2.metric("Samlet gevinst/tap", f"{pf['Gevinst'].sum():+,.0f}")
        invested_value = (pf["Verdi"] - pf["Gevinst"]).sum()
        portfolio_value = pf["Verdi"].sum()
        return_pct = ((portfolio_value / invested_value) - 1) * 100 if invested_value else 0
        p3.metric("Avkastning", f"{return_pct:+.1f}%")
        weighted_volatility = ((pf["Volatilitet %"] * pf["Verdi"]).sum() / portfolio_value) if portfolio_value else 0
        concentration = ((pf["Verdi"] / portfolio_value).max() * 100) if portfolio_value else 0
        p4, p5 = st.columns(2)
        p4.metric("Vektet volatilitet", f"{weighted_volatility:.1f}%")
        p5.metric("Største eksponering", f"{concentration:.1f}%")
        portfolio_status = "Lavere risiko" if weighted_volatility < 20 and concentration < 35 else "Moderat risiko" if weighted_volatility < 35 and concentration < 50 else "Høy risiko"
        st.info(f"Samlet porteføljestatus: **{portfolio_status}**")
        atr_stop = detail["close"] - (2 * detail["atr"])
        risk_budget = capital * risk_per_trade_pct / 100
        suggested_shares = max(0, int(risk_budget / max(detail["close"] - atr_stop, 0.01)))
        s1, s2, s3 = st.columns(3)
        s1.metric("ATR-stop-loss", f"{atr_stop:.2f}")
        s2.metric("Risiko per handel", f"{risk_budget:,.0f}")
        s3.metric("Foreslått antall", str(suggested_shares))
        if pf["Verdi"].sum() > capital:
            st.warning("Porteføljen overstiger angitt investeringskapital.")
        for _, row in pf[pf["Verdi"] > max_position_value].iterrows():
            st.warning(f"{row['Symbol']}: posisjonen er {row['Andel %']:.1f}% av kapitalen og overstiger grensen på {max_position_pct}%.")
        st.caption(f"Risikokontroll: ingen enkeltposisjon bør overstige {max_position_value:,.0f} ({max_position_pct}% av kapitalen).")
        for _, row in pf[pf["Risiko"] != "OK"].iterrows():
            if row["Risiko"] == "UTLØST":
                st.error(f"{row['Symbol']}: stop-loss er brutt ({row['Siste']:.2f} ≤ {row['Stop-loss']:.2f})")
            else:
                st.warning(f"{row['Symbol']}: kursen nærmer seg stop-loss ({row['Siste']:.2f} mot {row['Stop-loss']:.2f})")
        st.dataframe(pf.style.format({"Kjøpspris":"{:.2f}", "Siste":"{:.2f}", "Stop-loss":"{:.2f}", "Verdi":"{:.0f}", "Gevinst":"{:+.0f}", "Avkastning %":"{:+.1f}", "Andel %":"{:.1f}", "Foreslått maks":"{:.0f}", "Volatilitet %":"{:.1f}"}), use_container_width=True, hide_index=True)
        st.markdown("#### Sektorfordeling")
        sector_values = pf.groupby("Sektor")["Verdi"].sum().sort_values(ascending=False)
        sector_pct = (sector_values / sector_values.sum() * 100).rename("Andel %").to_frame()
        st.bar_chart(sector_pct)
        st.dataframe(sector_pct.style.format("{:.1f}%"), use_container_width=True)
        concentrated = sector_pct[sector_pct["Andel %"] > 50]
        if not concentrated.empty:
            st.warning("Høy sektorkonsentrasjon: " + ", ".join(f"{name} ({row['Andel %']:.1f}%)" for name, row in concentrated.iterrows()))
        st.markdown("#### Porteføljekorrelasjon")
        corr = correlation_data(pf["Symbol"].tolist())
        if len(corr.columns) > 1:
            st.dataframe(corr.style.background_gradient(cmap="RdYlGn", vmin=-1, vmax=1).format("{:.2f}"), use_container_width=True)
            pairs = []
            for i, left in enumerate(corr.columns):
                for right in corr.columns[i + 1:]:
                    if corr.loc[left, right] >= 0.75:
                        pairs.append(f"{left} og {right} ({corr.loc[left, right]:.2f})")
            if pairs:
                st.warning("Høy korrelasjon kan gi skjult konsentrasjon: " + ", ".join(pairs))
        else:
            st.info("Legg inn minst to beholdninger for å beregne korrelasjon.")
    else:
        st.info("Legg inn beholdninger i sidepanelet og sørg for at symbolene finnes i watchlisten.")
