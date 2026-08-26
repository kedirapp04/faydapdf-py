"""Gregorian → Ethiopian calendar date.

The resident (Server 5) and FaydaPass (Server 6) APIs return only the Gregorian
date of birth; the ID card/PDF also shows the Ethiopian-calendar date (the
"Amharic" birth date, rendered in the Amharic font). Server 4's API supplied it
ready-made; Servers 5/6 must compute it. Faithful port of faydapdf-railway's
gregorianToEthiopian + toEthiopianDateString — same algorithm, same DD/MM/YYYY
output, so the three servers render identically.
"""
import re

_DATE = re.compile(r"^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$")


def gregorian_to_ethiopian(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    a = (14 - gm) // 12
    y = gy + 4800 - a
    m = gm + 12 * a - 3
    jdn = (gd + (153 * m + 2) // 5 + 365 * y
           + y // 4 - y // 100 + y // 400 - 32045)
    EPOCH = 1723856
    diff = jdn - EPOCH
    r = ((diff % 1461) + 1461) % 1461
    n = (r % 365) + 365 * (r // 1460)
    year = 4 * (diff // 1461) + (r // 365) - (r // 1460)
    month = n // 30 + 1
    day = n % 30 + 1
    return year, month, day


def to_ethiopian_date(value) -> str:
    """"YYYY/MM/DD" (or -) Gregorian → "DD/MM/YYYY" Ethiopian, or "" if unparseable."""
    m = _DATE.match(str(value or "").strip())
    if not m:
        return ""
    gy, gm, gd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= gm <= 12 and 1 <= gd <= 31):
        return ""
    ey, em, ed = gregorian_to_ethiopian(gy, gm, gd)
    return f"{ed:02d}/{em:02d}/{ey}"
