#!/usr/bin/env python3
"""
Helper to load test RSA key consistently for all tests
"""

import os

def load_test_rsa_key():
    """Load the test RSA key from file with absolute path"""
    test_dir = os.path.dirname(os.path.abspath(__file__))
    key_path = os.path.join(test_dir, 'test_rsa_key.pem')

    with open(key_path, 'r') as f:
        private_key_pem = f.read().strip()

    # Verify key is not empty
    if not private_key_pem:
        raise RuntimeError(f"Test RSA key file is empty: {key_path}")

    return private_key_pem


def load_test_rsa_public_key():
    """Derive the SPKI public key that belongs to test_rsa_key.pem.

    A sign/verify round trip in a test must use ONE keypair. The repository
    public key (license_public_key.pem) is the production signing key, so it
    cannot verify a test-key signature.

    The public half is rebuilt from the private key with stdlib DER only: a
    PKCS#8 RSAPrivateKey carries the modulus and the public exponent, which is
    everything a SubjectPublicKeyInfo needs. This avoids introducing a crypto
    dependency the project does not otherwise have.
    """
    import base64

    pem = load_test_rsa_key()
    body = "".join(
        line for line in pem.replace("\r\n", "\n").splitlines()
        if line and not line.startswith("-----")
    )
    der = base64.b64decode(body)

    def read_tlv(buf, i):
        """Return (tag, content, next_index) for the DER value at ``i``."""
        tag = buf[i]
        i += 1
        first = buf[i]
        i += 1
        if first & 0x80:
            count = first & 0x7F
            length = int.from_bytes(buf[i:i + count], "big")
            i += count
        else:
            length = first
        return tag, buf[i:i + length], i + length

    def der_length(n):
        if n < 0x80:
            return bytes([n])
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return bytes([0x80 | len(raw)]) + raw

    def der_sequence(parts):
        inner = b"".join(parts)
        return b"\x30" + der_length(len(inner)) + inner

    def der_integer(raw):
        raw = raw.lstrip(b"\x00") or b"\x00"
        if raw[0] & 0x80:          # keep it positive
            raw = b"\x00" + raw
        return b"\x02" + der_length(len(raw)) + raw

    # PrivateKeyInfo ::= SEQUENCE { version, AlgorithmIdentifier, OCTET STRING }
    tag, outer, _ = read_tlv(der, 0)
    if tag != 0x30:
        raise RuntimeError("test key is not a DER SEQUENCE")
    tag, _, i = read_tlv(outer, 0)                  # version
    tag, _, i = read_tlv(outer, i)                  # privateKeyAlgorithm
    tag, inner, _ = read_tlv(outer, i)              # privateKey OCTET STRING
    if tag != 0x04:
        raise RuntimeError("test key is not a PKCS#8 PrivateKeyInfo")

    # RSAPrivateKey ::= SEQUENCE { version, n, e, d, ... }
    tag, rsa, _ = read_tlv(inner, 0)
    if tag != 0x30:
        raise RuntimeError("test key does not wrap an RSAPrivateKey")
    tag, _, i = read_tlv(rsa, 0)                    # version
    tag, n, i = read_tlv(rsa, i)                    # modulus
    if tag != 0x02:
        raise RuntimeError("malformed RSA modulus")
    tag, e, i = read_tlv(rsa, i)                    # publicExponent
    if tag != 0x02:
        raise RuntimeError("malformed RSA public exponent")

    rsa_public = der_sequence([der_integer(n), der_integer(e)])
    algorithm = der_sequence([
        b"\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01",   # rsaEncryption
        b"\x05\x00",                                       # NULL
    ])
    # subjectPublicKey is a BIT STRING: one unused-bits octet, then the key.
    spki = der_sequence([
        algorithm,
        b"\x03" + der_length(len(rsa_public) + 1) + b"\x00" + rsa_public,
    ])

    b64 = base64.b64encode(spki).decode("ascii")
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return ("-----BEGIN PUBLIC KEY-----\n"
            + "\n".join(lines)
            + "\n-----END PUBLIC KEY-----\n")


# For testing this helper
if __name__ == '__main__':
    key = load_test_rsa_key()
    print("Test key loader works!")
    pub = load_test_rsa_public_key()
    print("Test public key derived: %d chars, header %r"
          % (len(pub), pub.splitlines()[0]))