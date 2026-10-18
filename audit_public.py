"""Scan the actual staged payload, never runtime files or credential stores."""
import json,re,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def git(*args):return subprocess.check_output(['git','-C',str(ROOT),*args])
def main():
    files=git('diff','--cached','--name-only','--diff-filter=ACM','-z').decode().split('\0');files=[x for x in files if x]
    if not files:raise SystemExit('No staged files to scan')
    problems=[]
    for file in files:
        data=git('show',':'+file)
        if any(part in {'deliveries','runner-logs','runtime','workspaces','backups','build','__pycache__'} for part in Path(file).parts) or file.endswith(('.sqlite3','.log','.local.json','.db','.pem','.key')):problems.append({'file':file,'reason':'runtime/private file'})
        if file.lower().endswith(('.png','.jpg','.webp')):continue
        text=data.decode('utf-8')
        checks={
          'API key value':r'\bsk-[A-Za-z0-9_-]{16,}\b',
          'private key':r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
          'GitHub token value':r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b',
          'local Windows account path':r'(?i)[A-Z]:[\\/]+Users[\\/]+[^\s\"\']+',
          'non-public personal email':r'\b[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
        }
        for reason,pattern in checks.items():
            if re.search(pattern,text):problems.append({'file':file,'reason':reason})
    print(json.dumps({'staged_files':len(files),'findings':problems},ensure_ascii=True))
    if problems:sys.exit(1)
if __name__=='__main__':main()
