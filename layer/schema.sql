-- Research store for paper candidates. UNKNOWN features stay NULL or the
-- text UNKNOWN. This file is applied by layer/db.py. It does not hold keys.

CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY,
  mint TEXT NOT NULL,
  symbol TEXT,
  name TEXT,
  source TEXT,
  program TEXT,
  market_cap_usd REAL,
  observed_at TEXT,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_features (
  candidate_id TEXT NOT NULL,
  feature_key TEXT NOT NULL,
  feature_value TEXT NOT NULL,
  source TEXT,
  observed_at TEXT,
  PRIMARY KEY (candidate_id, feature_key)
);

CREATE TABLE IF NOT EXISTS agent_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  verdict TEXT,
  review_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  thresholds_json TEXT NOT NULL,
  override_attempt INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_trades (
  trade_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL,
  mint TEXT NOT NULL,
  side TEXT NOT NULL,
  size_sol REAL NOT NULL,
  price_sol REAL,
  costs_json TEXT NOT NULL,
  fill_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
  position_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL,
  mint TEXT NOT NULL,
  size_sol REAL NOT NULL,
  tokens REAL,
  entry_price REAL,
  remaining_fraction REAL NOT NULL,
  status TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  high_water_price REAL,
  strategy_id TEXT,
  state_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_exits (
  exit_id TEXT PRIMARY KEY,
  position_id TEXT NOT NULL,
  family TEXT NOT NULL,
  fraction REAL NOT NULL,
  proceeds_sol REAL,
  costs_json TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_receipts (
  receipt_id TEXT PRIMARY KEY,
  candidate_id TEXT,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  signature TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_research (
  day TEXT PRIMARY KEY,
  summary_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_versions (
  strategy_id TEXT NOT NULL,
  version TEXT NOT NULL,
  hypothesis TEXT NOT NULL,
  params_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (strategy_id, version)
);

CREATE INDEX IF NOT EXISTS idx_features_candidate ON candidate_features(candidate_id);
CREATE INDEX IF NOT EXISTS idx_reviews_candidate ON agent_reviews(candidate_id);
CREATE INDEX IF NOT EXISTS idx_risk_candidate ON risk_decisions(candidate_id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
