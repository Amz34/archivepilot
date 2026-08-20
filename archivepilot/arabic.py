"""Arabic-aware text normalization for search.

Arabic script has many "same word, different spelling" traps:
diacritics (tashkeel), multiple alef forms, teh marbuta, alef maqsura
and hamza variants. We normalize both stored text and queries so
search matches the way people actually write.
"""

import re

_DIACRITICS = "[\u064B-\u0652\u0670\u0640\u06D6-\u06ED]"
_ALEFS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})
_HAMZA = str.maketrans({"ؤ": "و", "ئ": "ي", "ء": ""})
_TAIL = str.maketrans({"ة": "ه", "ى": "ي"})
# Strip the definite article ال- so "الموقع" and "موقع" both match,
# but only when at least 3 letters remain (keeps الله and friends intact).
_AL_DEFINITE = re.compile(r"(?<!\S)ال(?=\S{3,})")


def normalize(text: str) -> str:
    """Normalize Arabic text for consistent matching."""
    if not text:
        return ""
    t = text.translate(_ALEFS).translate(_HAMZA).translate(_TAIL)
    t = re.sub(_DIACRITICS, "", t)
    t = _AL_DEFINITE.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def has_arabic(text: str) -> bool:
    """True if the text contains Arabic script characters."""
    return bool(re.search(r"[\u0600-\u06FF]", text or ""))
