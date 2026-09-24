#!/usr/bin/env python3
"""Offline, disk-backed, read-only comparison of conventional MySQL dumps."""
import argparse
import gzip
import json
import re
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

VERSION = '0.1.1'
IDENT = r'`(?:``|[^`])*`'
IDENT_RE = re.compile(IDENT)
CREATE_RE = re.compile(r'^CREATE TABLE(?: IF NOT EXISTS)?\s+(`(?:``|[^`])*`)(?:\s*\.\s*(`(?:``|[^`])*`))?\s*\(', re.I | re.S)
INSERT_RE = re.compile(r'^(INSERT|REPLACE)\s+(?:IGNORE\s+)?INTO\s+(`(?:``|[^`])*`)(?:\s*\.\s*(`(?:``|[^`])*`))?\s*', re.I | re.S)
FK_RE = re.compile(r'FOREIGN KEY\s*\((.*?)\)\s*REFERENCES\s+(?:`(?:``|[^`])*`\s*\.\s*)?(`(?:``|[^`])*`)\s*\((.*?)\)(.*)', re.I | re.S)

try:
    from _dumptales_native import next_row as _native_next_row
except ImportError:
    _native_next_row = None

class DumpError(Exception):
    pass

def ident(s):
    return s[1:-1].replace('``', '`')

def open_dump(path):
    return gzip.open(path, 'rt', encoding='utf-8', errors='strict', newline='') if str(path).endswith('.gz') else open(path, encoding='utf-8', newline='')

def statements(path):
    """Stream SQL statements and PostgreSQL COPY blocks with bounded statement memory."""
    with open_dump(path) as fh:
        def chars():
            for chunk in fh:
                if chunk.startswith('INSERT INTO `') and chunk.rstrip('\r\n').endswith(');'):
                    yield ('fast_insert', chunk)
                else:
                    yield from chunk
        source = iter(chars()); pending = None
        def get():
            nonlocal pending
            if pending is not None:
                value = pending; pending = None; return value
            return next(source, None)
        def put(ch):
            nonlocal pending
            pending = ch
        state = 'normal'; buf = []; line = 1; start = 1
        while (ch := get()) is not None:
            if isinstance(ch, tuple):
                if state != 'normal' or ''.join(buf).strip():
                    raise DumpError(f'{path}:{line}: unexpected INSERT within SQL statement')
                buf.clear()
                statement = ch[1].rstrip('\r\n')[:-1]
                if len(statement) > 128 * 1024 * 1024: raise DumpError(f'{path}:{line}: SQL statement exceeds 128 MiB')
                yield line, statement
                line += ch[1].count('\n'); start = line
                continue
            if ch == '\n': line += 1
            if state == 'line':
                if ch == '\n': state = 'normal'; buf.append(' ')
                continue
            if state == 'block':
                if ch == '*' and (nxt := get()) == '/': state = 'normal'; buf.append(' ')
                else:
                    if ch == '*' and nxt is not None: put(nxt)
                continue
            if state == 'normal':
                if ch == '#': state = 'line'; continue
                if ch == '-':
                    nxt = get()
                    if nxt == '-': state = 'line'; continue
                    if nxt is not None: put(nxt)
                if ch == '/':
                    nxt = get()
                    if nxt == '*': state = 'block'; continue
                    if nxt is not None: put(nxt)
                if ch in ("'", '"', '`'): state = ch
                if ch == ';':
                    s = ''.join(buf).strip(); buf.clear(); start = line
                    if s: yield start, s
                    continue
            else:
                if ch == '\\':
                    buf.append(ch); escaped = get()
                    if escaped is None: raise DumpError(f'{path}:{line}: unterminated escape')
                    buf.append(escaped)
                    if escaped == '\n': line += 1
                    continue
                if ch == state:
                    nxt = get()
                    if nxt == state: buf.extend((ch, nxt)); continue
                    if nxt is not None: put(nxt)
                    state = 'normal'
            buf.append(ch)
            if len(buf) > 128 * 1024 * 1024:
                raise DumpError(f'{path}:{start}: SQL statement exceeds 128 MiB; use --skip-extended-insert when dumping')
        if state not in ('normal', 'line'): raise DumpError(f'{path}:{line}: unterminated SQL string or comment')
        if ''.join(buf).strip(): raise DumpError(f'{path}:{start}: unterminated SQL statement')

