"""Every CSV the portal writes goes through `to_csv` (the T2 export sheets, winners.csv, voter-links.csv).

Formula injection: a text cell that starts with = + - @ tab or carriage return is prefixed with a
single quote, so a spreadsheet shows it instead of running it (a team named =HYPERLINK(...) stays
a name). Real numbers (int, float, Decimal values the portal computed) are written as numbers and
never prefixed: a negative score is -0.25, not the text '-0.25. Anything a person typed arrives
as text and is escaped. UTF-8 with a BOM, so Excel reads names with accents correctly.
"""

import csv
import io
import json
from decimal import Decimal

from django.http import HttpResponse

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def cell(value):
    """A value as it should appear in a spreadsheet cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if hasattr(value, "isoformat") and hasattr(value, "tzinfo"):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, int):
        return str(value)  # a number, even a negative one, is never a formula
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".") if value == value else ""
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(v) for v in value)
    if isinstance(value, dict):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    text = str(value)
    if text.startswith(FORMULA_START):
        return "'" + text
    return text


def to_csv(header, rows) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([cell(h) for h in header])
    for row in rows:
        writer.writerow([cell(v) for v in row])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def stamp(now):
    return now.strftime("%Y%m%d-%H%MZ")


def download(body, content_type, filename):
    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response
