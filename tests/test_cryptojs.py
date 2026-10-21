"""The password leaves encrypted exactly as the site's login page encrypts it."""

import pytest

from robotarium._cryptojs import encrypt_password

#: CryptoJS.AES.encrypt(p, Utf8.parse("robotarium-secret-key"), {iv: zero, mode: CBC,
#: padding: Pkcs7}).toString(), from crypto-js 4.2.0 under node -- the call the site's login page
#: makes. The 21-byte key puts CryptoJS on a key schedule no standard AES has, so these can only
#: come from CryptoJS itself. The lengths straddle the block size, and one is not ASCII.
#: (Generated for robosheaf's test suite, where this port was first checked.)
CRYPTOJS_VECTORS = [
    ("", "PBNUqQ9Nirbyf3Uup10M7A=="),
    ("a", "/3LOedFN8yQtp38pN+4XsA=="),
    ("password", "m9J8xx+zU299czZMeX6R/g=="),
    ("0123456789abcdef", "u2E7M9OzcsJwCPym1sVF9k00USGH0rNNaNoxglabgSw="),
    ("0123456789abcdef0", "u2E7M9OzcsJwCPym1sVF9iGKAbVQIxSEnYSJLhIfQio="),
    ("hunter2!", "1njYGaK2Mpn1kPhA1lwiNA=="),
    ("pässwörd ✓ ünïcode", "j0iY2Pt4Vjwfs7O1XMeCnJGZ/fzx+FrU8AFFZKaz0MA="),
    ("x" * 47, "SiT5M5VPqf7hjmudBn1PoIex2F+AVgrGX2oTvhs7mufVTtlw58K9qsWTNJN1nKmy"),
]


@pytest.mark.parametrize("password, expected", CRYPTOJS_VECTORS)
def test_the_password_is_encrypted_exactly_as_cryptojs_does(password, expected):
    assert encrypt_password(password) == expected
