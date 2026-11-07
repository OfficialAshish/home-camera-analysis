"""SHA-256 Merkle tree (RFC 6962 hashing) over a list of evidence items.

Leaves are ``SHA256(0x00 || item)`` and inner nodes ``SHA256(0x01 || left || right)``, so a leaf
can never be confused with a node (second-preimage safe). An odd node at the end of a level is
promoted unchanged, as in RFC 6962 (Certificate Transparency). The root seals the whole set: changing,
adding, removing or reordering any item changes it, and ``proof``/``verify_proof`` show one
item's membership without the others.

Items here are lines ``"<name>  <sha256>"`` (sorted by name), so the root is a function of every
file's content hash and name. Reference: RFC 6962 section 2.1
(https://www.rfc-editor.org/rfc/rfc6962#section-2.1).
"""
from __future__ import annotations

import hashlib
from typing import Iterable, List, Tuple


def leaf_hash(item: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + item).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _levels(items: List[bytes]) -> List[List[bytes]]:
    level = [leaf_hash(i) for i in items]
    levels = [level]
    while len(level) > 1:
        nxt = [node_hash(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])
        levels.append(nxt)
        level = nxt
    return levels


def root(items: Iterable[bytes]) -> str:
    items = list(items)
    if not items:
        return hashlib.sha256(b"").hexdigest()          # RFC 6962: MTH({}) = SHA-256()
    return _levels(items)[-1][0].hex()


def proof(items: List[bytes], index: int) -> List[Tuple[str, str]]:
    """Audit path of item `index`: [(side, sibling hash hex)] from the leaf up."""
    levels = _levels(items)
    out = []
    i = index
    for level in levels[:-1]:
        sib = i ^ 1
        if sib < len(level):
            out.append(("L" if sib < i else "R", level[sib].hex()))
        i //= 2
    return out


def verify_proof(item: bytes, path: List[Tuple[str, str]], expected_root: str) -> bool:
    h = leaf_hash(item)
    for side, sib in path:
        s = bytes.fromhex(sib)
        h = node_hash(s, h) if side == "L" else node_hash(h, s)
    return h.hex() == expected_root


def file_lines(hashes: dict) -> List[bytes]:
    """{name: sha256} -> the Merkle items (sorted by name)."""
    return [f"{name}  {h}".encode() for name, h in sorted(hashes.items())]
