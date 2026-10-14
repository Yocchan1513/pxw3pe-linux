"""Minimal pure-Python DES (FIPS 46-3) in ECB mode.

Only what is needed to test the "w3pe" card-descramble hypothesis:
single-block encrypt/decrypt with a 64-bit key (parity bits ignored).
No dependencies beyond the standard library. Not constant-time and slow
(roughly tens of thousands of blocks per second) -- analysis use only.

Verified by selftest.py against published known-answer vectors
(e.g. key 133457799BBCDFF1, pt 0123456789ABCDEF -> ct 85E813540F0AB405;
FIPS 81 key 0123456789ABCDEF, pt "Now is t" -> 3FA40E8A984D4815).
"""

# --- Standard DES tables (1-based bit positions, MSB = bit 1) -------------

_IP = (
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
)

_FP = (
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25,
)

_E = (
    32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9,
    8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
    16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25,
    24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1,
)

_P = (
    16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
    2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25,
)

_PC1 = (
    57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18,
    10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22,
    14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4,
)

_PC2 = (
    14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10,
    23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
    41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48,
    44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32,
)

_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)

_SBOX = (
    (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
     0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
     15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
     3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
     13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
     13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
     1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
     13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
     3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
     14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
     11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
     10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
     4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
     13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
     6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
     1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
     2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
)


def _permute(value, table, in_bits):
    """Apply a DES permutation table. Bit 1 is the MSB of an in_bits value."""
    out = 0
    for pos in table:
        out = (out << 1) | ((value >> (in_bits - pos)) & 1)
    return out


# Pre-combine S-box and P permutation: for S-box i and 6-bit input,
# the 32-bit P-permuted contribution. Speeds up the round function a lot.
def _build_sp():
    sp = []
    for i, box in enumerate(_SBOX):
        table = []
        for six in range(64):
            row = ((six >> 4) & 2) | (six & 1)
            col = (six >> 1) & 0xF
            s = box[row * 16 + col]
            table.append(_permute(s << (28 - 4 * i), _P, 32))
        sp.append(tuple(table))
    return tuple(sp)


_SP = _build_sp()


def key_schedule(key):
    """Return the 16 48-bit round keys for an 8-byte key (parity ignored)."""
    if len(key) != 8:
        raise ValueError("DES key must be 8 bytes")
    k56 = _permute(int.from_bytes(key, "big"), _PC1, 64)
    c, d = k56 >> 28, k56 & 0x0FFFFFFF
    subkeys = []
    for s in _SHIFTS:
        c = ((c << s) | (c >> (28 - s))) & 0x0FFFFFFF
        d = ((d << s) | (d >> (28 - s))) & 0x0FFFFFFF
        subkeys.append(_permute((c << 28) | d, _PC2, 56))
    return tuple(subkeys)


def _feistel(r, k):
    x = _permute(r, _E, 32) ^ k
    out = 0
    for i in range(8):
        out |= _SP[i][(x >> (42 - 6 * i)) & 0x3F]
    return out


def _crypt_block(block, subkeys):
    v = _permute(int.from_bytes(block, "big"), _IP, 64)
    left, right = v >> 32, v & 0xFFFFFFFF
    for k in subkeys:
        left, right = right, left ^ _feistel(right, k)
    # Final swap (R16 L16) then inverse initial permutation.
    v = _permute((right << 32) | left, _FP, 64)
    return v.to_bytes(8, "big")


class DES:
    """Single-key DES, ECB, one 8-byte block at a time."""

    def __init__(self, key):
        self._enc = key_schedule(bytes(key))
        self._dec = tuple(reversed(self._enc))

    def encrypt_block(self, block):
        if len(block) != 8:
            raise ValueError("DES block must be 8 bytes")
        return _crypt_block(bytes(block), self._enc)

    def decrypt_block(self, block):
        if len(block) != 8:
            raise ValueError("DES block must be 8 bytes")
        return _crypt_block(bytes(block), self._dec)


# Published known-answer vectors: (key, plaintext, ciphertext), all hex.
TEST_VECTORS = (
    # Classic worked example (J. Orlin Grabbe, "The DES Algorithm Illustrated").
    ("133457799BBCDFF1", "0123456789ABCDEF", "85E813540F0AB405"),
    ("0E329232EA6D0D73", "8787878787878787", "0000000000000000"),
    # FIPS 81 Appendix B (ECB), first block of "Now is the time for all ".
    ("0123456789ABCDEF", "4E6F772069732074", "3FA40E8A984D4815"),
    # NIST SP 800-17 variable plaintext / variable key KATs (first entries).
    ("0101010101010101", "8000000000000000", "95F8A5E5DD31D900"),
    ("8001010101010101", "0000000000000000", "95A8D72813DAA94D"),
)
