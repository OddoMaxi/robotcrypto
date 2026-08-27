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
    duration_ms REAL NOT NULL
);
