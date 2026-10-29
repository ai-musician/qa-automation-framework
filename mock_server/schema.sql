-- The API's tables in Postgres. Run by the API every time it starts (mock_server/store.py), so every statement is
-- safe to run again: IF NOT EXISTS, and the seed users are inserted with ON CONFLICT DO NOTHING.

CREATE TABLE IF NOT EXISTS users (
    seq          bigserial,                        -- insertion order, so lists come back in the order users were created
    id           text PRIMARY KEY,
    name         text NOT NULL,
    email        text NOT NULL,
    account_type text NOT NULL CHECK (account_type IN ('standard', 'premium')),
    created_at   timestamptz NOT NULL
);
-- one account per email, ignoring case: the database enforces it, so two requests at the same moment can't both win
CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique ON users (lower(email));

CREATE TABLE IF NOT EXISTS transactions (
    seq          bigserial,
    id           text PRIMARY KEY,
    user_id      text NOT NULL REFERENCES users (id) ON DELETE CASCADE,   -- a deleted user's transactions go with them
    amount       numeric(12, 2) NOT NULL CHECK (amount > 0),              -- money: exact decimals, never a float
    type         text NOT NULL CHECK (type IN ('transfer', 'deposit', 'withdrawal')),
    recipient_id text,              -- no foreign key: the sender's history keeps the id after the recipient is deleted
    status       text NOT NULL,
    created_at   timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS transactions_by_user ON transactions (user_id, seq);
