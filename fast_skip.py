"""Identify identical conventional mysqldump table sections cheaply."""
import hashlib
import re
from collections import defaultdict
from dumptales import open_dump, parse_create, DumpError

INSERT_LINE = re.compile(r'^INSERT INTO\s+`((?:``|[^`])+)`\s+(?:\([^\n]*\)\s+)?VALUES\s*\(', re.I)
CREATE_LINE = re.compile(r'^CREATE TABLE(?: IF NOT EXISTS)?\s+`((?:``|[^`])+)`\s*\(', re.I)


def signatures(path):
    inserts = defaultdict(hashlib.sha256)
    definitions = {}
    counts = defaultdict(int)
    eligible = set()
    blocked = set()
    collecting = None
    body = []
    with open_dump(path) as fh:
        for line in fh:
            if collecting is not None:
                body.append(line)
                if line.rstrip().endswith(';'):
                    try:
                        name, schema = parse_create(''.join(body).rstrip()[:-1])
                    except DumpError:
                        blocked.add(collecting)
                    else:
                        if name != collecting or name in definitions:
                            blocked.add(collecting)
                        else:
                            definitions[name] = (hashlib.sha256(''.join(body).encode()).digest(), schema)
                    collecting = None
                    body = []
                continue
            match = CREATE_LINE.match(line)
            if match:
                collecting = match.group(1).replace('``', '`')
                body = [line]
                if line.rstrip().endswith(';'):
                    try:
                        name, schema = parse_create(line.rstrip()[:-1])
                        if name in definitions:
                            blocked.add(name)
                        else:
                            definitions[name] = (hashlib.sha256(line.encode()).digest(), schema)
                    except DumpError:
                        blocked.add(collecting)
                    collecting = None
                    body = []
                continue
            if line.lstrip().upper().startswith(('INSERT ', 'REPLACE ', 'UPDATE ', 'DELETE ', 'ALTER TABLE', 'LOAD DATA')):
                match = INSERT_LINE.match(line)
                if match and line.rstrip().endswith(');'):
                    name = match.group(1).replace('``', '`')
                    if name not in definitions:
                        blocked.add(name)
                    eligible.add(name)
                    inserts[name].update(line.encode())
                    counts[name] += 1
                else:
                    # Unknown data form: disable the shortcut for the entire dump.
                    return {}
    if collecting is not None:
        return {}
    return {name:(definitions[name][0], inserts[name].digest(), counts[name])
            for name in eligible - blocked if name in definitions and definitions[name][1]['pk']}


def unchanged_tables(old_path, new_path):
    old = signatures(old_path)
    new = signatures(new_path)
    return {name for name in old.keys() & new.keys() if old[name] == new[name]}