def split_top(s, separator=','):
    out = []; begin = 0; quote = None; depth = 0; i = 0
    while i < len(s):
        ch = s[i]
        if quote:
            if ch == '\\': i += 2; continue
            if ch == quote:
                if i + 1 < len(s) and s[i+1] == quote: i += 2; continue
                quote = None
        elif ch in ("'", '"', '`'): quote = ch
        elif ch == '(': depth += 1
        elif ch == ')': depth -= 1
        elif ch == separator and depth == 0:
            out.append(s[begin:i].strip()); begin = i+1
        i += 1
    out.append(s[begin:].strip())
    return out

def parens(s, at):
    if at >= len(s) or s[at] != '(': raise DumpError('expected opening parenthesis')
    quote = None; depth = 0; i = at
    while i < len(s):
        ch = s[i]
        if quote:
            if ch == '\\': i += 2; continue
            if ch == quote:
                if i+1 < len(s) and s[i+1] == quote: i += 2; continue
                quote = None
        elif ch in ("'", '"', '`'): quote = ch
        elif ch == '(': depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0: return s[at+1:i], i+1
        i += 1
    raise DumpError('unbalanced parentheses')

def literal(v):
    v = v.strip()
    if v.upper() == 'NULL': return None
    if len(v) >= 2 and v[0] in "'\"" and v[-1] == v[0]:
        s = v[1:-1].replace(v[0]*2, v[0])
        if '\\' not in s: return s
        out = []; i = 0
        escapes = {'0':'\0','n':'\n','r':'\r','t':'\t','b':'\b','Z':'\x1a'}
        while i < len(s):
            if s[i] == '\\' and i+1 < len(s):
                out.append(escapes.get(s[i+1], s[i+1])); i += 2
            else: out.append(s[i]); i += 1
        return ''.join(out)
    if re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', v): return v
    if re.fullmatch(r"(?:X'[0-9A-Fa-f]*'|0x[0-9A-Fa-f]+|b'[01]+')", v, re.I): return v.lower()
    raise DumpError(f'unsupported SQL value {v[:60]!r}')

def parse_create(s):
    m = CREATE_RE.match(s)
    if not m: raise DumpError('unsupported CREATE TABLE syntax')
    name = ident(m.group(2) or m.group(1)); body, _ = parens(s, m.end()-1)
    columns = []; pk = []; fks = []
    for part in split_top(body):
        part = part.strip()
        if part.startswith('`'):
            col = IDENT_RE.match(part)
            if col: columns.append(ident(col.group()))
        key = re.search(r'\bPRIMARY KEY\s*\((.*?)\)', part, re.I | re.S)
        if key: pk = [ident(x.group()) for x in IDENT_RE.finditer(key.group(1))]
        fk = FK_RE.search(part)
        if fk:
            local = [ident(x.group()) for x in IDENT_RE.finditer(fk.group(1))]
            remote = [ident(x.group()) for x in IDENT_RE.finditer(fk.group(3))]
            action = re.search(r'\bON DELETE\s+(CASCADE|SET\s+NULL|RESTRICT|NO ACTION|SET DEFAULT)\b', fk.group(4), re.I)
            if len(local) != len(remote): raise DumpError(f'{name}: malformed foreign key')
            fks.append(dict(child=name, child_columns=local, parent=ident(fk.group(2)), parent_columns=remote, on_delete=action.group(1).upper().replace('  ',' ') if action else 'RESTRICT'))
    if len(columns) != len(set(columns)): raise DumpError(f'{name}: duplicate column')
    return name, dict(columns=columns, pk=pk, fks=fks)

