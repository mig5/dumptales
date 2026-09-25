# DumpTales

<img src="icon.png" alt="DumpTales duck and database icon" width="200">

Read-only, offline comparison of two database snapshots. By default it attempts a streaming merge by primary key; if input order prevents that, it compares compressed hash partitions with bounded memory.

PostgreSQL `COPY` dumps are read twice because primary keys are often declared after the data.

SQLite reads rows from the source database in primary-key order. An older temporary SQLite index method (slower) remains available via `--engine sqlite`.

Temporary files are removed after the command finishes.

**Dumptales never executes SQL from a dump or contacts a database server**.

## Use

Requires Python 3.10 or newer. From this directory:

```sh
python dumptales.py old.sql new.sql --dialect mysql  # auto: stream, then partition if unordered
python dumptales.py forgejo1.sql forgejo2.sql         # detects PostgreSQL pg_dump
python dumptales.py old.sql.gz new.sql.gz --dialect mysql --format json > changes.json
python dumptales.py old.sql.bz2 new.sql.bz2          # bzip2; single-file ZIP also works
python dumptales.py old.dump new.dump --dialect postgres --format jsonl > changes.jsonl
python dumptales.py old.sqlite new.sqlite --dialect sqlite-db --color always
python dumptales.py snapshot old.sql snapshots/old
python dumptales.py snapshot new.sql snapshots/new
python dumptales.py snapshots/old snapshots/new --format json
```

You can also install locally with `python -m pip install .` and run `dumptales`; after publication, use `pipx install dumptales` or `python -m pip install dumptales`.

Formats: `human` (default), `json`, and streaming `jsonl`.

Colour defaults to terminals only. `--limit N` limits detailed human output while preserving change counts. `--ignore-column NAME` excludes a column from the value comparison. `--workdir DIR` selects a location with sufficient free disk space for temporary data.

`--dialect auto` is the default. It inspects both inputs and rejects unknown or mismatched formats; use an explicit `--dialect` for supported SQL that it cannot identify. Plain, gzip, bzip2 and ZIP archives containing exactly one SQL dump are read without extraction. Compression is detected from file contents, so filenames need no particular suffix. Native SQLite database files must remain uncompressed. PostgreSQL dumps should be plain-text `pg_dump` output (the default format), not PostgreSQL custom-format archives (`pg_dump -Fc`). ZIP means a conventional ZIP archive with one SQL file, not the custom `pg_dump` format.

## Inputs and scope

- MySQL/MariaDB: conventional plain `mysqldump` with `CREATE TABLE` followed by `INSERT ... VALUES`, including multiple rows per statement and gzip files. Rejects unsupported value expressions, inserts before schema, and duplicate keys. Schema output compares parsed columns, primary keys and foreign keys; it does not compare indexes, triggers, views, collations or other DDL.
- PostgreSQL: plain `pg_dump` with table definitions, `COPY ... FROM stdin`, and `ALTER TABLE ONLY ... ADD CONSTRAINT` primary/foreign keys. This reader is experimental; `INSERT`, multi-line constraints, quoted identifiers containing dots and partition-specific dump forms are not supported. It currently skips most other SQL. **Do not use it to assert equality for unfamiliar pg_dump output.** Compressed files ending `.gz` work. Its schema pass and row pass both scan the dump, and unordered rows require an additional partition pass.
- SQLite: native `.sqlite`/`.db` files read in read-only mode. The schema representation compares columns, primary keys and foreign keys; it does not compare indexes, triggers or views. SQL text dumps are not supported yet.
- Rows without declared primary keys are counted as skipped. Key changes show as removal and addition. Ignored columns are ignored for modified rows only. SQLite BLOBs are rendered as hex; PostgreSQL `bytea` decoding and cross-dialect comparison are outside this prototype.
- If no primary keys are detected anywhere, the comparison fails instead of reporting a misleading zero-change result. Check the skipped-table list on other comparisons too: those tables' rows have not been compared.

Relationship annotations use foreign keys declared in the *old* schema. If a removed child refers to a removed parent and the FK is `ON DELETE CASCADE`, it says "consistent with foreign-key cascade". Likewise, `SET NULL` annotations link a changed child to a removed parent. Two snapshots cannot establish which SQL operation caused the change.

**Resource use:** streaming keeps only current rows in memory and writes only changes to a temporary JSONL file.

The `partition` path writes compressed old and new row partitions to temporary disk and processes each under an approximate memory budget.

SQLite source files may use temporary storage internally to satisfy `ORDER BY`, depending on their primary-key indexes and query planner.

