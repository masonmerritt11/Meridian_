"""The full MERIDIAN organization: 9 departments, 52 logical roles.
Roles with an implementing worker class are registered ACTIVE; the rest are PLANNED (defined, not running).
PLANNED roles are never shown as healthy or producing output."""
from __future__ import annotations

DEPARTMENTS: list[tuple[str, str, str]] = [
    ("trend", "Trend Following & Momentum", "Directional signals from price trend and momentum."),
    ("stat_arb", "Statistical Arbitrage", "Relative-value and mean-reversion research."),
    ("global_macro", "Global Macro", "Economic calendar, central banks, cross-asset macro."),
    ("liquidity", "Liquidity & Microstructure", "Spreads, depth, sessions, trading costs."),
    ("volatility", "Volatility & Regime Research", "Volatility measurement and regime classification."),
    ("portfolio", "Portfolio Intelligence", "Sizing, correlation budgets, attribution."),
    ("risk", "Risk & Compliance", "Independent deterministic risk controls and position monitoring."),
    ("data_ops", "Data, Engineering & Operations", "Market data, event bus, reliability, audit."),
    ("executive", "Executive Management & Investment Committee", "Coordination, research oversight, committee."),
]

# (role_id, department, name, responsibility, escalation_to)
ROLES: list[tuple[str, str, str, str, str]] = [
    # Trend (7)
    ("trend.signal_analyst", "trend", "Trend Signal Analyst", "EMA trend + Donchian breakout signals with ATR stops.", "exec.research_officer"),
    ("trend.breakout_specialist", "trend", "Breakout Specialist", "Range/consolidation breakout detection.", "trend.portfolio_lead"),
    ("trend.momentum_ranker", "trend", "Momentum Ranker", "Cross-asset momentum ranking.", "trend.portfolio_lead"),
    ("trend.multi_timeframe", "trend", "Multi-Timeframe Analyst", "Alignment of trends across timeframes.", "trend.portfolio_lead"),
    ("trend.exhaustion_monitor", "trend", "Trend Exhaustion Monitor", "Overextension and divergence detection.", "trend.portfolio_lead"),
    ("trend.carry_roll", "trend", "Carry & Roll Analyst", "Futures term structure, roll yield, funding.", "trend.portfolio_lead"),
    ("trend.portfolio_lead", "trend", "Trend Portfolio Lead", "Owns trend department output quality.", "exec.research_officer"),
    # Stat arb (6)
    ("stat_arb.pairs", "stat_arb", "Pairs Analyst", "Gold/silver and crypto pair relationships.", "stat_arb.lead"),
    ("stat_arb.cointegration", "stat_arb", "Cointegration Researcher", "Cointegration testing and half-life.", "stat_arb.lead"),
    ("stat_arb.mean_reversion", "stat_arb", "Mean-Reversion Analyst", "Short-horizon reversion signals.", "stat_arb.lead"),
    ("stat_arb.basis", "stat_arb", "Basis Spread Analyst", "Futures vs spot basis.", "stat_arb.lead"),
    ("stat_arb.cross_venue", "stat_arb", "Cross-Venue Analyst", "Price dislocations across venues.", "stat_arb.lead"),
    ("stat_arb.lead", "stat_arb", "Stat Arb Lead", "Owns stat-arb output quality.", "exec.research_officer"),
    # Global macro (7)
    ("macro.calendar_analyst", "global_macro", "Economic Calendar Analyst", "Fetches and normalizes scheduled macro events.", "exec.chief_macro_officer"),
    ("macro.central_bank", "global_macro", "Central Bank Analyst", "Fed and global policy assessment.", "exec.chief_macro_officer"),
    ("macro.inflation", "global_macro", "Inflation Analyst", "CPI/PCE trend assessment.", "exec.chief_macro_officer"),
    ("macro.rates_dollar", "global_macro", "Rates & Dollar Analyst", "Yields and USD impact on gold/crypto.", "exec.chief_macro_officer"),
    ("macro.energy", "global_macro", "Energy & Commodities Analyst", "Oil inventories, OPEC, supply.", "exec.chief_macro_officer"),
    ("macro.geopolitical", "global_macro", "Geopolitical Analyst", "Geopolitical risk events.", "exec.chief_macro_officer"),
    ("macro.regime", "global_macro", "Cross-Asset Regime Analyst", "Risk-on/risk-off regime.", "exec.chief_macro_officer"),
    # Liquidity (5)
    ("liquidity.order_book", "liquidity", "Order Book Analyst", "Depth and imbalance analysis.", "risk.chief_risk_officer"),
    ("liquidity.spread_monitor", "liquidity", "Spread Monitor", "Spread tracking and alerts.", "risk.chief_risk_officer"),
    ("liquidity.scorer", "liquidity", "Liquidity Scorer", "Per-contract liquidity score.", "risk.chief_risk_officer"),
    ("liquidity.sessions", "liquidity", "Session Analyst", "Trading hours and session effects.", "risk.chief_risk_officer"),
    ("liquidity.cost", "liquidity", "Trading Cost Analyst", "Estimated fees/slippage for advisories.", "risk.chief_risk_officer"),
    # Volatility (5)
    ("vol.realized", "volatility", "Realized Volatility Analyst", "Realized vol and ATR measurement.", "exec.research_officer"),
    ("vol.regime", "volatility", "Regime Classifier", "Trend/range/high-vol regime labels.", "exec.research_officer"),
    ("vol.forecast", "volatility", "Volatility Forecaster", "Forward volatility estimates.", "exec.research_officer"),
    ("vol.tail", "volatility", "Tail Risk Analyst", "Gap and tail-event risk.", "risk.chief_risk_officer"),
    ("vol.correlation", "volatility", "Correlation Analyst", "Rolling cross-asset correlation.", "risk.chief_risk_officer"),
    # Portfolio (5)
    ("portfolio.constructor", "portfolio", "Portfolio Constructor", "Ranks live recommendations and allocates the account (risk + capital limits).", "exec.committee_coordinator"),
    ("portfolio.sizer", "portfolio", "Position Sizer", "Risk-budget sizing proposals.", "risk.chief_risk_officer"),
    ("portfolio.correlation_budget", "portfolio", "Correlation Budgeter", "Correlated-exposure budgeting.", "risk.chief_risk_officer"),
    ("portfolio.attribution", "portfolio", "Performance Attribution Analyst", "Advisory track record and attribution.", "exec.chief_of_staff"),
    ("portfolio.pnl", "portfolio", "Equity & PnL Tracker", "Equity from CIO-recorded fills.", "exec.chief_of_staff"),
    # Risk (6)
    ("risk.chief_risk_officer", "risk", "Chief Risk Officer (Risk Control Engine)", "Deterministic ALLOW/RESTRICT/BLOCK on proposals. Veto is final.", "cio"),
    ("risk.position_monitor", "risk", "Position Monitor", "HOLD / EXIT / REVIEW advisories on CIO-recorded positions.", "cio"),
    ("risk.exposure", "risk", "Exposure Analyst", "Gross/net and correlated exposure reporting.", "risk.chief_risk_officer"),
    ("risk.limits", "risk", "Limits Officer", "Daily/weekly loss-limit tracking.", "risk.chief_risk_officer"),
    ("risk.compliance", "risk", "Compliance Officer", "Advisory-only boundary and audit checks.", "cio"),
    ("risk.event_risk", "risk", "Event Risk Analyst", "Event blackout management.", "risk.chief_risk_officer"),
    # Data / ops (6)
    ("data.instrument_discovery", "data_ops", "Instrument Discovery", "Resolves the 6-asset universe to live products.", "exec.chief_of_staff"),
    ("data.market_ingest", "data_ops", "Market Data Ingestor", "Fetches, validates, normalizes candles and books.", "exec.chief_of_staff"),
    ("data.freshness_monitor", "data_ops", "Data Quality Monitor", "Freshness and integrity of every dataset.", "exec.chief_of_staff"),
    ("data.event_relay", "data_ops", "Event Bus Relay", "Moves outbox events to Redis Streams.", "exec.chief_of_staff"),
    ("data.reliability", "data_ops", "Platform Reliability Engineer", "Uptime, alerting, recovery.", "exec.chief_of_staff"),
    ("data.archivist", "data_ops", "Audit Archivist", "Audit retention and integrity verification.", "risk.compliance"),
    # Executive (5)
    ("exec.chief_of_staff", "executive", "Chief of Staff", "Case intake, macro attachment, expiry sweeps, reporting.", "cio"),
    ("exec.research_officer", "executive", "Chief Research Officer", "Independent challenge of every signal.", "cio"),
    ("exec.chief_macro_officer", "executive", "Chief Macro Officer", "Cross-market macro regime oversight.", "cio"),
    ("exec.committee_coordinator", "executive", "Investment Committee Coordinator", "Advisory memos, ranking, dissent.", "cio"),
    ("exec.cio_liaison", "executive", "CIO Liaison", "Delivers recommendations to the human CIO.", "cio"),
]

assert len(DEPARTMENTS) == 9
assert len(ROLES) == 52, len(ROLES)