def parse_insert(s, schemas):
    m = INSERT_RE.match(s)
    if not m: raise DumpError('unsupported INSERT syntax')
    if m.group(1).upper() != 'INSERT' or re.search(r'\bIGNORE\b', s[:m.end()], re.I): raise DumpError('REPLACE and INSERT IGNORE are unsupported')
    name = ident(m.group(3) or m.group(2)); pos = m.end(); cols = None
    if s[pos:pos+1] == '(':
        raw, pos = parens(s, pos); cols = [ident(x.strip()) for x in split_top(raw)]
    vm = re.match(r'\s*VALUES\s*', s[pos:], re.I)
    if not vm: raise DumpError(f'{name}: only INSERT ... VALUES is supported')
    pos += vm.end()
    if name not in schemas: raise DumpError(f'{name}: INSERT before CREATE TABLE (or missing schema)')
    schema = schemas[name]
    cols = cols or schema['columns']
    if len(cols) != len(set(cols)) or set(cols) - set(schema['columns']): raise DumpError(f'{name}: invalid INSERT column list')
    while pos < len(s):
        if _native_next_row is None:
            raw, pos = parens(s, pos)
            raw_values = split_top(raw)
        else:
            try:
                raw_values, pos = _native_next_row(s, pos)
            except ValueError as exc:
                raise DumpError(str(exc)) from exc
        values = [literal(v) for v in raw_values]
        if len(values) != len(cols): raise DumpError(f'{name}: INSERT column/value count mismatch')
        row = dict.fromkeys(schema['columns']); row.update(zip(cols, values))
        yield name, row
        while pos < len(s) and s[pos].isspace(): pos += 1
        if pos == len(s): break
        if s[pos] != ',': raise DumpError(f'{name}: unsupported INSERT suffix {s[pos:pos+60]!r}')
        pos += 1
        while pos < len(s) and s[pos].isspace(): pos += 1

def key_for(row, columns, table):
    vals = [row.get(c) for c in columns]
    if any(v is None for v in vals): raise DumpError(f'{table}: NULL in primary key')
    return json.dumps(vals, ensure_ascii=False, separators=(',', ':'))

def store_row(db, side, name, key, row, columns):
    # Column names are stored once per table in the schema, not in every row.
    data = json.dumps([row.get(column) for column in columns], ensure_ascii=False, separators=(',', ':'))
    if side == 0:
        db.execute('INSERT INTO rows VALUES (?,?,?)', (name, key, data))
    else:
        db.execute('INSERT INTO new_keys VALUES (?,?)', (name, key))
        previous = db.execute('SELECT data FROM rows WHERE tbl=? AND key=?', (name, key)).fetchone()
        if previous is None or previous[0] != data:
            db.execute('INSERT INTO delta VALUES (?,?,?)', (name,key,data))

def ingest(path, db, side):
    schemas = {}; counts = Counter()
    for lineno, s in statements(path):
        upper = s[:70].lstrip().upper()
        try:
            if upper.startswith('CREATE TABLE'):
                name, schema = parse_create(s)
                if name in schemas: raise DumpError(f'{name}: duplicate CREATE TABLE')
                schemas[name] = schema
            elif upper.startswith(('INSERT ', 'REPLACE ')):
                for name, row in parse_insert(s, schemas):
                    pk = schemas[name]['pk']
                    if not pk:
                        counts[f'skipped:{name}'] += 1; continue
                    key = key_for(row, pk, name)
                    try: store_row(db, side, name, key, row, schemas[name]['columns'])
                    except sqlite3.IntegrityError: raise DumpError(f'{name}: duplicate primary key {key}')
                    counts[name] += 1
            elif upper.startswith(('UPDATE ', 'DELETE ', 'LOAD DATA', 'INSERT ', 'REPLACE ', 'ALTER TABLE', 'CREATE TABLE')):
                raise DumpError(f'unsupported data/schema statement: {s[:90]!r}')
        except DumpError as ex: raise DumpError(f'{path}:{lineno}: {ex}') from ex
    db.commit()
    return schemas, counts


