"""Checking an event bundle before anything is written (C1b). The import (C1c) writes only what this
returns; every refusal here is a 400 with a code and a reason in words, audited, and leaves nothing
behind but its audit row.

Order: who may import (403, not a bundle problem), then the file itself, from the outside in -- its
size; that it is a zip; how many entries; each entry's name (no `..`, no absolute or drive paths,
no backslashes or NUL, NFC, no symlinks, no directories, only the three kinds of file a bundle has);
each entry's declared size and compression ratio, all before a byte is decompressed; then every
entry read in chunks with a hard cap, so a zip that lies about its sizes cannot inflate past them;
the manifest (format, version, exactly the other files, every sha256); event.json's shape
(imports/bundle_schema.py); a vote still running with ballots in it; and every image through the
same re-encoding pipeline as an upload.
"""

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from core import audit
from core.deadlines import db_now
from core.models import AuditAction

from . import bundle_schema
from .bundle import FORMAT, VERSION, BundleError

ALLOWED_NAME = re.compile(r"manifest\.json|event\.json|media/[0-9a-f]{64}\.(jpg|png|webp)")
MAX_NAME = 200
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_RATIO = 100
CHUNK = 64 * 1024


class ImportForbidden(Exception):
    """Not a bundle problem: this account may not import events at all."""

    status = 403
    code = "forbidden"
    detail = "Only platform admins and accounts that may create events can import one."


@dataclass
class ValidatedBundle:
    sha256: str
    manifest: dict
    body: dict
    images: dict = field(default_factory=dict)  # media path -> ContentFile, re-encoded


def may_import(user):
    return user is not None and user.is_authenticated and (user.is_platform_admin or user.can_create_events)


def save_upload(uploaded, limit=None):
    """Copy an uploaded file to a temporary file, stopping (400) the moment it passes the size cap:
    a huge upload is never held whole in memory or on disk. Returns the path; the caller deletes it."""
    limit = limit or settings.BUNDLE_MAX_BYTES
    handle, path = tempfile.mkstemp(prefix="dogfood-import-", suffix=".zip")
    written = 0
    try:
        with os.fdopen(handle, "wb") as out:
            for chunk in uploaded.chunks(CHUNK):
                written += len(chunk)
                if written > limit:
                    raise BundleError("bundle_too_large", f"the upload is over {limit} bytes")
                out.write(chunk)
    except BaseException:
        os.unlink(path)
        raise
    return path


def validate(path, *, actor, origin=None):
    """The bundle at `path`, checked end to end, or a refusal (audited). Writes nothing else."""
    if not may_import(actor):
        audit.record(AuditAction.EVENT_IMPORT_REFUSED, origin=origin, actor=actor, reason="forbidden")
        raise ImportForbidden(ImportForbidden.detail)
    try:
        return _validate(path)
    except BundleError as error:
        audit.record(AuditAction.EVENT_IMPORT_REFUSED, origin=origin, actor=actor, reason=error.code,
                     detail_text=error.detail[:300])
        raise


def _refuse(code, detail):
    raise BundleError(code, detail)


def _validate(path):
    size = os.path.getsize(path)
    if size > settings.BUNDLE_MAX_BYTES:
        _refuse("bundle_too_large", f"the bundle is {size} bytes; at most {settings.BUNDLE_MAX_BYTES}")
    if not zipfile.is_zipfile(path):
        _refuse("not_a_zip", "the file is not a zip archive")
    with open(path, "rb") as fh:
        digest = hashlib.file_digest(fh, "sha256").hexdigest()
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            _check_entries(infos)
            files = {info.filename: _read(archive, info) for info in infos}
    except zipfile.BadZipFile as error:
        _refuse("corrupt_zip", f"the zip is damaged: {error}")
    except (zipfile.LargeZipFile, NotImplementedError, RuntimeError, EOFError, OSError) as error:
        _refuse("corrupt_zip", f"the zip cannot be read: {error}")

    manifest = _json(files, "manifest.json")
    _check_manifest(manifest, files)
    body = _json(files, "event.json")
    media = {name: data for name, data in files.items() if name.startswith("media/")}
    try:
        bundle_schema.check(body, media)
    except bundle_schema.SchemaError as error:
        _refuse("invalid_bundle", str(error))
    _check_vote(body)
    return ValidatedBundle(sha256=digest, manifest=manifest, body=body, images=_clean_images(media))


