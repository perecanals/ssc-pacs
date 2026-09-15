"""Safe, portable download filenames for table and imaging exports."""

import re
import unicodedata
from urllib.parse import quote


def safe_name(value):
    value = unicodedata.normalize("NFC", str(value))
    value = re.sub(r"[^\w.\-]", "_", value).strip("._")
    return value.encode("utf-8")[:180].decode("utf-8", errors="ignore") or "series"


def content_disposition(filename):
    fallback = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", fallback).strip(".") or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"
