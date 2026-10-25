-- Ghost Data Still Exists: point-in-time database snapshot
-- Dialect: SQLite-compatible SQL
-- Captured at: 2026-03-08T09:00:00Z
-- Database topology: failover_replica
-- Compare the primary and any replica tables for replication drift.

BEGIN TRANSACTION;
CREATE TABLE customer_profiles_failover_replica (
    resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    full_name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
    account_status TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO "customer_profiles_failover_replica" VALUES('account_9019','user_17','Rui Sato','rui.sato@example.com','+1 555 0117','ACTIVE','2026-03-08T08:45:00Z');
INSERT INTO "customer_profiles_failover_replica" VALUES('account_9190','user_9190','Other Person','other.person@example.com','+1 555 0990','ACTIVE','2026-03-08T08:45:00Z');
CREATE TABLE customer_profiles_primary (
    resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    full_name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
    account_status TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO "customer_profiles_primary" VALUES('account_9190','user_9190','Other Person','other.person@example.com','+1 555 0990','ACTIVE','2026-03-08T08:45:00Z');
COMMIT;
