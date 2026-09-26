# python-toolkit Method Usage

How to call the public methods in the `python_toolkit` package. Signatures and return values below match the code in `src/python_toolkit/`.

## Package layout

```text
src/python_toolkit/
├── api/          client.py, dynamics_crm.py      (extra: api)
├── cdc/          mssql.py                        (extra: cdc)
├── common/       dates.py, json.py, logging.py, validation.py
├── parquet/      writer.py                       (extra: parquet)
├── sqlserver/    sqlserver.py                    (extra: sqlserver)
└── storage/      object_store.py
```

Each module exposes a standalone class. The package version is in `python_toolkit.__version__`.

### Conventions

- Import from the package: `from python_toolkit.<stack>.<module> import <Class>`.
- Most methods are `@staticmethod`s — call them on the class, no instance needed. `ApiClient` and `LocalObjectStore` are the exceptions.
- Methods that return structured data return a `dict`, so results can go straight into `Kestra.outputs(...)`.
- Keep secrets, connection strings, and tenant IDs in Kestra secrets or env vars — never hard-code them.

### Usage in Kestra

Install the package as a task dependency, with the extras the task needs:

```yaml
dependencies:
  - kestra
  - "python-toolkit[api,cdc] @ git+https://github.com/<owner>/<repo>.git@v0.1.0"

script: |
  from kestra import Kestra
  from python_toolkit.common.json import JsonTools
  from python_toolkit.cdc.mssql import MssqlCdc
  from python_toolkit.api.dynamics_crm import DynamicsCrmClient
```

---

## api

### client.py — `ApiClient`

A `requests`-based HTTP client bound to a base URL. Every request raises `requests.HTTPError` on a 4xx/5xx status.

```python
from python_toolkit.api.client import ApiClient

# Use as a context manager so the underlying session is closed.
with ApiClient(
    "https://example.com",
    headers={"Authorization": "Bearer <token>"},  # sent with every request
    timeout=30.0,
) as client:
    status = client.get("/api/status", params={"verbose": "true"})
    print(status.json())

    created = client.post("/api/items", json={"name": "Example"})
    print(created.status_code)
```

Constructor: `ApiClient(base_url, *, headers=None, timeout=30.0, session=None)` — pass `session` to reuse an existing `requests.Session` (the client will not close a session it did not create).

Public methods (all return `requests.Response`):

| Method | Notes |
|--------|-------|
| `request(method, path, *, params=None, json=None, headers=None)` | Per-call `headers` are merged over the constructor headers |
| `get(path, **kwargs)` | Same keyword arguments as `request` |
| `post(path, **kwargs)` | |
| `put(path, **kwargs)` | |
| `delete(path, **kwargs)` | |
| `close()` | Called automatically when used with `with` |

### dynamics_crm.py — `DynamicsCrmClient`

Azure AD token acquisition and Dataverse (Dynamics 365) record updates.

```python
from python_toolkit.api.dynamics_crm import DynamicsCrmClient

resource_url = "https://<org>.crm4.dynamics.com"

# 1. Get a token (Azure AD v1 client credentials grant).
token_result = DynamicsCrmClient.get_access_token(
    tenant_id="<tenant-guid>",
    client_id="<app-client-guid>",
    client_secret="<client-secret>",
    resource=f"{resource_url}/",
)
access_token = token_result["access_token"]

# 2. Update one record.
single_update = DynamicsCrmClient.update_entity(
    resource_url=resource_url,
    entity_set_name="accounts",
    entity_id="11111111-2222-3333-4444-555555666666",
    field_values={"name": "Updated Company", "description": "Updated via API"},
    token=access_token,
)

# 3. Update many records in one UpdateMultiple request.
bulk_update = DynamicsCrmClient.bulk_update_entities(
    resource_url=resource_url,
    entity_set_name="accounts",
    entity_type_name="account",
    updates=[
        {"entity_id": "11111111-2222-3333-4444-555555666666", "values": {"name": "Batch A"}},
        {"entity_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "field_values": {"name": "Batch B"}},
    ],
    token=access_token,
)

if not bulk_update["success"]:
    for error in bulk_update["errors"]:
        print(error["entity_id"], error["status_code"], error["message"])
```

Public methods — all accept keyword-only `timeout=30.0` and `session=None`:

