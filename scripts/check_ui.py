"""Lint helper: syntax-check the web UI's JavaScript (no browser or Node needed)."""
import sys
from pathlib import Path

import esprima

app_js = Path(__file__).resolve().parent.parent / "legion_orch/server/static/app.js"
# esprima predates ES2020: neutralise ?? and ?. for parsing only.
try:
    esprima.parseScript(app_js.read_text().replace("??", "||").replace("?.", "."))
except esprima.Error as e:
    sys.exit(f"{app_js}: {e}")
print(f"{app_js.name}: JavaScript OK")
