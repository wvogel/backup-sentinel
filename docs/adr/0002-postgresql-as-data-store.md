# ADR-0002: PostgreSQL as data store

## Status

Accepted

## Context

Backup Sentinel persists cluster inventory, backup events, restore tests, audit logs, and notification history. The data model is relational (VMs belong to clusters, events belong to VMs) and requires:

- ACID guarantees for the audit log and notification dedup
- Full-text queries (notification search in future)
- JSONB support for report snapshots
- Mature tooling for backup and restore
- Strong typing and constraints

Options considered:
1. **SQLite** — simple, file-based, zero setup. But: no concurrent writes, limited to single-container deployments, no JSONB.
2. **MySQL / MariaDB** — widely deployed, but weaker JSON handling and no `make_interval()` / `INTERVAL` arithmetic.
3. **PostgreSQL** — the industry standard for operational databases, excellent Python tooling via psycopg.
4. **Embedded K/V (e.g. BadgerDB, RocksDB)** — too low-level for a relational data model.

## Decision

Use PostgreSQL 18 as the primary data store. Access via `psycopg[binary]` with `dict_row` cursor factory and `psycopg-pool` for connection pooling.

## Consequences

**Positive:**
- Relational integrity enforced at the database level (foreign keys, unique constraints)
- JSONB columns for report snapshots allow flexible schema evolution without migrations
- `ON CONFLICT ... DO UPDATE` enables atomic dedup operations
- Interval arithmetic (`now() - make_interval(days => X)`) keeps time-based queries clean
- Mature backup story — `pg_dump`, streaming replication, PITR if needed

**Negative:**
- Adds a second container to the stack (postgres)
- Operators need basic SQL / Postgres knowledge for debugging
- Storage footprint is larger than SQLite for small deployments
- No embedded fallback — cannot run the app without a reachable Postgres

**Neutral:**
- Schema is managed via `CREATE TABLE IF NOT EXISTS` statements in `app/db_core.py` rather than a migrations framework. This is sufficient for additive changes but will require migration to a proper framework (e.g. Alembic) if we ever need `ALTER TABLE` drops or column renames.
