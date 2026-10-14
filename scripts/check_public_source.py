"""Check versioned source for common secret/state leaks without printing values."""
from pathlib import Path
import json
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
SENSITIVE_NAMES={'nova.db','nova_settings.json','nova_workspace.json','.nova_mcp_policy.json',
    'photon.json','photon-state.json','photon-status.json','notes.json','auth.json',
    'background-service-state.json','background-service.json'}
SENSITIVE_DIRS={'task-workspaces','workspace','media','backups','.git','node_modules','.dotnet',
    '.venv','.pipecat-venv','__pycache__','bin','obj','dist','app','kokoro','voices'}
RULES={
    'private-key':r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    'github-token':r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b',
    'provider-key':r'\bsk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{30,}\b',
    'aws-access-id':r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'slack-token':r'\bxox[baprs]-[A-Za-z0-9-]{20,}\b',
    'credential-url':r'https?://[^\s/@:]+:[^\s/@]+@',
    'encoded-jwt':r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b',
}


def paths():
    manifest=ROOT/'PUBLIC_FILES.json'
    reviewed=[ROOT/name for name in json.loads(manifest.read_text())['files']]
    if (ROOT/'.git').exists():
        result=subprocess.run(['git','-C',str(ROOT),'ls-files','-z'],capture_output=True,check=True)
        reviewed += [ROOT/name for name in result.stdout.decode().split('\0') if name]
    return sorted(set(reviewed))


def main():
    findings=[];count=0
    for path in paths():
        if not path.resolve().is_relative_to(ROOT.resolve()):
            findings.append({'file':'[outside repository]','rule':'unsafe-manifest-path'});continue
        relative=path.relative_to(ROOT);name=path.name.lower()
        if path.is_symlink() or getattr(path,'is_junction',lambda:False)():
            findings.append({'file':relative.as_posix(),'rule':'linked-file'});continue
        if not path.is_file():
            findings.append({'file':relative.as_posix(),'rule':'missing-manifest-file'});continue
        count+=1
        if name in SENSITIVE_NAMES or name=='.env' or (name.startswith('.env.') and name!='.env.example') or any(p.lower() in SENSITIVE_DIRS for p in relative.parts):
            findings.append({'file':relative.as_posix(),'rule':'private-runtime-state'})
        if path.suffix.lower() in {'.db','.sqlite','.sqlite3','.gguf','.onnx','.safetensors','.zip','.exe','.dll','.pfx','.p12','.pem','.key'}:
            findings.append({'file':relative.as_posix(),'rule':'binary-secret-or-model-artifact'})
        if path.suffix.lower() in {'.png','.ico'}:continue
        try:text=path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            findings.append({'file':relative.as_posix(),'rule':'unexpected-binary'});continue
        for rule,pattern in RULES.items():
            for match in re.finditer(pattern,text):
                findings.append({'file':relative.as_posix(),'rule':rule,'line':text.count('\n',0,match.start())+1})
    print(json.dumps({'checked_files':count,'findings':findings},indent=2))
    return 1 if findings else 0


if __name__=='__main__':sys.exit(main())
