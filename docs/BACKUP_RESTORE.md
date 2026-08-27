# Backup & Restore

**PostgreSQL is the authoritative business record.** Redis holds only cache,
throttling counters and offline-session coordination — it is disposable and is
**never** a financial source of truth. A cold Redis costs a few slow requests
and a reset rate-limit window, nothing more.

Nothing in this document has been run against Railway. The local drill below
*has* been run and verified (see the Stage 17 report and
`apps/core/tests/test_backup_restore.py`).

---

## 1. Local PostgreSQL backup & restore

Custom-format dump (compressed, selective restore, parallelisable):

```bash
pg_dump -Fc -f viable_stone-$(date +%F).dump viable_stone
```

Restore **into a new database**, never over a live one:

```bash
createdb viable_stone_restored
pg_restore --no-owner --no-privileges -d viable_stone_restored viable_stone-YYYY-MM-DD.dump
```

Scripted drill (dump → restore into a scratch DB → print comparison metrics):

```bash
SOURCE_DB=viable_stone PGHOST=127.0.0.1 PGUSER=viable_stone_app PGPASSWORD=... \
  bash scripts/backup_restore_drill.sh
```

## 2. Encrypted logical backups to independent object storage

Keep an off-Railway copy so a whole-account loss is survivable:

```bash
pg_dump -Fc viable_stone \
  | gpg --encrypt --recipient ops@viable-stone.example \
  > viable_stone-$(date +%FT%H%M).dump.gpg
# upload the .gpg to a bucket in a DIFFERENT provider/account
```

* Encrypt **before** the bytes leave the host (GPG, or `age`).
* The bucket must be **private**, versioned, and in a different failure domain
  from Railway.
* Store the decryption key in a password manager / KMS, **not** beside the
  backups.

## 3. Railway-managed recovery (to configure later — not done yet)

* **Volume backups** — enable scheduled snapshots on the Postgres plugin volume
  in the Railway dashboard. Snapshots are same-provider; treat them as fast
  local recovery, not disaster recovery.
* **Point-in-time recovery (PITR)** — available on Railway's managed Postgres
  Pro tier. Enable WAL archiving / PITR and record the retention window. PITR
  lets you restore to any second inside the window after an accidental
  `DELETE`/`UPDATE`.
* Both are complementary to the encrypted off-site `pg_dump` in §2, which is the
  only copy that survives losing the Railway account itself.

## 4. Retention policy

| Tier | What | Keep |
|---|---|---|
| Hourly | Railway PITR window | ≥ 72 h |
| Daily | Encrypted `pg_dump` to off-site bucket | 30 days |
| Weekly | Encrypted `pg_dump` (Sunday) | 12 weeks |
| Monthly | Encrypted `pg_dump` (1st) | 12 months |

Object-storage lifecycle rules should enforce this automatically. Verify at
least one **monthly** restore per quarter (see §6).

## 5. Recovery objectives (shop-sized)

* **RPO (max data loss): 1 hour.** Achieved by PITR; the daily off-site dump
  caps worst-case loss at 24 h if Railway itself is unavailable.
* **RTO (time to serve again): 4 hours.** Provision a fresh Postgres, restore
  the latest dump / PITR, point `DATABASE_URL` at it, run
  `manage.py migrate`, redeploy.

## 6. Restore drill procedure

1. Provision an empty target database (`createdb`, or a new Railway Postgres).
2. Restore the most recent backup into it (`pg_restore`, or Railway PITR to a
   fork).
3. Run verification queries on the restored DB and compare to the source /
   to what you expect:

   ```sql
   SELECT count(*) FROM sales_sale;
   SELECT coalesce(sum(total),0)  FROM sales_sale;
   SELECT count(*) FROM sales_payment;
   SELECT coalesce(sum(amount),0) FROM sales_payment;
   SELECT count(*) FROM inventory_stockmovement;
   SELECT coalesce(sum(amount),0) FROM finance_expense WHERE NOT is_voided;
   ```

4. Application checks against the restored DB
   (`DATABASE_URL=<restored> python manage.py ...`):

   ```bash
   python manage.py check
   python manage.py migrate --plan          # expect: "No planned migrations"
   python manage.py shell -c "from apps.finance.services.reports import profit_report; \
     from apps.accounts.models import Branch; \
     print(profit_report(branch=Branch.objects.first(), period='month'))"
   ```

   Then run the readiness probe against an app instance pointed at the restored
   DB: `GET /api/v1/health/ready/` → `200 {"status":"ready"}`.

5. Record: backup timestamp, restore duration, row-count/total deltas (must be
   zero), who ran it. File it with the ops runbook.
6. Drop the scratch database.

## 7. What is *not* backed up (by design)

* **Redis** — cache/throttle/offline-coordination state. Rebuilds itself.
* **Container filesystem** — ephemeral. Private media (`expenses/…`,
  `restocks/…`) must live in object storage in production
  (`DJANGO_STORAGE_BACKEND=s3`); see `docs/PRODUCTION_ENV.md`.
* The version-controlled Viable Stone **logo** is a static brand asset in the
  repo; customer and expense documents are not and must never be.
