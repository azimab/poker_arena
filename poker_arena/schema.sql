CREATE TABLE IF NOT EXISTS accounts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bots (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id bigint NOT NULL REFERENCES accounts (id),
    name text,
    source text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'rejected', 'retired')),
    error text,
    mu double precision NOT NULL DEFAULT 25.0,
    sigma double precision NOT NULL DEFAULT 25.0 / 3,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS bots_one_active_per_account ON bots (account_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS matches (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bot_a bigint NOT NULL REFERENCES bots (id),
    bot_b bigint NOT NULL REFERENCES bots (id),
    score integer NOT NULL,
    legs jsonb NOT NULL,
    played_at timestamptz NOT NULL DEFAULT now()
);