| Method | Returns |
|--------|---------|
| `get_access_token(tenant_id, client_id, client_secret, resource)` | `access_token`, `token_type`, `expires_in`, `ext_expires_in`, `expires_on`, `not_before`, `resource` |
| `update_entity(resource_url, entity_set_name, entity_id, field_values, token)` | `entity_set_name`, `entity_id`, `updated_fields`, `status_code` |
| `bulk_update_entities(resource_url, entity_set_name, updates, token, *, entity_type_name=None, max_batch_size=1000)` | see below |

`update_entity` raises `RuntimeError` (with the Dataverse error payload) on any status other than 200/204.

**`bulk_update_entities` details**

- Each item in `updates` needs `entity_id` plus either `values` or `field_values` (a non-empty dict).
- Sends Dataverse's `UpdateMultiple` action with a `Targets` payload.
- `entity_type_name` is the singular logical name used in `@odata.type`. If omitted, it is derived by dropping a trailing `s` from `entity_set_name` — pass it explicitly when that is wrong (e.g. entity set `atm_atmterminalinformations` → type `atm_atmterminalinformation`).
- More than `max_batch_size` (default 1000, the Dataverse limit) updates raises `ValueError` — split larger lists first, e.g. with `SqlServer.iter_batches`.
- If a request fails, the batch is split in half recursively until the failing records are isolated. Records are only sent one at a time when a split narrows down to a single failure.
- Does not raise on record failures. Returns:

| Key | Meaning |
|-----|---------|
| `entity_set_name` | Entity set updated |
| `total_records` / `batch_size` | Number of updates submitted |
| `success` | `True` when no record failed |
| `success_count` / `updated_count` | Records updated |
| `error_count` | Records that failed |
| `status_code` | `200` if all succeeded, `207` if any failed |
| `errors` | List of `{entity_id, field_values, status_code, message}` — `message` is the short Microsoft error text |

---

## cdc

### mssql.py — `MssqlCdc`

SQL Server Change Data Capture polling using `pymssql` (no ODBC driver needed).

**Connection string** — semicolon-delimited, typically pointing at `master`; the target database is passed to each method:

```text
server=<host>;port=1433;user=<user>;password=<password>;database=master
```

Accepted keys: `server` (or `host`, `data source`), `port`, `user` (or `uid`), `password` (or `pwd`), optional `database` (or `initial catalog`).

**Capture instance** — pass `capture_instance`, or pass `schema_name` and `table_name` to use the default `<schema>_<table>` name.

**Watermark** — LSNs are exchanged as 20-character hex strings **without** `0x`. Store `last_start_lsn` and `last_seqval` from each `fetch_changes` result (e.g. in Kestra KV) and pass them back on the next run. Omit both on the first run.

```python
from python_toolkit.cdc.mssql import MssqlCdc

conn_str = "server=<host>;port=1433;user=<user>;password=<password>;database=master"

# Previous watermark from Kestra KV — None on the first run.
last_start_lsn = None
last_seqval = None

# 1. Work out the LSN range for this poll.
lsn_info = MssqlCdc.get_next_lsn(
    conn_str,
    "MyDb",
    capture_instance="dbo_TerminalCashPosition",
    # or: schema_name="dbo", table_name="TerminalCashPosition",
    last_start_lsn=last_start_lsn,
    last_seqval=last_seqval,
)

if lsn_info["has_changes"]:
    # 2. Fetch the next batch of changed rows.
    changes = MssqlCdc.fetch_changes(
        conn_str,
        "MyDb",
        lsn_info["capture_instance"],
        lsn_info["from_lsn"],
        lsn_info["to_lsn"],
        after_seqval=lsn_info["after_seqval"],
        batch_size=500,
    )

    # 3. Persist the new watermark once the rows are processed.
    if changes["should_persist"]:
        kv_value = MssqlCdc.build_kv_config(
            changes["capture_name"],
            changes["last_start_lsn"],
            changes["last_seqval"],
        )
```

Public methods:

| Method | Returns |
|--------|---------|
| `get_lsn_bounds(conn_str, database_name, capture_instance=None, *, schema_name=None, table_name=None)` | `capture_instance`, `min_lsn`, `max_lsn` |
| `get_next_lsn(conn_str, database_name, capture_instance=None, last_start_lsn=None, last_seqval=None, *, schema_name=None, table_name=None)` | see below |
| `fetch_changes(conn_str, database_name, capture_instance, from_lsn, to_lsn, *, after_seqval=None, batch_size=500, schema_name=None, table_name=None)` | see below |
| `build_kv_config(capture_name, last_start_lsn, last_seqval)` | `capture_name`, `last_start_lsn`, `last_seqval` — normalized for KV storage |