def ingest_sqlite_file(path, db, side):
    source = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)
    schemas = {}; counts = Counter()
    try:
        for (name,) in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', name):
                quoted = '"' + name.replace('"', '""') + '"'
            else: quoted = '"' + name + '"'
            cols = list(source.execute(f'PRAGMA table_info({quoted})'))
            columns = [c[1] for c in cols]
            pk = [c[1] for c in sorted(cols, key=lambda c:c[5]) if c[5]]
            fks = []
            grouped = defaultdict(list)
            for row in source.execute(f'PRAGMA foreign_key_list({quoted})'): grouped[row[0]].append(row)
            for group in grouped.values():
                group.sort(key=lambda r:r[1]); r = group[0]
                fks.append(dict(child=name, child_columns=[v[3] for v in group], parent=r[2], parent_columns=[v[4] for v in group], on_delete=r[6].upper()))
            schemas[name] = dict(columns=columns, pk=pk, fks=fks)
            if not pk: counts[f'skipped:{name}'] += source.execute(f'SELECT COUNT(*) FROM {quoted}').fetchone()[0]; continue
            for values in source.execute(f'SELECT * FROM {quoted}'):
                row = {k:(v.hex() if isinstance(v, bytes) else v) for k,v in zip(columns, values)}
                key = key_for(row, pk, name)
                store_row(db, side, name, key, row, schemas[name]['columns'])
                counts[name] += 1
        db.commit()
        return schemas, counts
    finally: source.close()

def pg_ident(raw):
    return raw.strip().split('.')[-1].strip('"').replace('""','"')

def pg_unescape(v):
    if v == r'\N': return None
    return re.sub(r'\\(?:([0-7]{3})|(.))', lambda m: chr(int(m[1],8)) if m[1] else {'t':'\t','n':'\n','r':'\r','b':'\b','f':'\f','v':'\v'}.get(m[2], m[2]), v)

def ingest_postgres(path, db, side):
    schemas = {}; counts = Counter(); copy = None
    db.execute('CREATE TEMP TABLE pg_stage (tbl TEXT, data TEXT)')
    with open_dump(path) as fh:
        for lineno, line in enumerate(fh, 1):
            if copy:
                if line.rstrip('\r\n') == r'\.': copy = None; continue
                name, columns = copy; values = [pg_unescape(x) for x in line.rstrip('\r\n').split('\t')]
                if len(values) != len(columns): raise DumpError(f'{path}:{lineno}: COPY column count mismatch')
                if name not in schemas: raise DumpError(f'{path}:{lineno}: COPY table lacks schema')
                schema = schemas[name]; row = dict.fromkeys(schema['columns']); row.update(zip(columns, values))
                db.execute('INSERT INTO pg_stage VALUES (?,?)', (name, json.dumps(row,ensure_ascii=False,sort_keys=True)))
                continue
            m = re.match(r'^COPY\s+(.+?)\s*\((.*?)\)\s+FROM stdin;', line, re.I)
            if m:
                copy = (pg_ident(m[1]), [pg_ident(c) for c in split_top(m[2])]); continue
            m = re.match(r'^CREATE TABLE\s+(.+?)\s*\(', line, re.I)
            if m:
                name = pg_ident(m[1]); columns=[]
                for definition in fh:
                    lineno += 1
                    if definition.strip().startswith(')'): break
                    field = re.match(r'^\s*("(?:[^"]|"")*"|[A-Za-z_][A-Za-z_0-9]*)\s+', definition)
                    if field and field[1].upper() not in ('CONSTRAINT','PRIMARY','FOREIGN','UNIQUE','CHECK'): columns.append(pg_ident(field[1]))
                schemas[name] = dict(columns=columns,pk=[],fks=[])
                continue
            m = re.match(r'^ALTER TABLE(?: ONLY)?\s+(.+?)\s+ADD CONSTRAINT\s+.+?\s+PRIMARY KEY\s*\((.*?)\);', line, re.I)
            if m and pg_ident(m[1]) in schemas:
                schemas[pg_ident(m[1])]['pk'] = [pg_ident(c) for c in split_top(m[2])]
            m = re.match(r'^ALTER TABLE(?: ONLY)?\s+(.+?)\s+ADD CONSTRAINT\s+.+?\s+FOREIGN KEY\s*\((.*?)\)\s+REFERENCES\s+(.+?)\s*\((.*?)\)(.*);', line, re.I)
            if m and pg_ident(m[1]) in schemas:
                action = re.search(r'ON DELETE\s+(CASCADE|SET NULL|RESTRICT|NO ACTION)', m[5], re.I)
                schemas[pg_ident(m[1])]['fks'].append(dict(child=pg_ident(m[1]),child_columns=[pg_ident(c) for c in split_top(m[2])],parent=pg_ident(m[3]),parent_columns=[pg_ident(c) for c in split_top(m[4])],on_delete=action[1].upper() if action else 'RESTRICT'))
        if copy: raise DumpError(f'{path}: unterminated COPY block')
    for name, data in db.execute('SELECT tbl,data FROM pg_stage'):
        schema = schemas[name]
        if not schema['pk']: counts[f'skipped:{name}'] += 1; continue
        row = json.loads(data); key = key_for(row, schema['pk'], name)
        try: store_row(db, side, name, key, row, schemas[name]['columns'])
        except sqlite3.IntegrityError: raise DumpError(f'{path}: duplicate key {name} {key}')
        counts[name] += 1
    db.execute('DROP TABLE pg_stage')
    db.commit(); return schemas, counts

