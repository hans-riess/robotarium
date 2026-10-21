"""
The password, encrypted as the Robotarium's login page encrypts it before posting it.

The page runs CryptoJS.AES.encrypt(password, key, {iv, mode: CBC, padding: Pkcs7}) with a 21-byte
key, which is no AES key length; CryptoJS does not refuse it but runs its key schedule with a
fractional key size. No standard AES implementation reproduces that, and the server decrypts with
whatever CryptoJS made, so this is a port of CryptoJS's own algorithm (crypto-js 4.x, aes.js),
quirks included. tests/test_cryptojs.py pins it to vectors produced by crypto-js itself.

Ported unchanged from robosheaf's submit.py, where it was first worked out against the live site.
"""

import base64
import struct


#: What the login page encrypts the password with before posting it: CryptoJS AES, CBC, PKCS7,
#: a zero IV, and this key -- the bytes of the string, not a passphrase run through a KDF.
LOGIN_KEY = b"robotarium-secret-key"
LOGIN_IV = bytes(16)


def encrypt_password(password: str) -> str:
    """
    The password as the site's login page sends it: CryptoJS.AES.encrypt(password, key, {iv,
    mode: CBC, padding: Pkcs7}).toString(), which is the base64 of the bare ciphertext. With a
    21-byte key CryptoJS runs a key size of 21 / 4 = 5.25 words and 11.25 rounds.

    Args:
        password (str): The password in the clear.
    """
    plaintext = password.encode("utf-8")
    padding = 16 - len(plaintext) % 16
    plaintext += bytes([padding]) * padding

    key_schedule, rounds = _cryptojs_key_schedule(LOGIN_KEY)

    ciphertext = b""
    previous = LOGIN_IV
    for offset in range(0, len(plaintext), 16):
        block = bytes(a ^ b for a, b in zip(plaintext[offset:offset + 16], previous))
        previous = _cryptojs_encrypt_block(block, key_schedule, rounds)
        ciphertext += previous

    return base64.b64encode(ciphertext).decode("ascii")


def _cryptojs_tables():
    """CryptoJS's S-box and encryption T-tables, generated the way aes.js generates them."""
    double = [(k << 1) if k < 128 else (k << 1) ^ 0x11B for k in range(256)]
    sbox = [0] * 256
    sub_mix = [[0] * 256 for _ in range(4)]

    x = xi = 0
    for _ in range(256):
        sx = xi ^ (xi << 1) ^ (xi << 2) ^ (xi << 3) ^ (xi << 4)
        sx = (sx >> 8) ^ (sx & 0xFF) ^ 0x63
        sbox[x] = sx

        x2 = double[x]
        x4 = double[x2]
        x8 = double[x4]

        t = ((double[sx] * 0x101) ^ (sx * 0x1010100)) & 0xFFFFFFFF
        sub_mix[0][x] = ((t << 24) | (t >> 8)) & 0xFFFFFFFF
        sub_mix[1][x] = ((t << 16) | (t >> 16)) & 0xFFFFFFFF
        sub_mix[2][x] = ((t << 8) | (t >> 24)) & 0xFFFFFFFF
        sub_mix[3][x] = t

        if not x:
            x = xi = 1
        else:
            x = x2 ^ double[double[double[x8 ^ x2]]]
            xi ^= double[double[xi]]

    return sbox, sub_mix


_SBOX, _SUB_MIX = _cryptojs_tables()

#: aes.js's RCON.
_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _at(array: list, index) -> int:
    """
    array[index] as JavaScript reads it under a bitwise operator: a fractional or missing index
    is undefined, and undefined is 0 to ^. With a 5.25-word key, aes.js reads both.
    """
    if index != int(index) or not 0 <= index < len(array):
        return 0
    return array[int(index)]


def _sub_word(word: int) -> int:
    return ((_SBOX[word >> 24] << 24) | (_SBOX[(word >> 16) & 0xFF] << 16)
            | (_SBOX[(word >> 8) & 0xFF] << 8) | _SBOX[word & 0xFF])


def _cryptojs_key_schedule(key: bytes):
    """
    aes.js's _doReset, for encryption: the key schedule and the round count, both computed from
    key.sigBytes / 4 exactly as CryptoJS computes them, fractional or not.

    Args:
        key (bytes): The key's bytes, as CryptoJS.enc.Utf8.parse would hold them.
    """
    words = list(struct.unpack(">%dI" % (-(-len(key) // 4)), key.ljust(-(-len(key) // 4) * 4,
                                                                        b"\0")))
    key_size = len(key) / 4
    rounds = key_size + 6
    rows = (rounds + 1) * 4

    schedule = []
    row = 0
    while row < rows:
        if row < key_size:
            schedule.append(words[row])
        else:
            t = schedule[row - 1]
            if not row % key_size:
                t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF
                t = _sub_word(t)
                t ^= _RCON[int(row / key_size)] << 24
            elif key_size > 6 and row % key_size == 4:
                t = _sub_word(t)
            schedule.append(_at(schedule, row - key_size) ^ t)
        row += 1

    return schedule, rounds


def _cryptojs_encrypt_block(block: bytes, schedule: list, rounds) -> bytes:
    """
    aes.js's _doCryptBlock, encrypting: T-table rounds while round < nRounds, then the final
    S-box round. A key-schedule word past the end reads as 0, as it does in JavaScript.

    Args:
        block (bytes): Sixteen bytes, already XORed with the CBC chain.
        schedule (list): From _cryptojs_key_schedule.
        rounds: The round count, from _cryptojs_key_schedule; possibly fractional.
    """
    s0, s1, s2, s3 = struct.unpack(">4I", block)
    s0 ^= _at(schedule, 0)
    s1 ^= _at(schedule, 1)
    s2 ^= _at(schedule, 2)
    s3 ^= _at(schedule, 3)

    m0, m1, m2, m3 = _SUB_MIX
    row = 4
    round_number = 1
    while round_number < rounds:
        t0 = m0[s0 >> 24] ^ m1[(s1 >> 16) & 0xFF] ^ m2[(s2 >> 8) & 0xFF] ^ m3[s3 & 0xFF] \
            ^ _at(schedule, row)
        t1 = m0[s1 >> 24] ^ m1[(s2 >> 16) & 0xFF] ^ m2[(s3 >> 8) & 0xFF] ^ m3[s0 & 0xFF] \
            ^ _at(schedule, row + 1)
        t2 = m0[s2 >> 24] ^ m1[(s3 >> 16) & 0xFF] ^ m2[(s0 >> 8) & 0xFF] ^ m3[s1 & 0xFF] \
            ^ _at(schedule, row + 2)
        t3 = m0[s3 >> 24] ^ m1[(s0 >> 16) & 0xFF] ^ m2[(s1 >> 8) & 0xFF] ^ m3[s2 & 0xFF] \
            ^ _at(schedule, row + 3)
        s0, s1, s2, s3 = t0, t1, t2, t3
        row += 4
        round_number += 1

    def final(a, b, c, d, offset):
        return ((_SBOX[a >> 24] << 24) | (_SBOX[(b >> 16) & 0xFF] << 16)
                | (_SBOX[(c >> 8) & 0xFF] << 8) | _SBOX[d & 0xFF]) ^ _at(schedule, offset)

    return struct.pack(">4I", final(s0, s1, s2, s3, row), final(s1, s2, s3, s0, row + 1),
                       final(s2, s3, s0, s1, row + 2), final(s3, s0, s1, s2, row + 3))