One MySQL SQL statement may occupy up to 128 MiB of RAM.

Extended inserts can be large; use `mysqldump --skip-extended-insert` to bound them.

Disk usage can still exceed one uncompressed dump, especially with many changes or wide keys.

No full new-row copy is retained for unchanged rows.

SQLite source files are read without modifying them; use consistent snapshots, not live files changing during comparison.

JSON output is streamed, but errors partway through may leave partial output: check exit status before consuming it.

**This is an early prototype for controlled dumps**, not a general SQL parser or a tool for generating migrations. **Please validate on representative dumps before relying on its findings.**

## Test

```sh
python -m unittest discover -s tests -v
```

## MySQL comparison engines

- `--engine auto` (default): try a streaming merge when each dump is strictly ordered by table and primary key. If either is out of order, restart using hash partitions. This rereads both dumps on fallback; for known unordered dumps choose `--engine partition` directly.
- `--engine stream`: require compatible ordering. Uses no row index; detected disorder returns exit 2.
- `--engine partition`: hash rows into 256 gzip-compressed temporary partitions; compare one pair at a time. Oversize partitions split recursively. `--memory-limit 64` is an approximate per-partition MiB budget, not a strict process RSS cap. Temporary space depends on compressibility and how many rows change.
- `--engine sqlite`: retain the previous comparison backend for benchmarking or when partitioning is slower.

The streaming path requires dump **row order** to be compatible, not merely matching sets of rows. It checks ascending table name and primary key order and rejects duplicate keys. The fallback uses a stable hash so corresponding rows end up in the same partition. Every row is compared exactly, rather than trusting a hash of its contents.

MySQL relationship annotations are computed from recorded parent removals and may be omitted if more than 100,000 referenced parent rows are removed; `summary.relationships_complete` reports this. The temporary change event file can grow with the amount of change. SQL statements may still consume substantial memory with extended inserts. If the dumps differ in schema or lack primary keys, the existing scope limitations apply.

Synthetic measurements on a test environment:

| Pair | Engine | Time | Peak temporary files |
| --- | --- | ---: | ---: |
| Identical ordered 40 MB dumps, 100,000 rows | stream | 13.6 s | <0.1 MB |
| Identical unordered 10 MB dumps, 25,000 rows | partition | 7.3 s | 4.3 MB |
| Same unordered pair | SQLite backend | 3.6 s | 12.5 MB |

This is a space/time tradeoff, and the partition path was slower than SQLite on that sample. The MySQL parser itself still does substantial Python work. Measure both engines on representative dumps before choosing one.

## partition file handles

The partition writer keeps up to 256 gzip buckets open, subject to the process file-descriptor limit.

## fast skip, canonical snapshots, optional C scanner

**Identical table sections:** MySQL dumps get a quick pass over conventional one-line INSERT records and CREATE TABLE definitions. For each table whose definition and ordered INSERT text have matching SHA-256 digests, row parsing is skipped. This is a content fingerprint shortcut: hash collisions are computationally negligible, but the skipped SQL values are not validated. Use `--no-fast-skip` to parse all rows, including when complete old/new row totals are needed. The JSON summary lists `unchanged_tables_skipped`; `old_rows`/`new_rows` are `null` if any table was skipped. Nonconforming insert forms disable the shortcut. A local identical 40 MB dump pair took 0.26 seconds with the shortcut and 12.94 seconds with pure Python full parsing.

**Canonical snapshots:** `dumptales snapshot dump.sql SNAPSHOT_DIR` parses the dump once and creates a directory of sorted, gzip-compressed table data plus a manifest. Creating a snapshot requires GNU `sort` and temporary disk space; it refuses an existing destination. A subsequent `dumptales SNAPSHOT_OLD SNAPSHOT_NEW` verifies each compressed file against the manifest, then skips tables with identical canonical SHA-256 digests and compares changed tables by streaming sorted rows. Snapshots are currently MySQL only. They require a declared primary key on every table; keyless tables cause an error. Keep the original SQL dump as your backup: this snapshot format is an experimental comparison cache, not a restore format.

`poetry build` compiles the C scanner through `native_build.py` using setuptools and requires a C compiler and Python development headers. The runtime can use the Python parser when a manually created install lacks the module. The summary reports `parser=native` or `parser=python`.

The quick pass and snapshots trade a tiny cryptographic collision probability for skipping full row comparisons of fingerprint-equal sections. For high-assurance equality checks, use `--no-fast-skip` and retain the source dumps. Even with the native scanner, actual results will depend on row widths, SQL escaping, dump order, and number of changed tables.
