CREATE SCHEMA IF NOT EXISTS tfm_bot;

--------------------------------------------------
-- CONTACTS
--------------------------------------------------

CREATE TABLE tfm_bot.contacts (
    id SERIAL PRIMARY KEY,
    name TEXT,
    phone TEXT UNIQUE,
    email TEXT UNIQUE,
);


--------------------------------------------------
-- PROJECTS
--------------------------------------------------

CREATE TABLE tfm_bot.projects (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER
        REFERENCES tfm_bot.contacts(id),
    name TEXT NOT NULL,
    description TEXT,
    address TEXT,
    status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    start_date DATE,
    end_date DATE,
    -- set when manager clicks the trigger button to start the availability flow
    availability_requested_at TIMESTAMP
);


--------------------------------------------------
-- TASKS
--------------------------------------------------

CREATE TABLE tfm_bot.tasks (
    id SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL
        REFERENCES tfm_bot.projects(id),
    contact_id INTEGER
        REFERENCES tfm_bot.contacts(id),
    title TEXT NOT NULL,
    description TEXT,
    address TEXT,
    status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    start_date DATE,
    end_date DATE,
    availability_requested_at TIMESTAMP
);

--------------------------------------------------
-- CONVERSATIONS
--
-- One open conversation per contact at any time.
-- States: COLLECTING_AVAILABILITY → CONFIRMING_AVAILABILITY → CLOSED
--         CLOSED → fallback message → CLOSED
           CLOSED → message → COLLECTING_AVAILABILITY
--------------------------------------------------

CREATE TABLE tfm_bot.conversations (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER NOT NULL
        REFERENCES tfm_bot.contacts(id),
    state TEXT NOT NULL
        CHECK (state IN (
            'AWAITING_REPLY',
            'COLLECTING_AVAILABILITY',
            'CONFIRMING_AVAILABILITY',
            'CLOSED'
        )),
    -- incremented each time the client gives vague/incomplete availability
    clarification_count INTEGER NOT NULL DEFAULT 0,
    last_message_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP,
    closed_at TIMESTAMP
);

--------------------------------------------------
-- MESSAGES
--------------------------------------------------

CREATE TABLE tfm_bot.messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL
        REFERENCES tfm_bot.conversations(id),
    contact_id INTEGER
        REFERENCES tfm_bot.contacts(id),
    direction TEXT NOT NULL
        CHECK (direction IN ('inbound', 'outbound')),
    content_raw TEXT,
    content_structured JSONB,
    intent TEXT
        CHECK (intent IN (
            'availability',
            'confirmation',
            'reschedule',
            'other'
        )),
    -- full LLM JSON output, for debugging
    llm_raw_response JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


--------------------------------------------------
-- CONTACT AVAILABILITIES
--
-- Slots are always stored as concrete timestamps (start_ts / end_ts).
-- status lifecycle: active → selected (manager picks it)
--                          → cancelled (client corrected or rejected)
--------------------------------------------------

CREATE TABLE tfm_bot.contact_availabilities (
    id SERIAL PRIMARY KEY,
    contact_id INTEGER NOT NULL
        REFERENCES tfm_bot.contacts(id),
    project_id INTEGER
        REFERENCES tfm_bot.projects(id),
    task_id INTEGER
        REFERENCES tfm_bot.tasks(id),
    conversation_id INTEGER
        REFERENCES tfm_bot.conversations(id),
    -- the inbound message that produced this slot (for traceability)
    source_message_id INTEGER
        REFERENCES tfm_bot.messages(id),

    -- concrete slot (always resolved to absolute timestamps)
    start_ts TIMESTAMP NOT NULL,
    end_ts TIMESTAMP NOT NULL,

    -- lifecycle status
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'selected', 'cancelled')),

    -- optional validity window (used for recurring / window-bounded slots)
    valid_from DATE,
    valid_until DATE,

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_slot_order CHECK (end_ts > start_ts),
    CONSTRAINT chk_valid_range CHECK (
        valid_from IS NULL OR valid_until IS NULL OR valid_until >= valid_from
    )
);

CREATE INDEX idx_availability_contact ON tfm_bot.contact_availabilities(contact_id);
CREATE INDEX idx_availability_conversation ON tfm_bot.contact_availabilities(conversation_id);
CREATE INDEX idx_tasks_project ON tfm_bot.tasks(project_id);
CREATE INDEX idx_messages_conversation ON tfm_bot.messages(conversation_id);
