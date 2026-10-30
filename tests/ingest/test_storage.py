import hashlib

import pytest

from ingest.errors import Quarantined
from ingest.storage import FileStore


def test_put_is_content_addressed_and_idempotent(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"hello")
    sha = hashlib.sha256(b"hello").hexdigest()
    store = FileStore(tmp_path / "uploads")
    rel = store.put_file(src, sha)
    assert rel == f"{sha[:2]}/{sha}"
    src.unlink()
    assert store.resolve(rel).read_bytes() == b"hello"
    assert store.put_file(store.resolve(rel), sha) == rel


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../x"])
def test_resolve_rejects_traversal(tmp_path, bad):
    store = FileStore(tmp_path / "uploads")
    with pytest.raises(Quarantined):
        store.resolve(bad)
