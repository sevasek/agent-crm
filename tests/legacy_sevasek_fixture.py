"""Unversioned sqlite file shaped like a sevasek/crm database.

sevasek/crm is private and was not readable from this environment. This
fixture is reconstructed from the columns agent-crm's v0 upgrade already
knows how to add, plus the live catalog (service slugs, Health Check stage
keys and role flags) read from crm.sevasek.com's MCP list_catalog on
2026-10-07. user_version stays 0. deal_tags, app_install, users.session_version
and mcp_oauth_used_codes are absent on purpose: those are agent-crm additions.
A leftover rate_limit_hits table is included so migrate_003 still drops it.
"""
import sqlite3


def build_legacy_sevasek_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE partners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            is_company INTEGER NOT NULL DEFAULT 0,
            parent_id INTEGER REFERENCES partners(id),
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            website TEXT,
            title TEXT,
            address TEXT,
            social_url TEXT,
            preferred_channel TEXT,
            industry TEXT,
            team_size INTEGER,
            linkedin_url TEXT,
            x_url TEXT,
            instagram_url TEXT,
            facebook_url TEXT,
            youtube_url TEXT,
            owner_key TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            description TEXT,
            nurture_list_slug TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT
        );

        CREATE TABLE offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0,
            pitch TEXT,
            proof_point TEXT,
            price_anchor TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            service_id INTEGER REFERENCES services(id),
            price REAL,
            currency TEXT,
            description TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_id INTEGER NOT NULL REFERENCES partners(id),
            service_id INTEGER NOT NULL REFERENCES services(id),
            stage TEXT NOT NULL DEFAULT 'new',
            source TEXT,
            value_estimate REAL,
            pain_points TEXT,
            goals TEXT,
            next_action TEXT,
            next_action_date TEXT,
            created_at TEXT,
            updated_at TEXT,
            closed_at TEXT,
            offer_id INTEGER REFERENCES offers(id),
            owner_key TEXT,
            external_ref TEXT,
            parent_deal_id INTEGER REFERENCES deals(id)
        );

        CREATE TABLE activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_id INTEGER NOT NULL REFERENCES partners(id),
            deal_id INTEGER REFERENCES deals(id),
            type TEXT NOT NULL,
            body TEXT,
            occurred_at TEXT,
            created_at TEXT
        );

        CREATE TABLE pipeline_stages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            label TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            is_default INTEGER NOT NULL DEFAULT 0,
            is_qualified_pool INTEGER NOT NULL DEFAULT 0,
            triggers_nurture INTEGER NOT NULL DEFAULT 0,
            is_won INTEGER NOT NULL DEFAULT 0,
            is_lost INTEGER NOT NULL DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE delegated_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id INTEGER NOT NULL REFERENCES deals(id),
            partner_id INTEGER NOT NULL REFERENCES partners(id),
            title TEXT NOT NULL,
            brief TEXT,
            owner TEXT NOT NULL,
            status TEXT NOT NULL,
            due_date TEXT,
            created_by TEXT,
            result_notes TEXT,
            webhook_notified_at TEXT,
            webhook_last_attempt_at TEXT,
            webhook_last_error TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE mcp_oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_name TEXT,
            redirect_uris TEXT NOT NULL,
            token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
            created_at TEXT
        );

        CREATE TABLE rate_limit_hits (
            bucket TEXT NOT NULL,
            hit_at REAL NOT NULL
        );
        """
    )

    stages = [
        ("new", "New", 0, 1, 0, 0, 0, 0),
        ("contacted", "Contacted", 1, 0, 0, 0, 0, 0),
        ("qualified", "Qualified", 2, 0, 1, 0, 0, 0),
        ("nurture", "Nurture", 3, 0, 0, 1, 0, 0),
        ("proposal", "Proposal", 4, 0, 0, 0, 0, 0),
        ("hc_booked", "HC booked", 5, 0, 0, 0, 0, 0),
        ("hc_paid", "HC paid", 6, 0, 0, 0, 0, 0),
        ("hc_in_delivery", "HC in delivery", 7, 0, 0, 0, 0, 0),
        ("hc_presented", "HC presented", 8, 0, 0, 0, 0, 0),
        ("won", "Won", 9, 0, 0, 0, 1, 0),
        ("hc_won", "HC won", 10, 0, 0, 0, 1, 0),
        ("lost", "Lost", 11, 0, 0, 0, 0, 1),
    ]
    conn.executemany(
        """INSERT INTO pipeline_stages
           (key, label, position, is_default, is_qualified_pool, triggers_nurture, is_won, is_lost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        stages,
    )

    services = [
        (1, "Health Check", "health-check"),
        (2, "Automation Delivery", "automation-delivery"),
        (5, "Automations Support", "automations-support"),
        (6, "IT Support", "it-support"),
        (3, "Dead Lead Reactivation", "dead-lead-reactivation"),
        (4, "GEO", "geo"),
    ]
    conn.executemany(
        "INSERT INTO services (id, name, slug, active) VALUES (?, ?, ?, 1)",
        services,
    )
    conn.execute(
        """INSERT INTO offers
           (id, name, is_default, service_id, price, currency, price_anchor, active)
           VALUES (1, 'Health Check', 1, 1, 700, 'AUD', 'A$700', 1)"""
    )
    conn.execute(
        """INSERT INTO offers
           (id, name, is_default, service_id, price, currency, active)
           VALUES (3, 'Automation Delivery', 0, 2, NULL, 'AUD', 1)"""
    )

    conn.execute(
        """INSERT INTO partners
           (id, is_company, name, email, phone, owner_key, social_url)
           VALUES (1, 1, 'North Church', 'office@north.example', '+61290000000', 'paul', NULL)"""
    )
    conn.execute(
        """INSERT INTO partners
           (id, is_company, parent_id, name, email, title, owner_key, social_url)
           VALUES (2, 0, 1, 'Ada North', 'ada@north.example', 'Office', 'bethany',
                   'https://www.linkedin.com/in/ada-north')"""
    )

    # Parent Health Check, then the two ladder children, then unrelated deals.
    conn.execute(
        """INSERT INTO deals
           (id, partner_id, service_id, stage, source, value_estimate, offer_id,
            owner_key, external_ref, pain_points, goals)
           VALUES (1, 1, 1, 'hc_presented', 'referral', 700, 1, 'paul',
                   'campaign:icp-hc-illawarra-2026-09', 'Manual roster', 'Fewer hours')"""
    )
    conn.execute(
        """INSERT INTO deals
           (id, partner_id, service_id, stage, source, parent_deal_id, owner_key)
           VALUES (2, 1, 2, 'new', 'hc-spawn:1', 1, 'paul')"""
    )
    conn.execute(
        """INSERT INTO deals
           (id, partner_id, service_id, stage, source, parent_deal_id, owner_key)
           VALUES (3, 1, 5, 'qualified', 'hc-spawn:1', 1, 'paul')"""
    )
    conn.execute(
        """INSERT INTO deals
           (id, partner_id, service_id, stage, source, owner_key, external_ref)
           VALUES (4, 2, 3, 'new', 'campaign', 'joe-bot', 'Campaign:Dead-Leads-2026')"""
    )
    conn.execute(
        """INSERT INTO deals
           (id, partner_id, service_id, stage, source, owner_key, external_ref)
           VALUES (5, 2, 4, 'contacted', 'ingest', 'paul', 'success-agent:mustard-seed')"""
    )

    conn.execute(
        """INSERT INTO activities (id, partner_id, deal_id, type, body, occurred_at)
           VALUES (1, 1, 1, 'note', 'Presented the Health Check', '2026-09-01T01:00:00')"""
    )
    conn.execute(
        """INSERT INTO activities (id, partner_id, deal_id, type, body, occurred_at)
           VALUES (2, 2, 4, 'call', 'No answer', '2026-09-02T01:00:00')"""
    )

    conn.execute(
        """INSERT INTO delegated_tasks
           (id, deal_id, partner_id, title, brief, owner, status, created_by,
            webhook_notified_at, webhook_last_attempt_at, webhook_last_error)
           VALUES (1, 1, 1, 'Send HC kickoff', 'Paid Health Check. Send the kickoff.',
                   'willow', 'done', 'joe',
                   '2026-09-01T00:00:00', '2026-09-01T00:00:00', NULL)"""
    )
    conn.execute(
        """INSERT INTO delegated_tasks
           (id, deal_id, partner_id, title, brief, owner, status, created_by,
            webhook_notified_at, webhook_last_attempt_at, webhook_last_error)
           VALUES (2, 4, 2, 'Chase the list', 'Dead leads',
                   'hermes', 'delegated', 'joe',
                   NULL, '2026-09-03T00:00:00', 'HTTP 502')"""
    )

    conn.execute(
        """INSERT INTO users (id, email, name, password_hash)
           VALUES (1, 'paul@sevasek.example', 'Paul', 'pbkdf2-sha256$stored-hash')"""
    )
    conn.execute(
        """INSERT INTO mcp_oauth_clients
           (client_id, client_name, redirect_uris, token_endpoint_auth_method)
           VALUES ('grok-connector', 'Grok', '["https://connector.example/callback"]', 'none')"""
    )
    conn.execute(
        "INSERT INTO rate_limit_hits (bucket, hit_at) VALUES ('login:203.0.113.8', 1.0)"
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
