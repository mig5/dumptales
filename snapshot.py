"""Portable, canonical per-table snapshot format for repeat comparisons."""
import argparse
from collections import Counter, OrderedDict
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from dumptales import DumpError, key_for
from flat_backend import compact, dump_rows, event, write_event

FORMAT = 'dumpstory.snapshot.v1'  # Preserve compatibility with existing snapshots.


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def snapshot_name(table):
    return hashlib.sha256(table.encode('utf-8')).hexdigest() + '.jsonl.gz'


def create(source, destination, memory_mib=64):
    target = Path(destination).absolute()
    if target.exists():
        raise DumpError(f'output path already exists: {target}')
    if not shutil.which('sort'):
        raise DumpError('GNU sort is required to create a snapshot')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.dumptales-build-', dir=target.parent) as temp:
        work = Path(temp)
        schema, counts = {}, Counter()
        handles = OrderedDict()
        def write(table, line):
            filename = snapshot_name(table)
            if filename in handles:
                fh = handles.pop(filename)
            else:
                if len(handles) == 48:
                    _, old = handles.popitem(last=False)
                    old.close()
                fh = (work / (filename + '.raw')).open('a', encoding='utf-8')
            handles[filename] = fh
            fh.write(line)
        try:
            for table, key, row in dump_rows(source, schema, counts):
                values = [row.get(col) for col in schema[table]['columns']]
                write(table, key + '\t' + compact(values) + '\n')
        finally:
            for fh in handles.values():
                fh.close()
        if any(not item['pk'] for item in schema.values()):
            raise DumpError('canonical snapshot requires primary keys on every table')
        manifest = dict(format=FORMAT, tables={})
        for table in sorted(schema):
            filename = snapshot_name(table)
            raw = work / (filename + '.raw')
            raw.touch(exist_ok=True)
            ordered = work / (filename + '.sorted')
            env = dict(os.environ, LC_ALL='C')
            result = subprocess.run(['sort', '--stable', '--buffer-size', f'{memory_mib}M',
                                     '--temporary-directory', str(work), '-t', '\t', '-k1,1',
                                     str(raw), '-o', str(ordered)], env=env, capture_output=True, text=True)
            if result.returncode:
                raise DumpError(f'sort failed for {table}: {result.stderr[:300]}')
            digest = hashlib.sha256()
            previous = None
            count = 0
            with ordered.open('rb') as src, gzip.open(work / filename, 'wb', compresslevel=1) as dst:
                for line in src:
                    key = line.split(b'\t', 1)[0]
                    if previous == key:
                        raise DumpError(f'{table}: duplicate primary key {key[:100]!r}')
                    previous = key
                    digest.update(line)
                    dst.write(line)
                    count += 1
            file_hash = hash_file(work / filename)
            manifest['tables'][table] = dict(schema=schema[table], file=filename,
                                             sha256=digest.hexdigest(), file_sha256=file_hash, rows=count)
            raw.unlink()
            ordered.unlink()
        (work / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        work.rename(target)
    return manifest


def load(directory):
    path = Path(directory) / 'manifest.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise DumpError(f'invalid snapshot manifest {path}: {exc}') from exc
    if data.get('format') != FORMAT or not isinstance(data.get('tables'), dict):
        raise DumpError(f'unsupported snapshot format in {path}')
    for table, entry in data['tables'].items():
        if entry.get('file') != snapshot_name(table):
            raise DumpError(f'invalid snapshot table filename for {table}')
        table_path = Path(directory) / entry['file']
        if not table_path.is_file():
            raise DumpError(f'missing snapshot table {table}')
        if hash_file(table_path) != entry.get('file_sha256'):
            raise DumpError(f'snapshot file checksum mismatch: {table_path}')
    return data


def records(directory, entry):
    path = Path(directory) / entry['file']
    digest = hashlib.sha256()
    previous = None
    count = 0
    with gzip.open(path, 'rb') as fh:
        for line in fh:
            digest.update(line)
            try:
                key, values = line.rstrip(b'\n').split(b'\t', 1)
                marker = key.decode('utf-8')
                if previous is not None and marker <= previous:
                    raise DumpError(f'snapshot keys out of order in {path}')
                previous = marker
                row = dict(zip(entry['schema']['columns'], json.loads(values)))
                if len(json.loads(values)) != len(entry['schema']['columns']):
                    raise DumpError(f'invalid row width in {path}')
                if key_for(row, entry['schema']['pk'], path.name) != marker:
                    raise DumpError(f'snapshot key mismatch in {path}')
            except (UnicodeError, ValueError) as exc:
                raise DumpError(f'invalid snapshot row in {path}: {exc}') from exc
            count += 1
            yield marker, row
    if digest.hexdigest() != entry['sha256'] or count != entry['rows']:
        raise DumpError(f'snapshot checksum/count mismatch: {path}')


def compare(old_dir, new_dir, output, ignored):
    a, b = load(old_dir), load(new_dir)
    old_schema = {t: entry['schema'] for t, entry in a['tables'].items()}
    new_schema = {t: entry['schema'] for t, entry in b['tables'].items()}
    with open(output, 'w', encoding='utf-8') as out:
        for table in sorted(set(a['tables']) & set(b['tables'])):
            left_entry, right_entry = a['tables'][table], b['tables'][table]
            if left_entry['sha256'] == right_entry['sha256'] and left_entry['schema'] == right_entry['schema'] and left_entry['rows'] == right_entry['rows']:
                continue
            if not left_entry['schema']['pk'] or left_entry['schema']['pk'] != right_entry['schema']['pk']:
                continue
            left = iter(records(old_dir, left_entry))
            right = iter(records(new_dir, right_entry))
            x = next(left, None)
            y = next(right, None)
            while x is not None or y is not None:
                if y is None or (x is not None and x[0] < y[0]):
                    write_event(out, event('removed', table, x[0], before=x[1]))
                    x = next(left, None)
                elif x is None or y[0] < x[0]:
                    write_event(out, event('added', table, y[0], after=y[1]))
                    y = next(right, None)
                else:
                    write_event(out, event('changed', table, x[0], x[1], y[1], ignored))
                    x = next(left, None)
                    y = next(right, None)
    old_count = Counter({t: entry['rows'] for t, entry in a['tables'].items()})
    new_count = Counter({t: entry['rows'] for t, entry in b['tables'].items()})
    return old_schema, new_schema, old_count, new_count


def main(argv):
    parser = argparse.ArgumentParser(prog='dumptales snapshot', description='Create a canonical MySQL snapshot for repeat comparisons.')
    parser.add_argument('source')
    parser.add_argument('output')
    parser.add_argument('--memory-limit', type=int, default=64)
    options = parser.parse_args(argv)
    if options.memory_limit < 1:
        parser.error('--memory-limit must be positive')
    try:
        result = create(options.source, options.output, options.memory_limit)
    except (DumpError, OSError, UnicodeError) as exc:
        print(f'dumptales: error: {exc}', file=sys.stderr)
        return 2
    print(f"Snapshot created: {options.output} ({len(result['tables'])} tables)")
    return 0