def _check_entries(infos):
    if len(infos) > settings.BUNDLE_MAX_ENTRIES:
        _refuse("too_many_files", f"{len(infos)} entries; at most {settings.BUNDLE_MAX_ENTRIES}")
    seen, total = set(), 0
    for info in infos:
        name = info.orig_filename
        shown = repr(name[:80])
        if "\x00" in name or name != info.filename:
            _refuse("bad_path", f"entry {shown} has a NUL byte or a name the zip library had to change")
        if len(name) > MAX_NAME or unicodedata.normalize("NFC", name) != name:
            _refuse("bad_path", f"entry {shown} is too long or not in Unicode NFC form")
        if name.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", name) or "\\" in name:
            _refuse("bad_path", f"entry {shown} is an absolute path or uses backslashes")
        if ".." in name.split("/"):
            _refuse("bad_path", f"entry {shown} climbs out of the bundle (..)")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            _refuse("bad_path", f"entry {shown} is a symbolic link")
        if info.is_dir() or name.endswith("/"):
            _refuse("bad_path", f"entry {shown} is a directory; a bundle holds only files")
        if not ALLOWED_NAME.fullmatch(name):
            _refuse("unexpected_file", f"entry {shown} is not manifest.json, event.json or media/<sha256>.<ext>")
        if name in seen:
            _refuse("bad_path", f"entry {shown} appears twice")
        seen.add(name)
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            _refuse("corrupt_zip", f"entry {shown} uses an unsupported compression method")
        if info.flag_bits & 0x1:
            _refuse("corrupt_zip", f"entry {shown} is encrypted")
        cap = {"manifest.json": MAX_MANIFEST_BYTES, "event.json": settings.BUNDLE_MAX_EVENT_JSON_BYTES}.get(
            name, settings.BUNDLE_MAX_MEDIA_BYTES)
        if info.file_size > cap:
            _refuse("entry_too_large", f"entry {shown} is {info.file_size} bytes; at most {cap}")
        if info.file_size and (info.compress_size == 0 or info.file_size / info.compress_size > MAX_RATIO):
            _refuse("zip_bomb", f"entry {shown} expands more than {MAX_RATIO} times")
        total += info.file_size
    if total > settings.BUNDLE_MAX_TOTAL_BYTES:
        _refuse("bundle_too_large", f"{total} bytes uncompressed; at most {settings.BUNDLE_MAX_TOTAL_BYTES}")
    for required in ("manifest.json", "event.json"):
        if required not in seen:
            _refuse("invalid_bundle", f"{required} is missing")


def _read(archive, info):
    """Read one entry in chunks, never more than its declared size (the zip library also checks the
    CRC at the end, so a lie about the size or the content is caught either way)."""
    parts, read = [], 0
    with archive.open(info) as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            read += len(chunk)
            if read > info.file_size:
                _refuse("zip_bomb", f"entry {info.filename!r} holds more than the {info.file_size} bytes it declares")
            parts.append(chunk)
    return b"".join(parts)


def _json(files, name):
    try:
        value = json.loads(files[name].decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        _refuse("invalid_bundle", f"{name} is not valid UTF-8 JSON: {str(error)[:120]}")
    return value


def _check_manifest(manifest, files):
    if not isinstance(manifest, dict):
        _refuse("invalid_bundle", "manifest.json is not an object")
    if manifest.get("format") != FORMAT:
        _refuse("unknown_format", f"format {manifest.get('format')!r} is not {FORMAT!r}")
    if manifest.get("version") != VERSION:
        _refuse("unsupported_version", f"version {manifest.get('version')!r}; this install reads version {VERSION}")
    listed = manifest.get("files")
    if not isinstance(listed, dict):
        _refuse("invalid_bundle", "manifest.json has no files list")
    others = set(files) - {"manifest.json"}
    if set(listed) != others:
        extra, missing = sorted(others - set(listed)), sorted(set(listed) - others)
        _refuse("files_mismatch", f"the manifest does not list exactly the bundle's files "
                f"(not listed: {extra[:3]}, listed but absent: {missing[:3]})")
    for name in sorted(others):
        actual = hashlib.sha256(files[name]).hexdigest()
        if listed[name] != actual:
            _refuse("checksum_mismatch", f"{name}'s sha256 does not match the manifest")
        if name.startswith("media/") and not name.startswith(f"media/{actual}."):
            _refuse("checksum_mismatch", f"{name} is not named after its own sha256")


def _check_vote(body):
    """A vote still running, with ballots in it, cannot move: its ballots are pseudonymised, so the
    new install could not stop the same people voting again."""
    config, ballots = body.get("voting_config"), body.get("ballots") or []
    if config and ballots and datetime.fromisoformat(config["closes_at"]) > db_now():
        _refuse("voting_in_progress", "this bundle's community vote is still open and has ballots. "
                "End voting on the source install (\"End voting now\" on the event's voting page), "
                "then export the event again.")


def _clean_images(media):
    from projects.images import clean_image

    cleaned = {}
    for name, data in sorted(media.items()):
        try:
            cleaned[name] = clean_image(ContentFile(data, name=name.rsplit("/", 1)[-1]))
        except ValidationError as error:
            _refuse("bad_image", f"{name}: {' '.join(error.messages)}")
    return cleaned
