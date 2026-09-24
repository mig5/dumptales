"""Row readers for the bounded-memory comparison engines."""
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from dumptales import DumpError, key_for, open_dump, pg_ident, pg_unescape, pg_statement_lines, split_top


def postgres_schema(path):
    """pg_dump normally declares keys after COPY; read metadata before rows."""
    schema = {}
    copying = False
    with open_dump(path) as fh:
        for line in pg_statement_lines(fh):
            if copying:
                if line.rstrip('\r\n') == r'\.':
                    copying = False
                continue
            if re.match(r'^COPY\s+(.+?)\s*\((.*?)\)\s+FROM stdin;', line, re.I):
                copying = True
                continue
            match = re.match(r'^CREATE TABLE\s+(.+?)\s*\(', line, re.I)
            if match:
                name = pg_ident(match[1])
                if name in schema:
                    raise DumpError(f'{path}: duplicate CREATE TABLE {name}')
                columns = []
                for definition in fh:
                    if definition.strip().startswith(')'):
                        break
                    field = re.match(r'^\s*("(?:[^"]|"")*"|[A-Za-z_][A-Za-z_0-9]*)\s+', definition)
                    if field and field[1].upper() not in ('CONSTRAINT', 'PRIMARY', 'FOREIGN', 'UNIQUE', 'CHECK'):
                        columns.append(pg_ident(field[1]))
                else:
                    raise DumpError(f'{path}: unterminated CREATE TABLE {name}')
                schema[name] = dict(columns=columns, pk=[], fks=[])
                continue
            match = re.match(r'^ALTER TABLE(?: ONLY)?\s+(.+?)\s+ADD CONSTRAINT\s+.+?\s+PRIMARY KEY\s*\((.*?)\);', line, re.I)
            if match and pg_ident(match[1]) in schema:
                schema[pg_ident(match[1])]['pk'] = [pg_ident(c) for c in split_top(match[2])]
            match = re.match(r'^ALTER TABLE(?: ONLY)?\s+(.+?)\s+ADD CONSTRAINT\s+.+?\s+FOREIGN KEY\s*\((.*?)\)\s+REFERENCES\s+(.+?)\s*\((.*?)\)(.*);', line, re.I)
            if match and pg_ident(match[1]) in schema:
                action = re.search(r'ON DELETE\s+(CASCADE|SET NULL|RESTRICT|NO ACTION)', match[5], re.I)
                name = pg_ident(match[1])
                schema[name]['fks'].append(dict(child=name, child_columns=[pg_ident(c) for c in split_top(match[2])], parent=pg_ident(match[3]), parent_columns=[pg_ident(c) for c in split_top(match[4])], on_delete=action[1].upper() if action else 'RESTRICT'))
        if copying:
            raise DumpError(f'{path}: unterminated COPY block')
    return schema


def postgres_rows(path, schema, counts, skip=frozenset()):
    schema.update(postgres_schema(path))
    copying = None
    with open_dump(path) as fh:
        for lineno, line in enumerate(fh, 1):
            if copying is not None:
                if line.rstrip('\r\n') == r'\.':
                    copying = None
                    continue
                name, columns = copying
                if name not in schema:
                    raise DumpError(f'{path}:{lineno}: COPY table lacks schema: {name}')
                values = [pg_unescape(v) for v in line.rstrip('\r\n').split('\t')]
                if len(values) != len(columns):
                    raise DumpError(f'{path}:{lineno}: COPY column count mismatch')
                spec = schema[name]
                if not spec['pk']:
                    counts[f'skipped:{name}'] += 1
                    continue
                row = dict.fromkeys(spec['columns'])
                row.update(zip(columns, values))
                if name not in skip:
                    counts[name] += 1
                    yield name, key_for(row, spec['pk'], name), row
                continue
            match = re.match(r'^COPY\s+(.+?)\s*\((.*?)\)\s+FROM stdin;', line, re.I)
            if match:
                copying = (pg_ident(match[1]), [pg_ident(c) for c in split_top(match[2])])
        if copying is not None:
            raise DumpError(f'{path}: unterminated COPY block')


def sqlite_schema(source):
    schema = {}
    for (name,) in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        quoted = '"' + name.replace('"', '""') + '"'
        columns_info = list(source.execute(f'PRAGMA table_info({quoted})'))
        columns = [c[1] for c in columns_info]
        pk = [c[1] for c in sorted(columns_info, key=lambda c: c[5]) if c[5]]
        grouped = defaultdict(list)
        for fk in source.execute(f'PRAGMA foreign_key_list({quoted})'):
            grouped[fk[0]].append(fk)
        fks = []
        for group in grouped.values():
            group.sort(key=lambda entry: entry[1])
            fks.append(dict(child=name, child_columns=[entry[3] for entry in group], parent=group[0][2], parent_columns=[entry[4] for entry in group], on_delete=group[0][6].upper()))
        schema[name] = dict(columns=columns, pk=pk, fks=fks)
    return schema


def sqlite_rows(path, schema, counts, skip=frozenset()):
    uri = f'file:{Path(path).resolve()}?mode=ro'
    with sqlite3.connect(uri, uri=True) as source:
        schema.update(sqlite_schema(source))
        for name in sorted(schema):
            quoted = '"' + name.replace('"', '""') + '"'
            spec = schema[name]
            if not spec['pk']:
                counts[f'skipped:{name}'] += source.execute(f'SELECT COUNT(*) FROM {quoted}').fetchone()[0]
                continue
            if name in skip:
                continue
            order = ', '.join('"' + col.replace('"', '""') + '"' for col in spec['pk'])
            for values in source.execute(f'SELECT * FROM {quoted} ORDER BY {order}'):
                row = {column: (value.hex() if isinstance(value, bytes) else value) for column, value in zip(spec['columns'], values)}
                counts[name] += 1
                yield name, key_for(row, spec['pk'], name), row
