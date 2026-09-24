"""Bounded-memory comparison of database snapshots without a database index."""
from collections import Counter, OrderedDict
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
import resource
from pathlib import Path

from dumptales import DumpError, key_for, parse_create, parse_insert, statements

class Unsorted(DumpError):
    pass


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def dump_rows(path, schema, counts, skip=frozenset()):
    for line, sql in statements(path):
        prefix = sql[:70].lstrip().upper()
        try:
            if prefix.startswith('CREATE TABLE'):
                table, spec = parse_create(sql)
                if table in schema:
                    raise DumpError(f'{table}: duplicate CREATE TABLE')
                schema[table] = spec
            elif prefix.startswith(('INSERT ', 'REPLACE ')):
                if skip:
                    match = __import__('re').match(r'^INSERT INTO\s+`((?:``|[^`])+)`', sql, __import__('re').I)
                    if match and match.group(1).replace('``', '`') in skip:
                        continue
                for table, row in parse_insert(sql, schema):
                    if not schema[table]['pk']:
                        counts[f'skipped:{table}'] += 1
                        continue
                    key = key_for(row, schema[table]['pk'], table)
                    counts[table] += 1
                    yield table, key, row
            elif prefix.startswith(('UPDATE ', 'DELETE ', 'LOAD DATA', 'ALTER TABLE')):
                raise DumpError(f'unsupported data/schema statement: {sql[:90]!r}')
        except DumpError as exc:
            raise DumpError(f'{path}:{line}: {exc}') from exc


def key_order(key):
    """Natural ordering for numeric keys; string keys retain lexical order."""
    order = []
    for value in json.loads(key):
        if isinstance(value, str):
            try:
                numeric = Decimal(value)
                if numeric.is_finite():
                    order.append((0, numeric))
                    continue
            except InvalidOperation:
                pass
            order.append((1, value))
        elif isinstance(value, (int, float)):
            order.append((0, Decimal(str(value))))
        else:
            order.append((2, str(value)))
    return tuple(order)


def ordered(rows):
    last = None
    for item in rows:
        table, key, _ = item
        marker = (table, key_order(key))
        if last is not None and marker <= last:
            raise Unsorted(f'rows are not strictly ordered by table and primary key near {table} {key}')
        last = marker
        yield item


def event(kind, table, key, before=None, after=None, ignored=frozenset()):
    common = dict(kind=kind, table=table, key=json.loads(key))
    if kind == 'removed':
        common['before'] = before
    elif kind == 'added':
        common['after'] = after
    else:
        fields = {name: dict(before=before.get(name), after=after.get(name))
                  for name in sorted(before.keys() | after.keys())
                  if name not in ignored and before.get(name) != after.get(name)}
        if not fields:
            return None
        common['fields'] = fields
    return common


def write_event(out, item):
    if item is not None:
        out.write(compact(item) + '\n')


def stream_compare(old_path, new_path, output, ignored, skip=frozenset(), reader=dump_rows):
    old_schema, new_schema = {}, {}
    old_count, new_count = Counter(), Counter()
    left = iter(ordered(reader(old_path, old_schema, old_count, skip)))
    right = iter(ordered(reader(new_path, new_schema, new_count, skip)))
    with open(output, 'w', encoding='utf-8') as out:
        a = next(left, None)
        b = next(right, None)
        while a is not None or b is not None:
            ak = (a[0], key_order(a[1])) if a else None
            bk = (b[0], key_order(b[1])) if b else None
            if b is None or (a is not None and ak < bk):
                write_event(out, event('removed', *a[:2], before=a[2]))
                a = next(left, None)
            elif a is None or bk < ak:
                write_event(out, event('added', *b[:2], after=b[2]))
                b = next(right, None)
            else:
                # Natural ordering may tie distinct textual keys, e.g. '01' and '1'.
                if a[0] == b[0] and a[1] == b[1]:
                    write_event(out, event('changed', a[0], a[1], a[2], b[2], ignored))
                else:
                    raise Unsorted('ambiguous primary-key ordering; use partition mode')
                a = next(left, None)
                b = next(right, None)
    return old_schema, new_schema, old_count, new_count


