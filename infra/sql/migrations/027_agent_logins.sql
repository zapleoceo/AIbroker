-- 027: subscription login health + dashboard re-login sessions (docs/subscription-logins.md).
-- Idempotent. Apply by hand as the schema owner (-U aibroker); default privileges
-- (013) grant DML to aibroker_app automatically.

-- One row per worker login: what the CLI itself reports, refreshed by the worker.
-- email is kept while logged out (the last known mailbox), so an alert can name it.
CREATE TABLE IF NOT EXISTS agent_login_status (
    provider    VARCHAR(32)  NOT NULL,
    account_id  VARCHAR(64)  NOT NULL,
    logged_in   BOOLEAN      NOT NULL,
    email       VARCHAR(320),
    plan        VARCHAR(64),
    detail      VARCHAR(200),
    checked_at  TIMESTAMP    NOT NULL,
    changed_at  TIMESTAMP    NOT NULL,
    PRIMARY KEY (provider, account_id)
);

-- A re-login started from the dashboard and carried out by the account's own worker.
-- code_sealed holds the Claude paste code encrypted to the worker's one-time public key;
-- the worker clears it in the same UPDATE that consumes it.
CREATE TABLE IF NOT EXISTS agent_login_sessions (
    id           BIGSERIAL    PRIMARY KEY,
    provider     VARCHAR(32)  NOT NULL,
    account_id   VARCHAR(64)  NOT NULL,
    state        VARCHAR(20)  NOT NULL,
    verify_url   TEXT,
    user_code    VARCHAR(64),
    public_key   TEXT,
    code_sealed  TEXT,
    message      VARCHAR(300),
    created_at   TIMESTAMP    NOT NULL,
    updated_at   TIMESTAMP    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agent_login_sessions_account
    ON agent_login_sessions (provider, account_id, state);
