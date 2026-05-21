# PostgreSQL Migration (MySQL → PostgreSQL)

Data has been migrated from MySQL to PostgreSQL via pgloader.
The steps below wire up the application layer to use PostgreSQL.

## Step 1 — Create the Tutor plugin

Create the file `~/.local/share/tutor/plugins/openedx_postgres.py` on the machine running Tutor.
Paste the following content exactly:

```python
from tutor import hooks

# Config keys — set these via `tutor config save` (see Step 2)
hooks.Filters.CONFIG_DEFAULTS.add_items([
    ("OPENEDX_POSTGRES_HOST",          "localhost"),
    ("OPENEDX_POSTGRES_PORT",          "5432"),
    ("OPENEDX_POSTGRES_DATABASE",      "openedx"),
    ("OPENEDX_POSTGRES_USERNAME",      "postgres"),
    ("OPENEDX_POSTGRES_PASSWORD",      ""),
    # student_module_history DB — set to same value as DATABASE if it's the same PG DB
    ("OPENEDX_POSTGRES_DATABASE_CSMH", "openedx"),
])

# This YAML block is appended to lms.yml and cms.yml.
# PyYAML uses last-key-wins, so this overrides the MySQL DATABASES block
# that Tutor's default template writes earlier in the file.
DB_PATCH = """
DATABASES:
  default:
    ATOMIC_REQUESTS: true
    CONN_MAX_AGE: 0
    ENGINE: django.db.backends.postgresql
    HOST: "{{ OPENEDX_POSTGRES_HOST }}"
    NAME: "{{ OPENEDX_POSTGRES_DATABASE }}"
    OPTIONS: {}
    PASSWORD: "{{ OPENEDX_POSTGRES_PASSWORD }}"
    PORT: "{{ OPENEDX_POSTGRES_PORT }}"
    USER: "{{ OPENEDX_POSTGRES_USERNAME }}"
  read_replica:
    CONN_MAX_AGE: 0
    ENGINE: django.db.backends.postgresql
    HOST: "{{ OPENEDX_POSTGRES_HOST }}"
    NAME: "{{ OPENEDX_POSTGRES_DATABASE }}"
    OPTIONS: {}
    PASSWORD: "{{ OPENEDX_POSTGRES_PASSWORD }}"
    PORT: "{{ OPENEDX_POSTGRES_PORT }}"
    USER: "{{ OPENEDX_POSTGRES_USERNAME }}"
  student_module_history:
    CONN_MAX_AGE: 0
    ENGINE: django.db.backends.postgresql
    HOST: "{{ OPENEDX_POSTGRES_HOST }}"
    NAME: "{{ OPENEDX_POSTGRES_DATABASE_CSMH }}"
    OPTIONS: {}
    PASSWORD: "{{ OPENEDX_POSTGRES_PASSWORD }}"
    PORT: "{{ OPENEDX_POSTGRES_PORT }}"
    USER: "{{ OPENEDX_POSTGRES_USERNAME }}"
"""

hooks.Filters.ENV_PATCHES.add_item(("lms-env", DB_PATCH))
hooks.Filters.ENV_PATCHES.add_item(("cms-env", DB_PATCH))
```

## Step 2 — Enable plugin and set connection details

```bash
tutor plugins enable openedx_postgres

tutor config save --set OPENEDX_POSTGRES_HOST=<your-postgres-host>
tutor config save --set OPENEDX_POSTGRES_PORT=5432
tutor config save --set OPENEDX_POSTGRES_DATABASE=<db-name>
tutor config save --set OPENEDX_POSTGRES_USERNAME=<db-user>
tutor config save --set OPENEDX_POSTGRES_PASSWORD=<db-password>
tutor config save --set OPENEDX_POSTGRES_DATABASE_CSMH=<csmh-db-name>
# ^ set CSMH to the same DB name as DATABASE if edxapp_csmh was migrated into the same PG database
```

## Step 3 — Reset PostgreSQL sequences (run against PG DB before starting the app)

pgloader copies data but does not reset auto-increment sequences.
Without this step, the first INSERT will fail with a duplicate key error.

```sql
-- Run this SQL against your PostgreSQL database:
DO $$
DECLARE
  seq_name TEXT;
  tbl_name TEXT;
  col_name TEXT;
BEGIN
  FOR seq_name, tbl_name, col_name IN
    SELECT pg_get_serial_sequence(quote_ident(table_name), column_name),
           table_name,
           column_name
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND column_default LIKE 'nextval%'
  LOOP
    EXECUTE format(
      'SELECT setval(%L, COALESCE(MAX(%I), 1)) FROM %I',
      seq_name, col_name, tbl_name
    );
  END LOOP;
END $$;
```

## Step 4 — Regenerate Tutor env and deploy

```bash
tutor config save
tutor k8s init
# Re-apply manual ConfigMap edits (CSRF whitelist, buffer-size, proxy-body-size) after init
```

## Step 5 — Verify Django migration state

```bash
tutor k8s exec lms -- ./manage.py lms migrate --check
# Should report 0 unapplied migrations (pgloader copied django_migrations table)
# If any are unapplied, run: tutor k8s exec lms -- ./manage.py lms migrate
```

## Step 6 — Confirm PostgreSQL is active

```bash
tutor k8s exec lms -- ./manage.py lms shell
# In the shell:
# from django.db import connection; print(connection.vendor)  # → postgresql
```
