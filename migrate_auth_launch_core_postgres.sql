-- TenderAI recovery migration for PostgreSQL / Aurora PostgreSQL
-- Safe-ish additive migration: creates missing tables/columns expected by the auth-enabled build.
-- It does NOT drop data.
-- Existing historical rows may keep NULL user ownership until new data is created through the app.

BEGIN;

-- 1) users table
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_users_email ON users(email);

-- 2) ingest_runs.result_json
ALTER TABLE ingest_runs
    ADD COLUMN IF NOT EXISTS result_json TEXT;

-- 3) profiles ownership columns
ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS user_id INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'profiles_user_id_fkey'
    ) THEN
        ALTER TABLE profiles
            ADD CONSTRAINT profiles_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_profiles_user_id ON profiles(user_id);

-- 4) analysis_jobs ownership columns
ALTER TABLE analysis_jobs
    ADD COLUMN IF NOT EXISTS user_id INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'analysis_jobs_user_id_fkey'
    ) THEN
        ALTER TABLE analysis_jobs
            ADD CONSTRAINT analysis_jobs_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES users(id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_analysis_jobs_user_id ON analysis_jobs(user_id);

-- 5) user_tender_decisions table
CREATE TABLE IF NOT EXISTS user_tender_decisions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    tender_id INTEGER NOT NULL,
    pursuit_status VARCHAR(50) NOT NULL DEFAULT 'not_decided',
    owner VARCHAR(255),
    next_action VARCHAR(255),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_user_tender_decision UNIQUE (user_id, tender_id),
    CONSTRAINT user_tender_decisions_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT user_tender_decisions_tender_id_fkey FOREIGN KEY (tender_id) REFERENCES tender_cache(id)
);

CREATE INDEX IF NOT EXISTS ix_user_tender_decisions_user_id ON user_tender_decisions(user_id);
CREATE INDEX IF NOT EXISTS ix_user_tender_decisions_tender_id ON user_tender_decisions(tender_id);

-- 6) helpful indexes if missing
CREATE INDEX IF NOT EXISTS ix_profiles_is_active ON profiles(is_active);
CREATE INDEX IF NOT EXISTS ix_analysis_jobs_tender_id ON analysis_jobs(tender_id);
CREATE INDEX IF NOT EXISTS ix_analysis_jobs_profile_id ON analysis_jobs(profile_id);

COMMIT;

-- Optional follow-up after your first signup:
-- Existing rows in profiles / analysis_jobs will have NULL user_id.
-- New records created through the auth-enabled app will populate ownership correctly.
