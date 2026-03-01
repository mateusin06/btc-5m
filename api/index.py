# Entrypoint para Vercel (FastAPI)
# A Vercel procura app em api/index.py, api/app.py, etc.
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from web import app
