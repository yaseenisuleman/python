# python-toolkit

Shared Python helpers for Kestra tasks — SQL Server, CDC, Parquet, storage, and API clients.

Install name: `python-toolkit` · Import name: `python_toolkit`

## Repository layout

```text
PyToolkitPlugin/
├── pyproject.toml         # package metadata, dependencies, extras
├── src/
│   └── python_toolkit/
│       ├── __init__.py    # __version__
│       ├── api/
│       ├── cdc/
│       ├── common/
│       ├── parquet/
│       ├── sqlserver/
│       └── storage/
├── METHODS_USAGE.md
└── README.md
```

## Installation

Core helpers have no third-party dependencies. Pick extras for the stacks you use:

| Extra | Installs | Needed for |
|-------|----------|------------|
| `api` | `requests` | `api.client`, `api.dynamics_crm` |
| `cdc` | `pymssql` | `cdc.mssql` |
| `parquet` | `pyarrow` | `parquet.writer` |
| `sqlserver` | `pyodbc` | `sqlserver.sqlserver` (`connect`) |
| `all` | all of the above | — |

From a git tag:

```bash
pip install "python-toolkit[cdc] @ git+https://github.com/<owner>/<repo>.git@v0.1.0"
```

For local development:

```bash
pip install -e ".[all]"
```

## Return values

Methods that return data must return a **`dict`** (typically `dict[str, Any]`). This keeps results JSON-serializable for Kestra task outputs and consistent across the toolkit.

## Usage in Kestra

Add the package to the Script task's `dependencies`, then import from `python_toolkit`:

```yaml
dependencies:
  - kestra
  - "python-toolkit[cdc] @ git+https://github.com/<owner>/<repo>.git@v0.1.0"

script: |
  from kestra import Kestra
  from python_toolkit.common.json import JsonTools
  from python_toolkit.cdc.mssql import MssqlCdc

  config = JsonTools.validate_object_or_fail(
      '''{{ outputs.read_capture_config_details.value }}''',
      required_keys=["capture_name"],
  )
```

Pass one generic SQL connection string from a Kestra env, for example
`{{ envs.sql_conn_master }}`, and provide the target database to each CDC method.

### MSSQL CDC in Kestra — Python (`pymssql`)

`MssqlCdc` uses **pymssql** (pure Python, no ODBC driver), installed by the `cdc` extra.

Use one generic connection string, typically pointing to `master`, and pass the target
database plus either `capture_instance` or `schema_name` + `table_name` to each CDC method.

The watermark is two values — `last_start_lsn` and `last_seqval` (20-char hex, no `0x`) —
taken from the previous `fetch_changes` result. Leave both empty on the first run.

```yaml
script: |
  from kestra import Kestra
  from python_toolkit.cdc.mssql import MssqlCdc

  result = MssqlCdc.get_next_lsn(
      "{{ envs.sql_conn_master }}",
      database_name="{{ outputs.check_config_details.vars.database_name }}",
      capture_instance="{{ outputs.check_config_details.vars.capture_name }}",
      last_start_lsn="{{ outputs.check_config_details.vars.last_start_lsn | default('') }}",
      last_seqval="{{ outputs.check_config_details.vars.last_seqval | default('') }}",
  )
  Kestra.outputs(result)
```

Empty strings and `"null"` are treated as "no watermark". See
[METHODS_USAGE.md](METHODS_USAGE.md#cdc) for the full poll → fetch → persist flow.

### MSSQL CDC in Kestra — JDBC (alternative)

For SQL-only steps with no Python, use the JDBC plugin
(`io.kestra.plugin.jdbc.sqlserver.Query`). It includes the Microsoft JDBC driver.

> This query tracks a single `last_lsn` (as `0x…` hex) and does not batch by `__$seqval`,
> so its watermark is not interchangeable with the Python `MssqlCdc` watermark.

```yaml
- id: get_next_lsn
  type: io.kestra.plugin.jdbc.sqlserver.Query
  url: "{{ envs.sql_jdbc_url_db4 }}"
  username: "{{ envs.sql_user_db4 }}"
  password: "{{ envs.sql_password_db4 }}"
  fetchType: FETCH
  sql: |
    DECLARE @capture_instance nvarchar(128) = '{{ outputs.check_config_details.vars.capture_name }}';
    {% if outputs.check_config_details.vars.last_lsn is defined and outputs.check_config_details.vars.last_lsn != null and outputs.check_config_details.vars.last_lsn != "" %}
    DECLARE @last_lsn binary(10) = CONVERT(binary(10), '{{ outputs.check_config_details.vars.last_lsn }}', 1);
    {% else %}
    DECLARE @last_lsn binary(10) = NULL;
    {% endif %}
    DECLARE @min_lsn binary(10) = sys.fn_cdc_get_min_lsn(@capture_instance);
    DECLARE @max_lsn binary(10) = sys.fn_cdc_get_max_lsn();
    DECLARE @from_lsn binary(10) = CASE
      WHEN @last_lsn IS NULL THEN @min_lsn
      ELSE sys.fn_cdc_increment_lsn(@last_lsn)
    END;

    SELECT
      @capture_instance AS capture_instance,
      CONVERT(varchar(50), @last_lsn, 1) AS last_lsn,
      CONVERT(varchar(50), @from_lsn, 1) AS from_lsn,
      CONVERT(varchar(50), @max_lsn, 1) AS to_lsn,
      CASE WHEN @last_lsn IS NULL THEN CAST(1 AS bit) ELSE CAST(0 AS bit) END AS is_first_run,
      CASE WHEN @from_lsn < @max_lsn THEN CAST(1 AS bit) ELSE CAST(0 AS bit) END AS has_changes;
```

JDBC URL example:

```text
jdbc:sqlserver://<host>:1433;databaseName=<database>;encrypt=true;trustServerCertificate=true
```

Read results from the task output: `outputs.get_next_lsn.rows[0].from_lsn`, etc.

## Classes by module

| Module | Class | Extra |
|--------|-------|-------|
| `python_toolkit.common.json` | `JsonTools` | — |
| `python_toolkit.common.validation` | `Validation` | — |
| `python_toolkit.common.logging` | `ToolkitLogger` | — |
| `python_toolkit.common.dates` | `Dates` | — |
| `python_toolkit.cdc.mssql` | `MssqlCdc` | `cdc` |
| `python_toolkit.sqlserver.sqlserver` | `SqlServer` | `sqlserver` |
| `python_toolkit.api.client` | `ApiClient` | `api` |
| `python_toolkit.api.dynamics_crm` | `DynamicsCrmClient` | `api` |
| `python_toolkit.parquet.writer` | `ParquetWriter` | `parquet` |
| `python_toolkit.storage.object_store` | `LocalObjectStore` | — |

See [METHODS_USAGE.md](METHODS_USAGE.md) for method-level examples.

## Releasing a new version

1. Bump `__version__` in `src/python_toolkit/__init__.py`
2. Commit, tag (e.g. `v0.2.0`), push
3. Update the `@v0.x.y` ref in Kestra task dependencies

## Notes

- Keep connection strings, client secrets, and tenant IDs in Kestra secrets or env vars.
- `__pycache__/` is gitignored; use `PYTHONDONTWRITEBYTECODE=1` in tasks.
