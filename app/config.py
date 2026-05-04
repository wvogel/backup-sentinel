from __future__ import annotations

import os
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("BSENTINEL_DATA_DIR", BASE_DIR / "data"))
REPORT_DIR = Path(os.getenv("BSENTINEL_REPORT_DIR", BASE_DIR / "reports"))
DATABASE_URL = os.getenv(
    "BSENTINEL_DATABASE_URL",
    "postgresql://backup_reports:backup_reports@postgres:5432/backup_reports",
)
_raw_app_url = os.getenv("APP_URL", "")
APP_BASE_URL = _raw_app_url.replace("http://", "https://", 1) if _raw_app_url.startswith("http://") else _raw_app_url
DEFAULT_TIMEZONE = os.getenv("BSENTINEL_DEFAULT_TIMEZONE", "Europe/Berlin")
APP_VERSION = "v2.2.2"
LOGOUT_URL = os.getenv("BSENTINEL_LOGOUT_URL", "/oauth2/sign_out")
BRAND_LOGO_LIGHT_URL = os.getenv("BSENTINEL_BRAND_LOGO_LIGHT_URL") or ""
BRAND_LOGO_DARK_URL = os.getenv("BSENTINEL_BRAND_LOGO_DARK_URL") or ""

FOOTER_LINKS = os.getenv("FOOTER_LINKS", "")
COPYRIGHT_TEXT = os.getenv("COPYRIGHT_TEXT", "")
INSECURE_SSL = os.getenv("BSENTINEL_INSECURE_SSL", "").lower() in ("1", "true", "yes", "on")
SYNC_INTERVAL_MINUTES = int(os.getenv("BSENTINEL_SYNC_INTERVAL_MINUTES", "60"))
API_TIMEOUT = float(os.getenv("BSENTINEL_API_TIMEOUT", "60"))
DEBUG = os.getenv("BSENTINEL_DEBUG", "").lower() in ("1", "true", "yes", "on")
# BSENTINEL_SECRET_KEY: Fernet-Key für verschlüsselte Secrets in der DB.
# Generieren mit: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
SECRET_KEY = os.getenv("BSENTINEL_SECRET_KEY", "")

# PDF report language (de / en). Falls back to "de" for any other value.
REPORT_LANGUAGE = os.getenv("BSENTINEL_REPORT_LANGUAGE", "de").lower()
if REPORT_LANGUAGE not in ("de", "en"):
    REPORT_LANGUAGE = "de"

# Sparkline / report "backup day" boundary offset in hours.
# Backup jobs routinely start in the evening and can run well into the next
# morning — especially when the job contains many VMs that are backed up
# sequentially. A single "backup day" should cover that whole run.
#
# With the default of 12h, the "backup day" runs from 12:00 noon to 12:00
# noon the next day. A job starting at 20:00 and ending at 09:30 the next
# morning is entirely grouped with the evening it started.
#
# Set to 0 for strict wall-clock midnight.
BACKUP_DAY_OFFSET_HOURS = int(os.getenv("BSENTINEL_BACKUP_DAY_OFFSET_HOURS", "12"))

CACHE_BUSTER = str(int(time.time()))
