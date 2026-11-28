from __future__ import annotations

import re


def term_key(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())