class Writers:
    def __init__(self, directory, side, handles=None):
        if handles is None:
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            handles = min(256, max(8, soft - 32))
        self.directory = Path(directory)
        self.side = side
        self.handles = handles
        self.active = OrderedDict()

    def write(self, number, record):
        key = f'{self.side}-{number:02x}.jsonl.gz'
        if key in self.active:
            fh = self.active.pop(key)
        else:
            if len(self.active) >= self.handles:
                _, old = self.active.popitem(last=False)
                old.close()
            fh = gzip.open(self.directory / key, 'at', encoding='utf-8', compresslevel=1)
        self.active[key] = fh
        fh.write(compact(record) + '\n')

    def close(self):
        for fh in self.active.values():
            fh.close()
        self.active.clear()


def bucket(table, key, depth=0):
    digest = hashlib.sha256((table + '\0' + key).encode()).digest()
    return digest[depth]


def partition_dump(path, directory, side, schema, counts, skip=frozenset(), reader=dump_rows):
    writers = Writers(directory, side)
    try:
        for table, key, row in reader(path, schema, counts, skip):
            writers.write(bucket(table, key), [table, key, row])
    finally:
        writers.close()


def records(path):
    if not path.exists():
        return
    with gzip.open(path, 'rt', encoding='utf-8') as fh:
        for line in fh:
            yield json.loads(line)


def partition_pair(old_path, new_path, output, ignored, max_bytes, depth=0):
    if depth >= 4:
        raise DumpError('partition remains too large after four splits; increase --memory-limit')
    # Bound both the old dictionary and the new-key duplicate set.
    def fits(path):
        size = 0
        for table, key, row in records(path):
            size += len(compact(row)) + len(key) + len(table) + 180
            if size > max_bytes:
                return False
        return True
    if fits(old_path) and fits(new_path):
        old = {}
        for table, key, row in records(old_path):
            marker = (table, key)
            if marker in old:
                raise DumpError(f'{table}: duplicate primary key {key}')
            old[marker] = row
        with open(output, 'a', encoding='utf-8') as out:
            seen = set()
            for table, key, row in records(new_path):
                marker = (table, key)
                if marker in seen:
                    raise DumpError(f'{table}: duplicate primary key {key}')
                seen.add(marker)
                previous = old.pop(marker, None)
                if previous is None:
                    write_event(out, event('added', table, key, after=row))
                elif previous != row:
                    write_event(out, event('changed', table, key, previous, row, ignored))
            for (table, key), row in old.items():
                write_event(out, event('removed', table, key, before=row))
        return
    # Repartition the pair by another hash byte, ensuring the oversize bucket
    # does not require either input to fit in memory.
    directory = old_path.parent / f'split-{depth}-{old_path.stem}'
    directory.mkdir()
    for side, path in (('a', old_path), ('b', new_path)):
        writer = Writers(directory, side)
        try:
            for table, key, row in records(path):
                writer.write(bucket(table, key, depth + 1), [table, key, row])
        finally:
            writer.close()
    for number in range(256):
        a = directory / f'a-{number:02x}.jsonl.gz'
        b = directory / f'b-{number:02x}.jsonl.gz'
        if a.exists() or b.exists():
            partition_pair(a, b, output, ignored, max_bytes, depth + 1)
            a.unlink(missing_ok=True)
            b.unlink(missing_ok=True)
    directory.rmdir()


def partition_compare(old_path, new_path, directory, output, ignored, max_bytes, skip=frozenset(), reader=dump_rows):
    old_schema, new_schema = {}, {}
    old_count, new_count = Counter(), Counter()
    partition_dump(old_path, directory, 'a', old_schema, old_count, skip, reader)
    partition_dump(new_path, directory, 'b', new_schema, new_count, skip, reader)
    Path(output).write_text('', encoding='utf-8')
    for number in range(256):
        a = Path(directory) / f'a-{number:02x}.jsonl.gz'
        b = Path(directory) / f'b-{number:02x}.jsonl.gz'
        if a.exists() or b.exists():
            partition_pair(a, b, output, ignored, max_bytes)
            a.unlink(missing_ok=True)
            b.unlink(missing_ok=True)
    return old_schema, new_schema, old_count, new_count
