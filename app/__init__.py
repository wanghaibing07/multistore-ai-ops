"""MultiStore local merchant workbench."""
from pathlib import Path
import os

# Local file contains only this app's settings; existing process environment wins.
_config = Path(__file__).resolve().parent.parent / ".env"
_allowed = {"MULTISTORE_MODEL_URL", "MULTISTORE_MODEL_API_KEY", "MULTISTORE_MODEL_NAME", "MULTISTORE_SECURE_COOKIE", "MULTISTORE_DATA_DIR", "MULTISTORE_DATABASE_URL", "MULTISTORE_PUBLIC_MODE", "MULTISTORE_QIANNIU_URL", "MULTISTORE_DOWNLOADS_DIR"}
if _config.is_file():
    for _line in _config.read_text(encoding="utf-8-sig").splitlines():
        _key, _sep, _value = _line.partition("=")
        if _sep and _key.strip() in _allowed:
            os.environ.setdefault(_key.strip(), _value.strip().strip('"').strip("'"))
