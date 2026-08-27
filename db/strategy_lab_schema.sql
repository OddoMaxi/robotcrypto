-- MOMENTUM STRATEGY LAB V2 - own SQLite database (db/strategy_lab.db), fully
-- separate from db/momentum.db (the existing Momentum Bot's ledger, untouched).
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS lab_symbols (
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

-- one row per strategy evaluation of one MARKET_EVENT_ID (symbol+cycle), incl.
-- rejected/no-trade ones - mirrors the baseline bot's "log every signal" pattern
-- (section 15/17). dataset_phase/dataset_version are immutable at insert time.
CREATE TABLE IF NOT EXISTS strategy_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_event_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('UP','DOWN')),
    price REAL NOT NULL,
    score REAL NOT NULL,
    phase TEXT,                          -- e.g. IMPULSE/PULLBACK/REACCELERATION, NULL for strategies without phases
    velocity_10s REAL,                   -- market-event-level reading (same for every strategy on this symbol/cycle)
    acceleration_10s REAL,               -- market-event-level reading (same for every strategy on this symbol/cycle)
    persistence_score REAL,              -- PERSISTENT_MICRO_TREND's own score; NULL for every other strategy
    spread_bps REAL,
    exhaustion_risk REAL,
    late_entry_risk REAL,
    regime_label TEXT,
    meta_signal_strength REAL,
    agreement_count INTEGER,
    conflict_count INTEGER,
    expected_move_pct REAL,
    expected_cost_pct REAL,
    expected_net_edge_pct REAL,
    accepted INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT,
    dataset_phase TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    details TEXT NOT NULL                -- JSON: strategy-specific sub-scores/checks
);
CREATE INDEX IF NOT EXISTS idx_lab_signals_strategy_ts ON strategy_signals(strategy, ts);
CREATE INDEX IF NOT EXISTS idx_lab_signals_symbol_ts ON strategy_signals(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_lab_signals_agreement ON strategy_signals(agreement_count);

CREATE TABLE IF NOT EXISTS strategy_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('UP','DOWN')),
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    size REAL NOT NULL,
    risk_pct REAL NOT NULL,
    risk_amount REAL NOT NULL,
    confirmation_window_s REAL,
    entry_latency_ms REAL,
    entry_fee REAL,
    entry_slippage_pct REAL,
    agreement_count INTEGER,             -- denormalized at entry time, for cohort slicing (section 8/15)
    regime_label TEXT,
    dataset_phase TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')),
    exit_policy TEXT,                    -- which exit policy actually closed this real trade
    exit_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    exit_fee REAL,
    exit_slippage_pct REAL,
    exit_latency_ms REAL,
    spread_cost_pct REAL,
    gross_pnl_pct REAL,
    true_net_pnl REAL,                   -- $ terms: GROSS_RETURN - ENTRY_FEE - EXIT_FEE - SPREAD_COST - SLIPPAGE - LATENCY_IMPACT
    true_net_pnl_pct REAL,
    r_multiple REAL,
    mfe_pct REAL DEFAULT 0,
    mae_pct REAL DEFAULT 0,
    hold_s REAL,
    FOREIGN KEY (signal_id) REFERENCES strategy_signals(id)
);
CREATE INDEX IF NOT EXISTS idx_lab_trades_strategy_status ON strategy_trades(strategy, status);
CREATE INDEX IF NOT EXISTS idx_lab_trades_dataset ON strategy_trades(dataset_phase, dataset_version);

-- section 10: every configured exit policy is replayed as a counterfactual off
-- the SAME real forward price path as the trade it belongs to (no separate
-- capital/risk - shadow only) so exit strategies can be compared honestly.
CREATE TABLE IF NOT EXISTS exit_policy_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    policy TEXT NOT NULL,
    exit_time TEXT NOT NULL,
    exit_price REAL NOT NULL,
    hold_s REAL NOT NULL,
    gross_pnl_pct REAL NOT NULL,
    true_net_pnl_pct REAL NOT NULL,
    mfe_pct REAL NOT NULL,
    mae_pct REAL NOT NULL,
    FOREIGN KEY (trade_id) REFERENCES strategy_trades(id)
);
CREATE INDEX IF NOT EXISTS idx_exit_policy_trade ON exit_policy_results(trade_id);
CREATE INDEX IF NOT EXISTS idx_exit_policy_name ON exit_policy_results(policy);

-- section 9: per-signal, per-confirmation-window observation of whether the
-- signal was "still valid" (persistence held) N seconds later and what the
-- resulting price move/net-edge would have been. Computed only once that much
-- real time has actually elapsed since the signal - no look-ahead.
CREATE TABLE IF NOT EXISTS confirmation_window_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('UP','DOWN')),
    window_s REAL NOT NULL,
    still_valid INTEGER NOT NULL,
    price_move_pct_at_window REAL NOT NULL,
    would_be_net_edge_pct REAL NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES strategy_signals(id)
);
CREATE INDEX IF NOT EXISTS idx_confirmation_window_strategy ON confirmation_window_stats(strategy, window_s);

-- section 12: analyzed only after the fact, for moves that were NOT traded -
-- never used to rewrite the original (already-logged) rejection decision.
CREATE TABLE IF NOT EXISTS missed_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('UP','DOWN')),
    move_pct REAL NOT NULL,
    move_window_s REAL NOT NULL,
    first_detectable_ts TEXT,
    first_detectable_scores TEXT,        -- JSON: what every strategy's score was at first detectability
    reject_reason TEXT,
    what_happened_next TEXT              -- JSON
);
CREATE INDEX IF NOT EXISTS idx_missed_moves_symbol ON missed_moves(symbol, ts);

-- section 13: post-trade classification of losing trades. Analytical only -
-- read by research, never fed back into the live decision path.
CREATE TABLE IF NOT EXISTS false_positives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    classification TEXT NOT NULL,
    details TEXT NOT NULL,               -- JSON
    FOREIGN KEY (trade_id) REFERENCES strategy_trades(id)
);
CREATE INDEX IF NOT EXISTS idx_false_positives_trade ON false_positives(trade_id);

-- section 7: lead/lag observations, persisted for statistical aggregation only.
-- follower_net_expectancy_pct is filled in once the follower-exchange outcome is
-- known; NULL until then, never fabricated.
CREATE TABLE IF NOT EXISTS lead_lag_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('UP','DOWN')),
    leading_exchange TEXT NOT NULL,
    following_exchange TEXT NOT NULL,
    lead_time_ms REAL NOT NULL,
    price_propagation_pct REAL,
    velocity_propagation_ratio REAL,
    volume_propagation_ratio REAL,
    follower_net_expectancy_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_lead_lag_pair ON lead_lag_observations(leading_exchange, following_exchange);

CREATE TABLE IF NOT EXISTS lab_engine_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbols_scanned INTEGER NOT NULL,
    symbols_full_pass INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    cpu_percent REAL,
    rss_mb REAL,
    event_loop_lag_ms REAL,
    degraded_mode INTEGER NOT NULL DEFAULT 0,
    market_events INTEGER NOT NULL DEFAULT 0
);

-- section 16: every change to walk_forward.version in strategy_lab_config.yaml is
-- logged here permanently, the moment the process observes it at startup.
CREATE TABLE IF NOT EXISTS config_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    version TEXT NOT NULL,
    phase TEXT NOT NULL,
    UNIQUE(version, phase)
);
