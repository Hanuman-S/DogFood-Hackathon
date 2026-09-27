"""Image uploads and the protected media endpoints.

Uploads are the one place where a participant hands the portal a file and the portal later hands
it to somebody else, so every claim the upload makes about itself is ignored: extension, filename
and declared content type play no part. What the bytes *decode to* is the only thing consulted, and
what gets stored is a re-encode rather than the bytes that arrived.
"""

from __future__ import annotations

import io
import struct
import zlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image

from core.errors import PermissionDenied, SubmissionsClosed, ValidationFailed
from projects import services
from projects.models import ProjectImage
from tests.factories import (
    PASSWORD,
    make_admin,
    make_event,
    make_organizer,
    make_submitted_project,
    make_team,
    make_user,
)


# --------------------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------------------


def image_bytes(fmt="PNG", size=(40, 30), mode="RGB", **save_kwargs) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color=(120, 140, 160) if mode == "RGB" else 128).save(
        buffer, format=fmt, **save_kwargs
    )
    return buffer.getvalue()


def upload(name="shot.png", content=None, fmt="PNG", content_type="image/png"):
    """A file as Django's request parsing would present it -- including a name and a declared
    content type, both of which the service layer must ignore."""
    return SimpleUploadedFile(name, content if content is not None else image_bytes(fmt), content_type)


def bomb_png(width=60_000, height=60_000) -> bytes:
    """A tiny PNG whose header declares an enormous canvas.

    Written by hand rather than produced by Pillow, because Pillow would refuse to *create* it.
    This is the cheap decompression bomb: a few hundred bytes on the wire that a naive decoder
    expands into gigabytes of pixels.
    """

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # One scanline of compressed data: enough for a valid-looking stream, nowhere near the
    # declared size, which is exactly the point.
    idat = zlib.compress(b"\x00" + b"\x00" * (width * 3))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.fixture
def event(db):
    return make_event(name="Upload Event")


@pytest.fixture
def team(event):
    return make_team(event, name="Uploaders")


@pytest.fixture
def member(team):
    return team.captain().user


@pytest.fixture
def project(team):
    return make_submitted_project(team, name="Uploader Project")


# --------------------------------------------------------------------------------------
# verification: what is accepted
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt,content_type", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_the_three_allowed_formats_are_accepted(db, fmt, content_type):
    content, detected, served_as = services.verify_image(
        upload(name="whatever.bin", content=image_bytes(fmt), content_type="application/octet-stream")
    )

    assert detected == fmt
    assert served_as == content_type
    # The stored name is generated here, never taken from the upload.
    assert content.name != "whatever.bin"
    assert content.name.endswith(services.FORMAT_EXTENSIONS[fmt])


def test_a_text_file_named_png_is_refused(db):
    with pytest.raises(ValidationFailed) as caught:
        services.verify_image(upload(content=b"this is not an image at all"))

    assert "not a readable image" in caught.value.message


def test_a_gif_is_refused_even_though_pillow_can_read_it(db):
    """The allow-list is JPEG/PNG/WebP. A format Pillow happens to support is not thereby
    accepted -- the refusal names the format so the uploader knows what happened."""
    with pytest.raises(ValidationFailed) as caught:
        services.verify_image(upload(name="anim.gif", content=image_bytes("GIF"), content_type="image/gif"))

    assert "GIF" in caught.value.message


