"""Uploaded images are never stored as sent.

Each upload is:
1. size-checked before decoding (MAX_IMAGE_BYTES),
2. identified by Pillow from its bytes -- the extension and Content-Type are ignored,
3. refused if it is not JPEG, PNG, WebP or GIF, or if its pixel count could be a
   decompression bomb (checked from the header, before the pixels are decoded),
4. decoded, rotated upright, shrunk to at most 2400px on the long side, and
5. re-encoded from the pixels alone.

Step 5 is what matters for safety: re-encoding drops EXIF (including GPS positions from phone
photos) and any bytes smuggled after the image data, so a "polyglot" file that is both a valid
PNG and, say, an HTML page does not survive.
"""

from io import BytesIO

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SIDE = 2400
FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "png"}  # GIFs keep frame 1, as PNG


def clean_image(upload):
    if upload.size > settings.MAX_IMAGE_BYTES:
        raise ValidationError(f"Images must be under {settings.MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
    try:
        probe = Image.open(upload)
        fmt = probe.format
        if fmt not in FORMATS:
            raise ValidationError("Use a JPEG, PNG, WebP or GIF image.")
        if probe.width * probe.height > settings.MAX_IMAGE_PIXELS:
            raise ValidationError("That image has too many pixels.")
        probe.verify()  # catches truncated and corrupt files

        upload.seek(0)
        image = Image.open(upload)
        image = ImageOps.exif_transpose(image)
        image.thumbnail((MAX_SIDE, MAX_SIDE))
        out = BytesIO()
        if fmt == "JPEG":
            image.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
        elif fmt == "WEBP":
            image.save(out, "WEBP", quality=88)
        else:
            if image.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                image = image.convert("RGBA")
            image.save(out, "PNG", optimize=True)
    except ValidationError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError) as error:
        raise ValidationError("That file is not a readable image.") from error
    return ContentFile(out.getvalue(), name=f"upload.{FORMATS[fmt]}")
