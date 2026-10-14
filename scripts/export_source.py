"""Package only the explicitly reviewed files; never recursively bundle runtime data."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
subprocess.run([sys.executable,str(ROOT/'scripts/check_public_source.py')],check=True,cwd=ROOT)
files=json.loads((ROOT/'PUBLIC_FILES.json').read_text())['files']
out=ROOT/'dist/NovaNative-public-source.zip';out.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as archive:
    for relative in files:
        path=ROOT/relative
        if not path.resolve().is_relative_to(ROOT.resolve()):raise ValueError('Unsafe publication path')
        archive.write(path,'NovaNative/'+relative)
with zipfile.ZipFile(out) as archive:
    assert archive.testzip() is None
print(out)
print('SHA256 '+hashlib.sha256(out.read_bytes()).hexdigest())
