"""Publish this experiment's noncredential artifacts to its verified WSL destination."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time

from .common import append


class WSLSync:
    def __init__(self, root):
        self.root = root
        self.sent = {}
        self.last_sync = 0.0

    def sync(self, *, force=False, vectors=False):
        if not force and time.monotonic() - self.last_sync < 60:
            return
        allowed_dirs = {'source', 'data', 'pilot', 'main', 'evaluator-code', 'published-sources', 'validation'}
        files = []
        for path in self.root.iterdir():
            if path.is_dir() and path.name in allowed_dirs:
                files.extend(p for p in path.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
            elif path.is_file() and (path.suffix in {'.json', '.jsonl', '.md', '.py', '.csv'} or path.name == 'model.toml'):
                files.append(path)
        if vectors and (self.root / 'vectors.sqlite3').exists():
            files.append(self.root / 'vectors.sqlite3')
        changed, identity = [], {}
        for path in sorted(files):
            relative = str(path.relative_to(self.root))
            stamp = (path.stat().st_size, path.stat().st_mtime_ns)
            if self.sent.get(relative) != stamp:
                changed.append(path)
        if not changed:
            return
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            for path in changed:
                relative = str(path.relative_to(self.root))
                # Monitoring may atomically replace a file during publication.
                # Hash and archive the same bytes from one opened file version.
                with path.open('rb') as stream:
                    snapshot_stat = os.fstat(stream.fileno())
                    snapshot = stream.read()
                info = tarfile.TarInfo(relative)
                info.size = len(snapshot)
                info.mtime = snapshot_stat.st_mtime
                info.mode = snapshot_stat.st_mode & 0o777
                archive.addfile(info, io.BytesIO(snapshot))
                identity[relative] = {
                    'sha256': hashlib.sha256(snapshot).hexdigest(),
                    'stamp': (len(snapshot), snapshot_stat.st_mtime_ns),
                }
            hashes = {name: item['sha256'] for name, item in identity.items()}
            manifest = json.dumps(hashes).encode()
            info = tarfile.TarInfo('.sync-hashes.json')
            info.size = len(manifest)
            archive.addfile(info, io.BytesIO(manifest))
        program = f"""import sys,os,tarfile,pathlib,json,hashlib,tempfile,shutil
os.umask(0o077)
root=pathlib.Path.home()/'.local/share/akashic-eval/experiments/{self.root.name}'
stage=pathlib.Path(tempfile.mkdtemp(prefix='.incoming-',dir=root))
try:
 with tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz') as archive:archive.extractall(stage,filter='data')
 expected=json.loads((stage/'.sync-hashes.json').read_text())
 for name,sha in expected.items():
  incoming=stage/name
  if hashlib.sha256(incoming.read_bytes()).hexdigest()!=sha:raise ValueError('checksum mismatch: '+name)
  dest=root/name
  if dest.exists() and dest.suffix=='.jsonl':
   old=dest.read_bytes()
   if not incoming.read_bytes().startswith(old):raise ValueError('refusing non-append journal update: '+name)
 for name in expected:
  dest=root/name;dest.parent.mkdir(parents=True,exist_ok=True);(stage/name).replace(dest)
 print(json.dumps({{'root':str(root),'verified_files':len(expected)}}))
finally:shutil.rmtree(stage)
"""
        encoded = base64.b64encode(program.encode()).decode()
        command = 'wsl.exe --distribution Ubuntu-24.04 --exec python3 -c "exec(__import__(\'base64\').b64decode(\'' + encoded + '\'))"'
        # Keep the manifest in the archive so Windows' command limit is constant.
        ssh = ['ssh', '-i', '/Users/namei/.ssh/id_ed25519_akashic_windows',
               '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1', 'namei@100.98.48.39', command]
        result = subprocess.run(ssh, input=buffer.getvalue(), capture_output=True, timeout=180)
        if result.returncode:
            raise RuntimeError('WSL artifact sync failed: ' + result.stderr.decode(errors='replace')[-2000:])
        receipt = json.loads(result.stdout)
        append(self.root / 'storage-receipts.jsonl', {'at': time.time(), **receipt, 'files': hashes})
        self.sent.update({name: item['stamp'] for name, item in identity.items()})
        self.last_sync = time.monotonic()