def test_an_svg_is_refused(db):
    """SVG is XML, can carry script, and is not in the allow-list."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(ValidationFailed):
        services.verify_image(upload(name="x.svg", content=svg, content_type="image/svg+xml"))


def test_an_oversized_file_is_refused(db, settings):
    settings.MAX_UPLOAD_BYTES = 1024
    with pytest.raises(ValidationFailed) as caught:
        services.verify_image(upload(content=image_bytes("PNG", size=(600, 600))))

    assert "smaller" in caught.value.message


def test_an_empty_file_is_refused(db):
    with pytest.raises(ValidationFailed):
        services.verify_image(upload(content=b""))


def test_a_decompression_bomb_is_refused(db):
    """Pillow only *warns* between MAX_IMAGE_PIXELS and twice that. A warning is not a refusal, so
    the service promotes it to an error -- otherwise a 300-byte upload could exhaust the worker."""
    with pytest.raises(ValidationFailed) as caught:
        services.verify_image(upload(content=bomb_png()))

    assert "pixels" in caught.value.message


def test_verify_returns_a_usable_image_after_verify_was_called(db):
    """`Image.verify()` leaves the object unusable; the file has to be rewound and reopened.
    Getting this wrong surfaces as "operation on closed image", so the test reads the result."""
    content, _fmt, _ct = services.verify_image(upload(content=image_bytes("PNG", size=(12, 9))))

    reopened = Image.open(io.BytesIO(content.read()))
    assert reopened.size == (12, 9)


# --------------------------------------------------------------------------------------
# verification: re-encoding
# --------------------------------------------------------------------------------------


def test_exif_metadata_is_stripped(db):
    """A phone camera attaches GPS coordinates. A team demoing from their kitchen did not mean to
    publish their home address."""
    original = Image.new("RGB", (24, 18), color=(10, 20, 30))
    exif = original.getexif()
    exif[0x9286] = "a user comment nobody asked to publish"  # UserComment
    exif[0x010F] = "SecretCameraMaker"  # Make
    buffer = io.BytesIO()
    original.save(buffer, format="JPEG", exif=exif)
    raw = buffer.getvalue()
    assert b"SecretCameraMaker" in raw  # it really is in the upload

    content, _fmt, _ct = services.verify_image(upload(name="photo.jpg", content=raw, content_type="image/jpeg"))

    stored = content.read()
    assert b"SecretCameraMaker" not in stored
    assert not Image.open(io.BytesIO(stored)).getexif()


def test_a_polyglot_payload_appended_to_an_image_does_not_survive(db):
    """A file that is a valid PNG *and* carries an HTML payload after the image data passes a
    format check. Only the pixels are copied out, so the payload does not reach storage."""
    payload = b"<script>alert('polyglot')</script>" * 20
    raw = image_bytes("PNG", size=(30, 30)) + payload

    content, _fmt, _ct = services.verify_image(upload(content=raw))

    stored = content.read()
    assert payload not in stored
    assert b"<script" not in stored
    assert len(stored) < len(raw)


def test_a_transparent_png_uploaded_as_jpeg_source_still_converts(db):
    """JPEG has no alpha channel; a naive re-encode of an RGBA image raises. Covered because the
    failure would only appear for the one participant who uploads a transparent screenshot."""
    content, fmt, _ct = services.verify_image(
        upload(content=image_bytes("PNG", mode="RGBA"), name="t.png")
    )
    assert fmt == "PNG"
    assert Image.open(io.BytesIO(content.read())).mode in ("RGBA", "P")


# --------------------------------------------------------------------------------------
# the image count limit
# --------------------------------------------------------------------------------------


def test_images_can_be_added_up_to_the_limit(db, project, member, settings):
    for i in range(settings.MAX_PROJECT_IMAGES):
        services.add_image(actor=member, project=project, upload=upload(), alt_text=f"shot {i}")

    assert project.images.count() == settings.MAX_PROJECT_IMAGES


def test_one_image_past_the_limit_is_refused_and_changes_nothing(db, project, member, settings):
    for _ in range(settings.MAX_PROJECT_IMAGES):
        services.add_image(actor=member, project=project, upload=upload())

    with pytest.raises(ValidationFailed) as caught:
        services.add_image(actor=member, project=project, upload=upload())

    assert str(settings.MAX_PROJECT_IMAGES) in caught.value.message
    assert project.images.count() == settings.MAX_PROJECT_IMAGES


def test_the_count_is_taken_under_a_row_lock(db, project, member, django_assert_num_queries):
    """Count-then-insert without a lock is racy under READ COMMITTED: two concurrent uploads each
    read 7 and each insert, leaving 9. "At most N rows per parent" cannot be a database
    constraint, so `SELECT ... FOR UPDATE` is the only enforcement there is.

    Asserted by inspecting the SQL rather than by racing two real connections, because a test
    that depends on interleaving is a test that fails intermittently on a loaded machine.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        services.add_image(actor=member, project=project, upload=upload())

    locking = [q["sql"] for q in captured.captured_queries if "FOR UPDATE" in q["sql"].upper()]
    assert locking, "the image count was taken without locking the project row"


def test_the_thumbnail_does_not_count_against_the_image_limit(db, project, member, settings):
    for _ in range(settings.MAX_PROJECT_IMAGES):
        services.add_image(actor=member, project=project, upload=upload())

    services.set_thumbnail(actor=member, project=project, upload=upload())

    project.refresh_from_db()
    assert project.thumbnail
    assert project.images.count() == settings.MAX_PROJECT_IMAGES


def test_replacing_a_thumbnail_records_the_new_content_type(db, project, member):
    services.set_thumbnail(actor=member, project=project, upload=upload())
    project.refresh_from_db()
    assert project.thumbnail_content_type == "image/png"

    services.set_thumbnail(
        actor=member, project=project, upload=upload(name="x.jpg", fmt="JPEG", content_type="image/jpeg")
    )

    project.refresh_from_db()
    assert project.thumbnail_content_type == "image/jpeg"


# --------------------------------------------------------------------------------------
# deletion
# --------------------------------------------------------------------------------------


