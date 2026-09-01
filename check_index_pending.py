from __future__ import annotations

import json

from config import INTEGRITY_REPORT_PATH
from services.automation import pending_index_items


if __name__ == "__main__":
    status = pending_index_items(INTEGRITY_REPORT_PATH)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    raise SystemExit(0 if status["needs_update"] else 1)
