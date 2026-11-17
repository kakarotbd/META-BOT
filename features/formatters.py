"""Small formatting and attachment helpers shared by bot features."""


MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]


def image_extension(content_type, data=None):
    """Return an attachment extension that matches the stored image MIME type."""
    # Prefer the actual GIF signature for legacy records with a wrong MIME value.
    if data and bytes(data[:6]) in (b"GIF87a", b"GIF89a"):
        return "gif"
    return {
        "image/gif": "gif",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }.get((content_type or "").lower(), "jpg")


def format_birthday(record):
    """Render a saved {day, month, year} birthday record as e.g. 'March 25' or 'March 25, 2000'."""
    month_index = int(record.get("month", 0))
    month_name = MONTH_NAMES[month_index - 1] if 1 <= month_index <= 12 else "?"
    year = record.get("year")
    return f"{month_name} {record.get('day')}" + (f", {year}" if year else "")
