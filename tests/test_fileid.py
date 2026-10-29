"""`media_stack.fileid`: file identity including st_ctime_ns.

A same-size in-place rewrite with the mtime restored looks untouched by mtime alone;
the kernel stamps ctime on every write and userspace cannot set it.
"""

import os
import time

import pytest

from media_stack import fileid


def _rewrite_in_place_keeping_mtime(path, new_bytes):
    st = path.stat()
    time.sleep(0.02)  # past the filesystem's timestamp granularity
    with path.open("r+b") as fh:
        fh.write(new_bytes)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return st


def test_identity_has_inode_size_mtime_and_ctime(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(b"hello")
    st = p.stat()
    assert fileid.file_identity(p) == (st.st_ino, 5, st.st_mtime_ns, st.st_ctime_ns)
    assert fileid.identity_from_stat(st) == fileid.file_identity(p)


def test_identity_of_a_missing_file_is_none(tmp_path):
    assert fileid.file_identity(tmp_path / "gone") is None


def test_a_same_size_rewrite_with_the_mtime_restored_changes_only_the_ctime(tmp_path):
    p = tmp_path / "video.mkv"
    p.write_bytes(b"A" * 100)
    before = fileid.file_identity(p)
    _rewrite_in_place_keeping_mtime(p, b"B" * 100)
    after = fileid.file_identity(p)
    assert before[:3] == after[:3]          # inode, size and mtime_ns all look untouched
    assert before[3] != after[3]            # ctime is what gives it away
    assert before != after


def test_a_touch_that_changes_nothing_but_the_ctime_is_still_a_change(tmp_path):
    p = tmp_path / "video.mkv"
    p.write_bytes(b"A" * 100)
    before = fileid.file_identity(p)
    time.sleep(0.02)
    os.chmod(p, 0o600)  # a pure metadata change
    assert fileid.file_identity(p)[3] != before[3]


def test_a_current_identity_of_none_matches_nothing():
    for allow in (True, False):
        assert not fileid.identity_matches([1, 2, 3, 4], None, allow_legacy=allow)


def test_a_four_field_record_must_match_exactly():
    cur = (7, 100, 1000, 2000)
    assert fileid.identity_matches([7, 100, 1000, 2000], cur, allow_legacy=False)
    assert fileid.identity_matches((7, 100, 1000, 2000), cur, allow_legacy=True)
    for bad in ([8, 100, 1000, 2000], [7, 101, 1000, 2000], [7, 100, 1001, 2000], [7, 100, 1000, 2001]):
        assert not fileid.identity_matches(bad, cur, allow_legacy=True)
        assert not fileid.identity_matches(bad, cur, allow_legacy=False)


def test_a_legacy_three_field_record_is_unknown_when_legacy_is_not_allowed():
    """A proof-of-verification record cannot be trusted without a ctime: unknown counts as changed."""
    assert not fileid.identity_matches([7, 100, 1000], (7, 100, 1000, 2000), allow_legacy=False)


def test_a_legacy_three_field_record_compares_on_the_shared_fields_when_allowed():
    """A 'same release' record still works: the three shared fields decide."""
    cur = (7, 100, 1000, 2000)
    assert fileid.identity_matches([7, 100, 1000], cur, allow_legacy=True)
    assert not fileid.identity_matches([7, 100, 999], cur, allow_legacy=True)
    assert not fileid.identity_matches([8, 100, 1000], cur, allow_legacy=True)


@pytest.mark.parametrize("junk", [None, "7,100,1000", 7, {}, [], [7], [7, 100], [7, 100, 1000, 2000, 1],
                                  [7, 100, "1000"], [7, 100, 1000.0], [True, 100, 1000],
                                  [7, 100, None, 2000]])
def test_malformed_records_never_match(junk):
    for allow in (True, False):
        assert not fileid.identity_matches(junk, (7, 100, 1000, 2000), allow_legacy=allow)