def decode_row(data, columns):
    return dict(zip(columns, json.loads(data)))

def changes(db, old, new, ignored):
    for table in sorted(set(old) | set(new)):
        if table not in old or table not in new:
            yield dict(kind='schema', table=table, detail='table added' if table in new else 'table removed'); continue
        a,b=old[table],new[table]
        if a != b: yield dict(kind='schema',table=table,detail='table definition changed',before=a,after=b)
        if not a['pk'] or not b['pk'] or a['pk'] != b['pk']: continue
        for key,data in db.execute('SELECT key,data FROM rows WHERE tbl=? AND NOT EXISTS (SELECT 1 FROM new_keys WHERE new_keys.tbl=rows.tbl AND new_keys.key=rows.key) ORDER BY key', (table,)):
            yield dict(kind='removed',table=table,key=json.loads(key),before=decode_row(data,a['columns']))
        for key,data,prior in db.execute('SELECT d.key,d.data,r.data FROM delta AS d LEFT JOIN rows AS r ON r.tbl=d.tbl AND r.key=d.key WHERE d.tbl=? ORDER BY d.key',(table,)):
            after=decode_row(data,b['columns'])
            if prior is None:
                yield dict(kind='added',table=table,key=json.loads(key),after=after)
            else:
                before=decode_row(prior,a['columns'])
                fields={k:dict(before=before.get(k),after=after.get(k)) for k in sorted(set(before)|set(after)) if before.get(k)!=after.get(k) and k not in ignored}
                if fields: yield dict(kind='changed',table=table,key=json.loads(key),fields=fields)

def annotate_relationship(db, old, event):
    if event['kind'] not in ('removed','changed'): return event
    schema = old.get(event['table'], {})
    for fk in schema.get('fks', []):
        parent = old.get(fk['parent'])
        if not parent or parent['pk'] != fk['parent_columns']: continue
        if event['kind'] == 'removed' and fk['on_delete'] == 'CASCADE':
            vals = [event['before'].get(c) for c in fk['child_columns']]
        elif event['kind'] == 'changed' and fk['on_delete'] == 'SET NULL' and all(c in event['fields'] and event['fields'][c]['after'] is None for c in fk['child_columns']):
            vals = [event['fields'][c]['before'] for c in fk['child_columns']]
        else: continue
        if any(v is None for v in vals): continue
        key = json.dumps(vals, ensure_ascii=False, separators=(',', ':'))
        present = db.execute('SELECT 1 FROM rows WHERE tbl=? AND key=?', (fk['parent'],key)).fetchone()
        newer = db.execute('SELECT 1 FROM new_keys WHERE tbl=? AND key=?', (fk['parent'],key)).fetchone()
        if present and not newer:
            event['relationship'] = dict(parent_table=fk['parent'],parent_key=vals,foreign_key=fk['child_columns'],action=fk['on_delete'],interpretation='consistent with foreign-key ' + ('cascade' if fk['on_delete']=='CASCADE' else 'SET NULL'))
            break
    return event

