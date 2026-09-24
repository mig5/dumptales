import json
import bz2
import gzip
import zipfile
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).parents[1] / 'dumptales.py'

class TestCLI(unittest.TestCase):
    def test_mysql_relationships_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td)/'a.sql', Path(td)/'b.sql'
            schema = '''CREATE TABLE `parent` (`id` int, `name` text, PRIMARY KEY (`id`));
CREATE TABLE `child` (`id` int, `parent_id` int, PRIMARY KEY (`id`), CONSTRAINT `fk` FOREIGN KEY (`parent_id`) REFERENCES `parent` (`id`) ON DELETE CASCADE);
'''
            a.write_text(schema + "INSERT INTO `parent` VALUES (1,'old'),(2,'keep');\nINSERT INTO `child` VALUES (3,1);\n")
            b.write_text(schema + "INSERT INTO `parent` VALUES (2,'new');\n")
            cmd = [sys.executable, str(CLI), str(a), str(b), '--format', 'json']
            proc = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            changes = json.loads(proc.stdout)['changes']
            self.assertEqual(sorted(e['kind'] for e in changes), ['changed','removed','removed'])
            child = next(e for e in changes if e['table'] == 'child')
            self.assertEqual(child['relationship']['parent_key'], ['1'])
            self.assertEqual(subprocess.run([sys.executable,str(CLI),str(a),str(a)],capture_output=True).returncode,0)

    def test_column_order_and_meaning(self):
        with tempfile.TemporaryDirectory() as td:
            a,b = Path(td)/'old.sql',Path(td)/'new.sql'
            a.write_text("CREATE TABLE `t` (`id` int, `first` text, `second` text, PRIMARY KEY (`id`));\nINSERT INTO `t` VALUES (1,'alpha','beta');\n")
            b.write_text("CREATE TABLE `t` (`id` int, `second` text, `first` text, PRIMARY KEY (`id`));\nINSERT INTO `t` VALUES (1,'beta','changed');\n")
            p=subprocess.run([sys.executable,str(CLI),str(a),str(b),'--format','json'],capture_output=True,text=True)
            self.assertEqual(p.returncode,1,p.stderr)
            changes=json.loads(p.stdout)['changes']
            self.assertEqual(changes[-1]['fields'], {'first':{'before':'alpha','after':'changed'}})

    def test_partition_fallback_and_sqlite_equivalence(self):
        with tempfile.TemporaryDirectory() as td:
            a,b=Path(td)/'a.sql',Path(td)/'b.sql'
            schema="CREATE TABLE `t` (`id` int, `value` text, PRIMARY KEY (`id`));\n"
            a.write_text(schema + "INSERT INTO `t` VALUES (3,'gone'),(1,'before'),(2,'same');\n")
            b.write_text(schema + "INSERT INTO `t` VALUES (2,'same'),(4,'new'),(1,'after');\n")
            results=[]
            for mode in ('auto','partition','sqlite'):
                proc=subprocess.run([sys.executable,str(CLI),str(a),str(b),'--engine',mode,'--format','json','--memory-limit','1'],capture_output=True,text=True)
                self.assertEqual(proc.returncode,1,proc.stderr)
                result=json.loads(proc.stdout)
                results.append(sorted(result['changes'], key=lambda x:(x['kind'],x['key'])))
                if mode == 'auto': self.assertEqual(result['summary']['engine'],'partition')
            self.assertEqual(results[0],results[1])
            self.assertEqual(results[0],results[2])
            p=subprocess.run([sys.executable,str(CLI),str(a),str(b),'--engine','stream'],capture_output=True,text=True)
            self.assertEqual(p.returncode,2)

    def test_partition_detects_duplicate_key(self):
        with tempfile.TemporaryDirectory() as td:
            a,b=Path(td)/'a.sql',Path(td)/'b.sql'
            schema="CREATE TABLE `t` (`id` int, PRIMARY KEY (`id`));\n"
            a.write_text(schema + 'INSERT INTO `t` VALUES (1),(1);\n')
            b.write_text(schema + 'INSERT INTO `t` VALUES (1);\n')
            p=subprocess.run([sys.executable,str(CLI),str(a),str(b),'--engine','partition'],capture_output=True,text=True)
            self.assertEqual(p.returncode,2)
            self.assertIn('duplicate primary key',p.stderr)

    def test_sqlite_file(self):
        with tempfile.TemporaryDirectory() as td:
            paths = [Path(td)/f'{i}.db' for i in range(2)]
            for i,path in enumerate(paths):
                con=sqlite3.connect(path); con.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, value TEXT)'); con.execute('INSERT INTO t VALUES (1,?)',('a' if i==0 else 'b',)); con.commit(); con.close()
            p=subprocess.run([sys.executable,str(CLI),str(paths[0]),str(paths[1]),'--dialect','sqlite-db','--format','json'],capture_output=True,text=True)
            self.assertEqual(p.returncode,1,p.stderr)
            self.assertEqual(json.loads(p.stdout)['changes'][0]['fields']['value'],{'before':'a','after':'b'})

    def test_postgres_copy_then_key(self):
        with tempfile.TemporaryDirectory() as td:
            paths = [Path(td)/f'{i}.sql' for i in range(2)]
            for i,path in enumerate(paths):
                path.write_text('CREATE TABLE public.t (\n    id integer,\n    val text\n);\nCOPY public.t (id, val) FROM stdin;\n1\t'+ ('old' if i==0 else 'new') +'\n\\.\nALTER TABLE ONLY public.t ADD CONSTRAINT t_pkey PRIMARY KEY (id);\n')
            p=subprocess.run([sys.executable,str(CLI),str(paths[0]),str(paths[1]),'--dialect','postgres','--format','json'],capture_output=True,text=True)
            self.assertEqual(p.returncode,1,p.stderr)
            self.assertEqual(json.loads(p.stdout)['summary']['counts'],{'changed':1})

    def test_multiline_postgres_keys_auto_and_compression(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            paths = [directory / f'{i}.sql' for i in range(2)]
            for i, path in enumerate(paths):
                path.write_text('-- PostgreSQL database dump\nCREATE TABLE public.t (\n    id integer,\n    val text\n);\n'
                                'COPY public.t (id, val) FROM stdin;\n1\t' + ('old' if i == 0 else 'new') +
                                '\n\\.\nALTER TABLE ONLY public.t\n    ADD CONSTRAINT t_pkey PRIMARY KEY (id);\n')
            for engine in ('auto', 'sqlite'):
                for extension in ('sql', 'gz', 'bz2', 'zip'):
                    copies = []
                    for i, path in enumerate(paths):
                        target = directory / f'{i}.{extension}'
                        raw = path.read_bytes()
                        if extension == 'sql':
                            target = path
                        elif extension == 'gz':
                            target.write_bytes(gzip.compress(raw))
                        elif extension == 'bz2':
                            target.write_bytes(bz2.compress(raw))
                        else:
                            with zipfile.ZipFile(target, 'w') as archive:
                                archive.writestr('dump.sql', raw)
                        copies.append(target)
                    proc = subprocess.run([sys.executable, str(CLI), *map(str, copies), '--engine', engine, '--format', 'json'], capture_output=True, text=True)
                    self.assertEqual(proc.returncode, 1, (extension, engine, proc.stderr))
                    self.assertEqual(json.loads(proc.stdout)['summary']['counts'], {'changed': 1})
            with zipfile.ZipFile(directory / 'bad.zip', 'w') as archive:
                archive.writestr('a.sql', paths[0].read_bytes())
                archive.writestr('b.sql', paths[1].read_bytes())
            proc = subprocess.run([sys.executable, str(CLI), str(directory / 'bad.zip'), str(paths[1])], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertIn('exactly one dump', proc.stderr)

    def test_no_detected_primary_keys_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'no-key.sql'
            path.write_text('-- PostgreSQL database dump\nCREATE TABLE public.t (\n    id integer\n);\n'
                            'COPY public.t (id) FROM stdin;\n1\n\\.\n')
            for engine in ('auto', 'sqlite'):
                proc = subprocess.run([sys.executable, str(CLI), str(path), str(path), '--engine', engine], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 2)
                self.assertIn('skip every row', proc.stderr)

    def test_postgres_unsorted_partition_matches_index(self):
        with tempfile.TemporaryDirectory() as td:
            paths = [Path(td)/f'{i}.sql' for i in range(2)]
            schema = 'CREATE TABLE public.t (\n    id integer,\n    val text\n);\n'
            rows = ['2\tbefore\n1\tsame\n', '1\tsame\n3\tadded\n2\tafter\n']
            for path, body in zip(paths, rows):
                path.write_text(schema + 'COPY public.t (id, val) FROM stdin;\n' + body + '\\.\nALTER TABLE ONLY public.t ADD CONSTRAINT t_pkey PRIMARY KEY (id);\n')
            outputs = []
            for engine in ('auto', 'sqlite'):
                p = subprocess.run([sys.executable, str(CLI), *map(str, paths), '--dialect', 'postgres', '--engine', engine, '--format', 'json'], capture_output=True, text=True)
                self.assertEqual(p.returncode, 1, p.stderr)
                outputs.append(json.loads(p.stdout))
            self.assertEqual(outputs[0]['summary']['engine'], 'partition')
            self.assertEqual(sorted(outputs[0]['changes'], key=str), sorted(outputs[1]['changes'], key=str))

    def test_sqlite_stream_matches_index(self):
        with tempfile.TemporaryDirectory() as td:
            paths = [Path(td)/f'{i}.db' for i in range(2)]
            for i, path in enumerate(paths):
                with sqlite3.connect(path) as con:
                    con.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, value TEXT)')
                    con.executemany('INSERT INTO t VALUES (?,?)', [(2, 'old' if not i else 'new'), (1, 'same'), (3 if i else 4, 'extra')])
            outputs = []
            for engine in ('auto', 'sqlite'):
                p = subprocess.run([sys.executable, str(CLI), *map(str, paths), '--dialect', 'sqlite-db', '--engine', engine, '--format', 'json'], capture_output=True, text=True)
                self.assertEqual(p.returncode, 1, p.stderr)
                outputs.append(json.loads(p.stdout))
            self.assertEqual(outputs[0]['summary']['engine'], 'stream')
            self.assertEqual(sorted(outputs[0]['changes'], key=str), sorted(outputs[1]['changes'], key=str))

if __name__ == '__main__': unittest.main()

class TestNewPaths(unittest.TestCase):
    def test_identical_table_skip_and_changed_table(self):
        with tempfile.TemporaryDirectory() as td:
            a,b=Path(td)/'a.sql',Path(td)/'b.sql'
            same="CREATE TABLE `same` (`id` int, `value` text, PRIMARY KEY (`id`));\nINSERT INTO `same` VALUES (1,'x'),(2,'y');\n"
            changed="CREATE TABLE `other` (`id` int, `value` text, PRIMARY KEY (`id`));\n"
            a.write_text(same+changed+"INSERT INTO `other` VALUES (1,'old');\n")
            b.write_text(same+changed+"INSERT INTO `other` VALUES (1,'new');\n")
            proc=subprocess.run([sys.executable,str(CLI),str(a),str(b),'--format','json'],capture_output=True,text=True)
            self.assertEqual(proc.returncode,1,proc.stderr)
            result=json.loads(proc.stdout)
            self.assertEqual(result['summary']['unchanged_tables_skipped'],['same'])
            self.assertIsNone(result['summary']['old_rows'])
            self.assertEqual([(e['table'],e['kind']) for e in result['changes']],[('other','changed')])
            full=subprocess.run([sys.executable,str(CLI),str(a),str(b),'--format','json','--no-fast-skip'],capture_output=True,text=True)
            self.assertEqual(json.loads(full.stdout)['changes'],result['changes'])

    def test_fast_skip_does_not_hide_insert_before_schema(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'invalid.sql'
            path.write_text("INSERT INTO `t` VALUES (1);\nCREATE TABLE `t` (`id` int, PRIMARY KEY (`id`));\n")
            proc=subprocess.run([sys.executable,str(CLI),str(path),str(path)],capture_output=True,text=True)
            self.assertEqual(proc.returncode,2)
            self.assertIn('before CREATE TABLE',proc.stderr)

    def test_snapshot_roundtrip_integrity(self):
        with tempfile.TemporaryDirectory() as td:
            a,b=Path(td)/'a.sql',Path(td)/'b.sql'
            old,new=Path(td)/'old',Path(td)/'new'
            ddl="CREATE TABLE `t` (`id` int, `value` text, PRIMARY KEY (`id`));\n"
            a.write_text(ddl+"INSERT INTO `t` VALUES (2,'same'),(1,'old');\n")
            b.write_text(ddl+"INSERT INTO `t` VALUES (3,'added'),(1,'new'),(2,'same');\n")
            for src,dst in ((a,old),(b,new)):
                proc=subprocess.run([sys.executable,str(CLI),'snapshot',str(src),str(dst)],capture_output=True,text=True)
                self.assertEqual(proc.returncode,0,proc.stderr)
            cmd=[sys.executable,str(CLI),str(old),str(new),'--format','json']
            result=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(result.returncode,1,result.stderr)
            self.assertEqual(result.returncode,subprocess.run([sys.executable,str(CLI),str(a),str(b)],capture_output=True).returncode)
            changes=json.loads(result.stdout)['changes']
            self.assertEqual(sorted(e['kind'] for e in changes),['added','changed'])
            table_file=next(old.glob('*.jsonl.gz'))
            with table_file.open('ab') as handle: handle.write(b'corruption')
            invalid=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(invalid.returncode,2)
            self.assertIn('checksum',invalid.stderr)

    def test_native_equivalence_if_built(self):
        sys.path.insert(0,str(CLI.parent))
        try:
            import dumptales
            try:
                import _dumptales_native
            except ImportError:
                self.skipTest('optional C extension not built')
            samples = ["(1,'a,b')", "('it\\'s','a\\n'),(2,'x')", "('élève','x''y')", "(NULL,0xAB,'\\\\')"]
            import random
            rng = random.Random(9)
            atoms = ["'a,b'", "'\\\\'", "'he''llo'", "'é,漢字'", 'NULL', '123.45', "'x\\'y'"]
            for _ in range(250):
                samples.append('(' + ','.join(rng.choices(atoms, k=rng.randint(1, 8))) + ')')
            for sample in samples:
                raw,pos=dumptales.parens(sample,0)
                self.assertEqual(_dumptales_native.next_row(sample,0),(dumptales.split_top(raw),pos))
        finally:
            sys.path.pop(0)
