"""The sync decisions, from observed facts to a status. No disk, no git."""

import pytest

from cimkit_git.state import Record
from cimkit_git.sync import Facts, Status, decide

REC = Record(source_tree="T0", binary_hash="B0", target="dev", mapping={})


def never():
    raise AssertionError("exploded the binary when its hash already decided")


def facts(
    binary="B0", source="T0", index=None, head=None, record=REC, exploded=never, unmerged=False, converts_text=False
):
    return Facts(
        binary_hash=binary,
        source_tree=source,
        index_tree=index,
        head_tree=head,
        record=record,
        exploded_tree=exploded,
        unmerged=unmerged,
        converts_text=converts_text,
    )


def test_matching_hash_and_tree_is_clean_without_exploding():
    assert decide(facts()) is Status.CLEAN


@pytest.mark.parametrize(
    "binary, source, status",
    [
        ("B1", "T0", Status.BINARY_EDITED),
        ("B0", "T1", Status.SOURCE_CHANGED),
        ("B1", "T1", Status.CONFLICT),
    ],
)
def test_which_side_changed_since_the_record_decides_the_status(binary, source, status):
    assert decide(facts(binary=binary, source=source, exploded=lambda: "T9")) is status


@pytest.mark.parametrize("source, status", [("T0", Status.CLEAN), ("T1", Status.SOURCE_CHANGED)])
def test_a_rezipped_binary_that_explodes_to_the_recorded_source_is_unchanged(source, status):
    assert decide(facts(binary="B1", source=source, exploded=lambda: "T0")) is status


def test_a_binary_that_cannot_be_exploded_counts_as_edited():
    assert decide(facts(binary="B1", exploded=lambda: None)) is Status.BINARY_EDITED


@pytest.mark.parametrize(
    "binary, source, record, status",
    [
        (None, "T1", REC, Status.NOT_BUILT),
        (None, "T1", None, Status.NOT_BUILT),
        ("B1", None, REC, Status.NEW_PROJECT),
        ("B1", None, None, Status.NEW_PROJECT),
        ("B1", "T1", None, Status.NO_RECORD),
    ],
)
def test_a_missing_side_or_record_decides_before_any_comparison(binary, source, record, status):
    assert decide(facts(binary=binary, source=source, record=record)) is status


@pytest.mark.parametrize(
    "index, source, record, status",
    [
        ("T2", "T1", REC, Status.STAGED_EDIT),
        ("T2", "T1", None, Status.STAGED_EDIT),
        ("T1", "T1", REC, Status.SOURCE_CHANGED),
        ("T0", "T1", REC, Status.SOURCE_CHANGED),
        (None, "T1", REC, Status.SOURCE_CHANGED),
    ],
)
def test_a_staged_source_matching_neither_side_would_be_lost(index, source, record, status):
    assert decide(facts(index=index, source=source, record=record)) is status


def test_a_staged_source_matching_head_holds_nothing_to_lose():
    # Explode leaves the committed tree in the index, matching neither the new Source nor the new record.
    assert decide(facts(index="T2", head="T2", source="T1")) is Status.SOURCE_CHANGED


@pytest.mark.parametrize(
    "kw, status",
    [
        ({"unmerged": True}, Status.UNMERGED),
        ({"converts_text": True}, Status.LINE_ENDINGS),
        ({"converts_text": True, "unmerged": True}, Status.LINE_ENDINGS),
    ],
)
def test_pre_checks_refuse_before_the_table(kw, status):
    for binary, source in [("B0", "T0"), (None, "T1"), ("B1", None)]:
        assert decide(facts(binary=binary, source=source, **kw)) is status