`get_next_lsn` returns:

| Key | Meaning |
|-----|---------|
| `capture_instance` | Resolved capture instance name |
| `last_start_lsn`, `last_seqval` | Normalized input watermark (`None` if not supplied) |
| `from_lsn` | Min LSN on the first run, otherwise `last_start_lsn` |
| `after_seqval` | `last_seqval` on later runs, `None` on the first run |
| `to_lsn` | Current `fn_cdc_get_max_lsn()` |
| `is_first_run` | `True` when no `last_seqval` was supplied |
| `has_changes` | `True` when `from_lsn <= to_lsn` |

`fetch_changes` returns up to `batch_size` rows ordered by `__$start_lsn`, `__$seqval`, filtered to rows after `after_seqval`:

| Key | Meaning |
|-----|---------|
| `capture_name`, `from_lsn`, `to_lsn`, `after_seqval` | The inputs used |
| `row_count` | Rows returned |
| `should_persist` | `True` when at least one row was returned |
| `rows` | Change rows — binary as `0x…` hex, dates as ISO strings, decimals as strings |
| `last_start_lsn`, `last_seqval` | Watermark of the last row (only present when rows were returned) |

Identifiers (`database_name`, `schema_name`, `table_name`, `capture_instance`) may contain only letters, numbers, underscores, and dots.

---

## common

### dates.py — `Dates`

```python
from python_toolkit.common.dates import Dates

now = Dates.utc_now()                                   # timezone-aware UTC datetime
parsed = Dates.parse_datetime("2026-09-21 10:30:00")    # parsed as UTC
custom = Dates.parse_datetime("21/09/2026", fmt="%d/%m/%Y")
```

| Method | Notes |
|--------|-------|
| `utc_now()` | Current time in UTC |
| `parse_datetime(value, *, fmt="%Y-%m-%d %H:%M:%S")` | Accepts a string or `datetime`; naive values are treated as UTC |

### json.py — `JsonTools`

```python
from python_toolkit.common.json import JsonTools

config = JsonTools.validate_object(
    '{"capture_name": "dbo_Orders", "database_name": "MyDb"}',
    name="config",
    required_keys=["capture_name", "database_name"],
)
```

| Method | Notes |
|--------|-------|
| `validate_object(value, *, name="config", required_keys=None)` | Parses a JSON string, checks it is an object with the required keys. Raises `JsonValidationError` (a `ValueError`) |
| `validate_object_or_fail(value, *, name="config", required_keys=None)` | Same, but exits the process with a one-line error instead of a traceback — use in Kestra scripts |

### logging.py — `ToolkitLogger`

```python
from python_toolkit.common.logging import ToolkitLogger

logger = ToolkitLogger.get_logger("MyTask")
logger.info("This is a log message")
# 2026-09-26 10:30:00,000 | INFO | MyTask | This is a log message
```

| Method | Notes |
|--------|-------|
| `get_logger(name, *, level=logging.INFO, fmt=None)` | Logs to stdout. Configured once per name — later calls return the same logger |

### validation.py — `Validation`

```python
from python_toolkit.common.validation import Validation

name = Validation.require_non_empty("  example  ", "name")   # returns "example"
value = Validation.require_not_none(some_value, "some_value")
```

| Method | Notes |
|--------|-------|
| `require_not_none(value, name)` | Raises `ValueError` if `None`, otherwise returns the value |
| `require_non_empty(value, name)` | Raises `ValueError` if empty or whitespace, otherwise returns the stripped string |

---

## parquet

### writer.py — `ParquetWriter`

```python
from python_toolkit.parquet.writer import ParquetWriter

rows = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
path = ParquetWriter.write(rows, "output/example.parquet")

# Stream several batches into one file (schema is taken from the first non-empty batch).
batches = ([{"id": i, "name": f"row-{i}"} for i in range(n, n + 500)] for n in range(0, 2000, 500))
path = ParquetWriter.write_batches(batches, "output/large.parquet", compression="zstd")
```

