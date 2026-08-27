PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    base_asset TEXT,
    quote_asset TEXT,
    tick_size REAL,
    step_size REAL,
    min_notional REAL,
    quote_volume_24h REAL,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, exchange)
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('UP','DOWN')),
    price REAL NOT NULL,
    spread_bps REAL,
    engine_scores TEXT NOT NULL,       -- JSON: per-engine up/down subscores
    momentum_confidence REAL NOT NULL,
    exhaustion_risk REAL NOT NULL,
    classification TEXT NOT NULL,
    entry_quality REAL,
    entry_type TEXT,
    accepted INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT,
    shadow_only INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_signals_classification ON signals(classification);

CREATE TABLE IF NOT EXISTS shadow_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('UP','DOWN')),
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    invalidation_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    size REAL NOT NULL,
    risk_pct REAL NOT NULL,
    risk_amount REAL NOT NULL,
    entry_type TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')),
    trailing_state TEXT,
    exit_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    fees_paid REAL DEFAULT 0,
    slippage_pct REAL DEFAULT 0,
    mfe_pct REAL DEFAULT 0,
    mae_pct REAL DEFAULT 0,
    net_pnl REAL,
    r_multiple REAL,
    latency_ms REAL,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);
CREATE INDEX IF NOT EXISTS idx_shadow_trades_status ON shadow_trades(status);

CREATE TABLE IF NOT EXISTS twin_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    horizon_s INTEGER NOT NULL,
    price REAL NOT NULL,
    pct_change REAL NOT NULL,
    mfe_pct REAL NOT NULL,
    mae_pct REAL NOT NULL,
    ts REAL NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);
CREATE INDEX IF NOT EXISTS idx_twin_signal ON twin_snapshots(signal_id);

CREATE TABLE IF NOT EXISTS engine_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbols_scanned INTEGER NOT NULL,
    symbols_promoted INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    cpu_percent REAL,
    rss_mb REAL,
    event_loop_lag_ms REAL,
    degraded_mode INTEGER NOT NULL DEFAULT 0
);

-- missions 7/8 (V1) + V1.1 mission 6: early up/down movers, tracked from first
-- significant anomaly (T0). exchange = FIRST_EXCHANGE (where first detected).
CREATE TABLE IF NOT EXISTS early_mover_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('UP','DOWN')),
    t0_price REAL NOT NULL,
    t0_confidence REAL NOT NULL,
    starting_score REAL,
    fast_score REAL,
    regime TEXT,
    second_exchange TEXT,
    third_exchange TEXT,
    lead_time_ms REAL,
    confirmation_delay_ms REAL,
    price_move_before_confirmation REAL,
    price_move_after_confirmation REAL,
    max_confidence REAL,
    time_to_peak_s REAL,
    mfe_pct REAL,
    mae_pct REAL,
    time_to_mfe_s REAL,
    time_to_mae_s REAL,
    time_to_025_s REAL,
    time_to_050_s REAL,
    time_to_100_s REAL,
    status TEXT NOT NULL DEFAULT 'TRACKING' CHECK (status IN ('TRACKING','DONE'))
);
CREATE INDEX IF NOT EXISTS idx_early_mover_symbol ON early_mover_events(symbol, ts);

CREATE TABLE IF NOT EXISTS early_mover_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    horizon_s INTEGER NOT NULL,
    pct_change REAL NOT NULL,
    ts REAL NOT NULL,
    FOREIGN KEY (event_id) REFERENCES early_mover_events(id)
);
CREATE INDEX IF NOT EXISTS idx_early_mover_returns_event ON early_mover_returns(event_id);

-- mission 4: lead/lag observations, persisted for statistical aggregation only -
-- never asserted as a causal "X leads Y" from a single instance
CREATE TABLE IF NOT EXISTS leader_lag_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    leading_exchange TEXT NOT NULL,
    following_exchange TEXT NOT NULL,
    lead_time_ms REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leader_lag_symbol ON leader_lag_events(symbol);

-- mission 9 (V1.1): stablecoin pairs are monitored separately, never mixed into
-- the normal momentum ranking/dataset
CREATE TABLE IF NOT EXISTS stablecoin_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    price REAL NOT NULL,
    deviation_pct REAL NOT NULL,
    anomaly_type TEXT NOT NULL CHECK (anomaly_type IN ('DEPEG','ABNORMAL_VOLATILITY','ABNORMAL_VOLUME')),
    severity REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stablecoin_symbol_ts ON stablecoin_events(symbol, ts);
