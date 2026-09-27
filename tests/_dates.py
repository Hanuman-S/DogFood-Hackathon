"""Posting dates the way the forms take them: a date box and a time box per field
(events.forms.UTCDateTimeWidget), named <field>_0 and <field>_1."""

from datetime import datetime


def dt_fields(name, when):
    """{"<name>_0": "YYYY-MM-DD", "<name>_1": "HH:MM"} for a datetime or a "YYYY-MM-DDTHH:MM" string.
    An empty string posts both boxes empty."""
    if when == "":
        return {f"{name}_0": "", f"{name}_1": ""}
    if isinstance(when, str):
        when = datetime.strptime(when, "%Y-%m-%dT%H:%M")
    return {f"{name}_0": when.strftime("%Y-%m-%d"), f"{name}_1": when.strftime("%H:%M")}