| Method | Notes |
|--------|-------|
| `write(rows, path, *, compression="snappy")` | Returns the output `Path`. Raises `ValueError` if `rows` is empty |
| `write_batches(batches, path, *, compression="snappy")` | Skips empty batches. Raises `ValueError` if every batch is empty |

The output directory must already exist.

---

## sqlserver

### sqlserver.py — `SqlServer`

General SQL Server helpers using `pyodbc` (requires an installed ODBC driver). For CDC, use `MssqlCdc` instead.

```python
from python_toolkit.sqlserver.sqlserver import SqlServer

conn_str = SqlServer.connection_string(
    server="<host>",
    database="MyDb",
    username="<user>",      # omit username/password for Windows auth
    password="<password>",
)

with SqlServer.connect(conn_str) as conn:
    rows = SqlServer.fetch_all(conn, "SELECT Id, Name FROM dbo.Items WHERE Status = ?", ["Active"])

    for batch in SqlServer.fetch_in_batches(conn, "SELECT * FROM dbo.BigTable", batch_size=1000):
        print(len(batch))

    SqlServer.execute(conn, "UPDATE dbo.Items SET Status = ? WHERE Id = ?", ["Done", 42])
    conn.commit()

# Build an upsert statement.
sql = SqlServer.build_merge_sql(
    target_table="dbo.TargetTable",
    source_table="dbo.SourceTable",
    key_columns=["Id"],
    update_columns=["Name", "Status"],
    insert_columns=["Id", "Name", "Status"],
)

# Split any iterable into fixed-size lists.
for batch in SqlServer.iter_batches(range(10), 4):
    print(batch)   # [0, 1, 2, 3], [4, 5, 6, 7], [8, 9]
```

| Method | Notes |
|--------|-------|
| `connection_string(*, server, database, username=None, password=None, driver="ODBC Driver 18 for SQL Server", trust_server_certificate=True, extra=None)` | Uses `Trusted_Connection=yes` when no username/password is given |
| `connect(conn_str, **kwargs)` | Context manager; closes the connection on exit. Does not commit |
| `execute(connection_or_cursor, sql, params=None)` | Runs a statement |
| `fetch_all(connection_or_cursor, sql, params=None)` | Returns rows as a list of dicts |
| `fetch_in_batches(connection_or_cursor, sql, *, batch_size=1000, params=None)` | Yields lists of dicts |
| `iter_batches(items, batch_size)` | Yields lists of up to `batch_size` items |
| `build_merge_sql(*, target_table, source_table, key_columns, update_columns, insert_columns=None)` | Returns a T-SQL `MERGE` string; `insert_columns` defaults to `update_columns` |

Methods that take `connection_or_cursor` accept either; a cursor they create themselves is closed afterwards.

---

## storage

### object_store.py — `LocalObjectStore`

A filesystem-backed object store. `ObjectStore` is the abstract base for other backends.

```python
from python_toolkit.storage.object_store import LocalObjectStore

store = LocalObjectStore("./tmp_store")                  # creates the folder if needed
stored_path = store.put("reports/demo.txt", "./data/demo.txt")
print(store.exists("reports/demo.txt"))                   # True
local_copy = store.get("reports/demo.txt", "./downloads/demo.txt")
```

| Method | Notes |
|--------|-------|
| `put(key, local_path)` | Copies a local file into the store; returns the stored path as a string |
| `get(key, local_path)` | Copies a stored file out; returns the destination `Path`. Raises `FileNotFoundError` if missing |
| `exists(key)` | `True` if the key exists |

Keys that resolve outside the store folder (e.g. `../x`) raise `ValueError`.

---

## Classes by module

| Module | Class | Extra |
|--------|-------|-------|
| `python_toolkit.api.client` | `ApiClient` | `api` |
| `python_toolkit.api.dynamics_crm` | `DynamicsCrmClient` | `api` |
| `python_toolkit.cdc.mssql` | `MssqlCdc` | `cdc` |
| `python_toolkit.common.dates` | `Dates` | — |
| `python_toolkit.common.json` | `JsonTools` | — |
| `python_toolkit.common.logging` | `ToolkitLogger` | — |
| `python_toolkit.common.validation` | `Validation` | — |
| `python_toolkit.parquet.writer` | `ParquetWriter` | `parquet` |
| `python_toolkit.sqlserver.sqlserver` | `SqlServer` | `sqlserver` |
| `python_toolkit.storage.object_store` | `LocalObjectStore` | — |