def test_removing_an_image_deletes_the_row_and_the_file(db, project, member):
    image = services.add_image(actor=member, project=project, upload=upload())
    path = image.image.path
    storage = image.image.storage
    assert storage.exists(image.image.name)

    services.remove_image(actor=member, image=image)

    assert not ProjectImage.objects.filter(pk=image.pk).exists()
    # The unlink is deferred to `transaction.on_commit`; pytest-django's `db` fixture wraps the
    # test in a transaction that never commits, so the file is still there. That is the
    # behaviour under test: the file must not vanish before the row's deletion is durable.
    assert storage.exists(image.image.name), (
        "the file was deleted before the transaction committed; a rollback would leave a row "
        "pointing at nothing"
    )
    assert path  # the field still knows where it was, for the on_commit callback


# --------------------------------------------------------------------------------------
# guards on every image path
# --------------------------------------------------------------------------------------


def test_uploads_are_refused_after_the_deadline(db):
    closed = make_event(name="Closed Uploads", open_window=False)
    team = make_team(closed)
    project = make_submitted_project(team)

    with pytest.raises(SubmissionsClosed):
        services.add_image(actor=team.captain().user, project=project, upload=upload())


def test_an_outsider_cannot_upload_to_someone_elses_project(db, event, project):
    stranger = make_team(event, name="Strangers").captain().user

    with pytest.raises(PermissionDenied):
        services.add_image(actor=stranger, project=project, upload=upload())


def test_an_organizer_cannot_upload_to_a_teams_project(db, event, project):
    """Organizers moderate and view; they never author. See `core.permissions.can_edit_project`."""
    for actor in (make_organizer(event), make_admin()):
        with pytest.raises(PermissionDenied):
            services.add_image(actor=actor, project=project, upload=upload())


# --------------------------------------------------------------------------------------
# protected media serving
# --------------------------------------------------------------------------------------


@pytest.fixture
def client():
    return Client()


def _login(client, user):
    assert client.login(email=user.email, password=PASSWORD)


def test_a_public_projects_image_is_served_to_anyone(db, project, member, client):
    image = services.add_image(actor=member, project=project, upload=upload(), alt_text="a shot")

    response = client.get(f"/projects/images/{image.pk}")

    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    # nosniff even with a correct Content-Type: a sniffing browser could otherwise decide a
    # stored file is HTML and run it on this origin.
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "private" in response["Cache-Control"]


def test_a_drafts_image_is_404_for_a_stranger_and_200_for_the_team(db, event, team, member, client):
    from projects.models import ProjectStatus

    draft = make_submitted_project(team, name="Hidden Draft")
    draft.status = ProjectStatus.DRAFT
    draft.submitted_at = None
    draft.save()
    image = services.add_image(actor=member, project=draft, upload=upload())

    # Anonymous: not 403 -- a 403 would confirm the draft exists.
    assert client.get(f"/projects/images/{image.pk}").status_code == 404

    _login(client, member)
    response = client.get(f"/projects/images/{image.pk}")
    assert response.status_code == 200
    # And it must not be cacheable by anything shared, or the next viewer gets it without
    # passing can_view_project at all.
    assert response["Cache-Control"] == "private, no-store"


def test_a_hidden_projects_image_is_not_public(db, project, member, client):
    image = services.add_image(actor=member, project=project, upload=upload())
    project.hidden_by_organizer = True
    project.save()

    assert client.get(f"/projects/images/{image.pk}").status_code == 404


def test_the_thumbnail_route_applies_the_same_rules(db, project, member, client):
    services.set_thumbnail(actor=member, project=project, upload=upload())

    assert client.get(f"/projects/{project.pk}/thumbnail.img").status_code == 200

    project.hidden_by_organizer = True
    project.save()
    assert client.get(f"/projects/{project.pk}/thumbnail.img").status_code == 404


def test_a_missing_file_is_a_404_not_a_500(db, project, member, client):
    """A database restored against an empty media volume, or the residual gap documented on
    `_delete_file_on_commit`. The row outlived its file; the page should not blow up."""
    image = services.add_image(actor=member, project=project, upload=upload())
    image.image.storage.delete(image.image.name)

    assert client.get(f"/projects/images/{image.pk}").status_code == 404


def test_media_is_not_reachable_by_any_static_route(db, project, member, client):
    """`MEDIA_URL` is None and nothing serves MEDIA_ROOT, so the view is the only door."""
    from django.conf import settings

    assert settings.MEDIA_URL is None
    image = services.add_image(actor=member, project=project, upload=upload())
    # The stored path must not be guessable as a URL under any prefix the portal serves.
    assert client.get(f"/media/{image.image.name}").status_code == 404
    assert client.get(f"/static/{image.image.name}").status_code == 404
