"""Portable, non-secret settings. Runtime state always lives outside the source tree."""
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('DS_CC_HOME',str(Path.home()/'.deepseek-mcp'/'control-center'))).expanduser().resolve()
DB=DATA/'control.sqlite3'
PYTHON=Path(os.environ.get('DS_CC_MCP_PYTHON',sys.executable)).expanduser().resolve()
LAUNCHER=Path(os.environ.get('DS_CC_MCP_LAUNCHER',str(ROOT/'mcp_launcher.py'))).expanduser().resolve()
PORT=int(os.environ.get('DS_CC_PORT','8791'))
if not 1024<=PORT<=65535:raise ValueError('invalid DS_CC_PORT')
