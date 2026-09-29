# Offboarding an instance

When a managed-hosting customer cancels, for a single-host multi-instance
setup (see [`README.md`](../README.md#multiple-instances-on-one-host)).
Replace `acme` with the instance name.

1. **Final backup**, kept outside the instance directory you're about to
   delete:

   ```bash
   export CRM_DB_PATH=instances/acme/data/crm.db
   export CRM_ENV_FILE=instances/acme/.env
   export BACKUP_DIR=/var/backups/crm-offboarded/acme
   ./scripts/backup.sh
   ```

2. **Hand off data**, if the contract calls for it. The backup is a single
   sqlite file — send it over whatever channel you already use for secrets
   (not email/Slack in the clear). No export tooling beyond the backup
   script is needed; the customer's own DB is the export.

3. **Stop and remove the stack**:

   ```bash
   docker compose --project-name crm-acme \
     -f docker-compose.yml -f docker-compose.prod.yml \
     -f docker-compose.port.yml -f docker-compose.instance.yml \
     down
   ```

4. **Remove the Caddy/nginx site block** for `acme.example.com` from the
   proxy config on the host and reload it (see [`DEPLOY.md`](DEPLOY.md)).

5. **Retain, then delete.** Keep the final backup for your agreed retention
   window, then delete `instances/acme/` (env file, data, per-instance
   backups) and the off-host copy from step 1. The row in
   `instances/ports.tsv` can be left as-is — it only prevents port reuse,
   never reassigned automatically — or deleted by hand if you want the
   port back.
