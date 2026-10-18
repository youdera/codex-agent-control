"""Load an existing API key without writing it; add local telemetry; launch upstream MCP."""
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))

def main():
    if not os.environ.get('DEEPSEEK_API_KEY'):
        if os.name!='nt':raise SystemExit('Set DEEPSEEK_API_KEY in the local environment before starting the service.')
        try:
            import win32cred
            credential=win32cred.CredRead('codex-deepseek-mcp-api-key',win32cred.CRED_TYPE_GENERIC,0)
            raw=credential['CredentialBlob']
            os.environ['DEEPSEEK_API_KEY']=raw.decode('utf-16-le') if isinstance(raw,bytes) else raw
            credential=raw=None
        except Exception:raise SystemExit('Configure DEEPSEEK_API_KEY or the local Windows credential. No credential was printed.') from None
    os.environ['PYTHONUTF8']='1';os.environ['PYTHONIOENCODING']='utf-8'
    from telemetry import install
    install()
    from deepseek_mcp.server import main as server_main
    server_main()
if __name__=='__main__':main()