def run_flat(args):
    from flat_backend import Unsorted, partition_compare, stream_compare
    from fast_skip import unchanged_tables
    from flat_backend import dump_rows
    from dialect_rows import postgres_rows, sqlite_rows
    snapshot_inputs = Path(args.old).is_dir() and Path(args.new).is_dir()
    if Path(args.old).is_dir() != Path(args.new).is_dir():
        raise DumpError('both inputs must be dumps or both must be canonical snapshots')
    skipped_fast = unchanged_tables(args.old, args.new) if args.fast_skip and args.dialect == 'mysql' and not snapshot_inputs else set()
    reader = {'mysql': dump_rows, 'postgres': postgres_rows, 'sqlite-db': sqlite_rows}[args.dialect]
    with tempfile.TemporaryDirectory(prefix='dumptales-', dir=args.workdir) as tmp:
        output = Path(tmp) / 'events.jsonl'
        mode = args.engine
        if snapshot_inputs:
            from snapshot import compare as compare_snapshots
            old, new, ca, cb = compare_snapshots(args.old, args.new, output, set(args.ignore_column))
            mode = 'snapshot'
        if not snapshot_inputs and mode in ('auto', 'stream'):
            try:
                old, new, ca, cb = stream_compare(args.old, args.new, output, set(args.ignore_column), skipped_fast, reader)
                mode = 'stream'
            except Unsorted:
                if mode == 'stream':
                    raise
                mode = 'partition'
        if not snapshot_inputs and mode == 'partition':
            old, new, ca, cb = partition_compare(args.old, args.new, tmp, output, set(args.ignore_column), args.memory_limit * 1024 * 1024, skipped_fast, reader)
        skipped = sorted(t for t in set(old) | set(new) if (t in old and not old[t]['pk']) or (t in new and not new[t]['pk']))
        comparable = {t for t in set(old) & set(new) if old[t]['pk'] and old[t]['pk'] == new[t]['pk']}
        schema_events = []
        for table in sorted(set(old) | set(new)):
            if table not in old or table not in new:
                schema_events.append(dict(kind='schema',table=table,detail='table added' if table in new else 'table removed'))
            elif old[table] != new[table]:
                schema_events.append(dict(kind='schema',table=table,detail='table definition changed',before=old[table],after=new[table]))
        # Relationship explanation is bounded; once the cap is reached, omit
        # annotations rather than silently claim that a parent was not deleted.
        referenced = {fk['parent'] for spec in old.values() for fk in spec['fks']}
        removed = set()
        relationships_available = True
        with output.open(encoding='utf-8') as fh:
            for line in fh:
                item = json.loads(line)
                if item['kind'] == 'removed' and item['table'] in referenced and item['table'] in comparable:
                    removed.add((item['table'], json.dumps(item['key'], ensure_ascii=False, separators=(',', ':'))))
                    if len(removed) > 100000:
                        relationships_available = False
                        removed.clear()
                        break
        def explain(item):
            if not relationships_available or item['kind'] not in ('removed','changed'):
                return item
            for fk in old.get(item['table'], {}).get('fks', []):
                parent = old.get(fk['parent'])
                if not parent or parent['pk'] != fk['parent_columns']:
                    continue
                if item['kind'] == 'removed' and fk['on_delete'] == 'CASCADE':
                    values = [item['before'].get(col) for col in fk['child_columns']]
                elif item['kind'] == 'changed' and fk['on_delete'] == 'SET NULL' and all(col in item['fields'] and item['fields'][col]['after'] is None for col in fk['child_columns']):
                    values = [item['fields'][col]['before'] for col in fk['child_columns']]
                else:
                    continue
                key = json.dumps(values, ensure_ascii=False, separators=(',', ':'))
                if all(v is not None for v in values) and (fk['parent'], key) in removed:
                    item['relationship'] = dict(parent_table=fk['parent'],parent_key=values,foreign_key=fk['child_columns'],action=fk['on_delete'],interpretation='consistent with foreign-key ' + ('cascade' if fk['on_delete']=='CASCADE' else 'SET NULL'))
                    break
            return item
        counts = Counter()
        shown = 0
        color = args.color == 'always' or (args.color == 'auto' and sys.stdout.isatty())
        def paint(value, code):
            return f'\033[{code}m{value}\033[0m' if color else value
        if args.format == 'json':
            print('{"version":' + json.dumps(VERSION) + ',"changes":[', end='')
        def events():
            yield from schema_events
            with output.open(encoding='utf-8') as fh:
                for line in fh:
                    item = json.loads(line)
                    if item['table'] in comparable:
                        yield explain(item)
        for item in events():
            counts[item['kind']] += 1
            if args.format == 'json':
                if shown:
                    print(',', end='')
                print(json.dumps(item, ensure_ascii=False), end='')
            elif args.format == 'jsonl':
                print(json.dumps(dict(type='change', **item), ensure_ascii=False))
            elif shown < args.limit:
                sign, code = {'added': ('+', '32'), 'removed': ('-', '31'), 'changed': ('~', '33'), 'schema': ('!', '36')}[item['kind']]
                print(paint(f"{sign} {item['table']} {item.get('key', item.get('detail'))}", code))
                if item['kind'] == 'changed':
                    for name, values in item['fields'].items():
                        print(f"    {name}: {values['before']!r} → {values['after']!r}")
                if item.get('relationship'):
                    rel = item['relationship']
                    print(f"    ↳ {rel['interpretation']}: {rel['parent_table']} {rel['parent_key']}")
            shown += 1
        summary = dict(counts=dict(counts), skipped_tables=skipped,
                       unchanged_tables_skipped=sorted(skipped_fast),
                       old_rows=None if skipped_fast else sum(v for k,v in ca.items() if not k.startswith('skipped:')),
                       new_rows=None if skipped_fast else sum(v for k,v in cb.items() if not k.startswith('skipped:')),
                       engine=mode, parser='native' if args.dialect == 'mysql' and _native_next_row is not None else 'python',
                       relationships_complete=relationships_available)
        if args.format == 'json':
            print('],"summary":' + json.dumps(summary, ensure_ascii=False) + '}')
        elif args.format == 'jsonl':
            print(json.dumps(dict(type='summary', **summary), ensure_ascii=False))
        else:
            if shown > args.limit:
                print(f'... {shown-args.limit} more changes; use --format json or jsonl')
            print('Summary: ' + ', '.join(f'{k}={counts.get(k, 0)}' for k in ('added','removed','changed','schema')) + f' (engine={mode}, parser={summary["parser"]})')
            if skipped:
                print('Skipped tables without a primary key: ' + ', '.join(skipped), file=sys.stderr)
            if not relationships_available:
                print('Relationship annotations omitted: more than 100000 referenced parent removals', file=sys.stderr)
        return 1 if shown or skipped else 0

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == 'snapshot':
        from snapshot import main as snapshot_main
        return snapshot_main(argv[1:])
    p = argparse.ArgumentParser(description='Explain differences between two MySQL/MariaDB SQL dumps (read-only).')
    p.add_argument('old'); p.add_argument('new'); p.add_argument('--dialect', choices=['mysql','sqlite-db','postgres'], default='mysql'); p.add_argument('--format', choices=['human','json','jsonl'], default='human')
    p.add_argument('--color', choices=['auto','always','never'], default='auto'); p.add_argument('--ignore-column', action='append', default=[])
    p.add_argument('--limit', type=int, default=200, help='Maximum human row details (counts remain complete)')
    p.add_argument('--workdir', help='Parent directory for temporary files; needs free space')
    p.add_argument('--engine', choices=['auto','stream','partition','sqlite'], default='auto', help='auto tries streaming then partitions if unordered; sqlite uses a temporary index')
    p.add_argument('--memory-limit', type=int, default=64, help='Approximate MiB per partition (default: 64)')
    p.add_argument('--no-fast-skip', dest='fast_skip', action='store_false', help='Fully parse identical MySQL table data; retain row totals')
    args = p.parse_args(argv)
    if args.limit < 0: p.error('--limit must be nonnegative')
    if args.memory_limit < 1: p.error('--memory-limit must be positive')
    if args.engine != 'sqlite' or (args.dialect == 'mysql' and Path(args.old).is_dir()):
        from flat_backend import DumpError as FlatDumpError
        try:
            return run_flat(args)
        except (FlatDumpError, DumpError, OSError, UnicodeError, ValueError) as ex:
            print(f'dumptales: error: {ex}', file=sys.stderr)
            return 2
    try:
        with tempfile.TemporaryDirectory(prefix='dumptales-', dir=args.workdir) as tmp:
            db = sqlite3.connect(str(Path(tmp)/'index.sqlite'))
            db.execute('CREATE TABLE rows (tbl TEXT, key TEXT, data TEXT, PRIMARY KEY (tbl,key)) WITHOUT ROWID')
            db.execute('CREATE TABLE new_keys (tbl TEXT, key TEXT, PRIMARY KEY (tbl,key)) WITHOUT ROWID')
            db.execute('CREATE TABLE delta (tbl TEXT, key TEXT, data TEXT, PRIMARY KEY (tbl,key)) WITHOUT ROWID')
            reader = {'mysql':ingest,'sqlite-db':ingest_sqlite_file,'postgres':ingest_postgres}[args.dialect]
            old, ca = reader(args.old, db, 0); new, cb = reader(args.new, db, 1)
            skipped = sorted(t for t in set(old)|set(new) if (t in old and not old[t]['pk']) or (t in new and not new[t]['pk']))
            counts = Counter(); shown = 0
            summary_base = dict(skipped_tables=skipped, old_rows=sum(v for k,v in ca.items() if not k.startswith('skipped:')), new_rows=sum(v for k,v in cb.items() if not k.startswith('skipped:')))
            color = args.color == 'always' or (args.color == 'auto' and sys.stdout.isatty())
            def paint(value, n): return f'\033[{n}m{value}\033[0m' if color else value
            if args.format == 'json': print('{"version":' + json.dumps(VERSION) + ',"changes":[')
            for e in changes(db, old, new, set(args.ignore_column)):
                annotate_relationship(db, old, e)
                counts[e['kind']] += 1
                if args.format == 'json':
                    if shown: print(',')
                    print(json.dumps(e, ensure_ascii=False), end='')
                elif args.format == 'jsonl': print(json.dumps(dict(type='change', **e), ensure_ascii=False))
                elif shown < args.limit:
                    tag = {'added':('+','32'), 'removed':('-','31'), 'changed':('~','33'), 'schema':('!','36')}[e['kind']]
                    print(paint(f"{tag[0]} {e['table']} {e.get('key',e.get('detail'))}", tag[1]))
                    if e['kind']=='changed':
                        for name, v in e['fields'].items(): print(f"    {name}: {v['before']!r} → {v['after']!r}")
                    if e.get('relationship'):
                        rel=e['relationship']; print(f"    ↳ {rel['interpretation']}: {rel['parent_table']} {rel['parent_key']}")
                shown += 1
            summary = dict(summary_base, counts=dict(counts))
            if args.format == 'json': print('],"summary":' + json.dumps(summary, ensure_ascii=False) + '}')
            elif args.format == 'jsonl': print(json.dumps(dict(type='summary', **summary), ensure_ascii=False))
            else:
                if shown>args.limit: print(f'... {shown-args.limit} more changes; use --format json or jsonl')
                print('Summary: ' + ', '.join(f'{k}={counts.get(k,0)}' for k in ('added','removed','changed','schema')))
                if skipped: print('Skipped tables without a primary key: ' + ', '.join(skipped), file=sys.stderr)
            return 1 if shown or skipped else 0
    except (DumpError, OSError, UnicodeError, sqlite3.Error) as ex:
        print(f'dumptales: error: {ex}', file=sys.stderr); return 2

if __name__ == '__main__': sys.exit(main())
