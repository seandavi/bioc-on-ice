"""merge.overwrite rides out the two transient commit failures and gives up on the rest."""

import pytest
from pyiceberg.exceptions import CommitFailedException, RESTError

from bioconice import merge


class Table:
    def __init__(self, fail):
        self.fail, self.calls = list(fail), 0

    def overwrite(self, arrow, overwrite_filter):
        self.calls += 1
        if self.fail:
            raise self.fail.pop(0)


class Cat:
    def __init__(self, table):
        self.table, self.reloads = table, 0

    def load_table(self, identifier):
        self.reloads += 1
        return self.table


def test_conflict_reloads_and_retries(monkeypatch):
    monkeypatch.setattr(merge.time, "sleep", lambda s: None)
    t = Table([CommitFailedException("other writer"), CommitFailedException("again")])
    cat = Cat(t)
    merge.overwrite(cat, "x.y", t, None, None)
    assert t.calls == 3 and cat.reloads == 2


def test_429_waits_and_retries_but_other_rest_errors_raise(monkeypatch):
    slept = []
    monkeypatch.setattr(merge.time, "sleep", slept.append)
    t = Table([RESTError("429 Client Error: Too Many Requests")])
    merge.overwrite(Cat(t), "x.y", t, None, None)
    assert t.calls == 2 and slept == [65]
    t = Table([RESTError("403 Client Error: Forbidden")])
    with pytest.raises(RESTError, match="403"):
        merge.overwrite(Cat(t), "x.y", t, None, None)


def test_gives_up_eventually(monkeypatch):
    monkeypatch.setattr(merge.time, "sleep", lambda s: None)
    t = Table([CommitFailedException("x")] * 20)
    with pytest.raises(RuntimeError, match="still failing"):
        merge.overwrite(Cat(t), "x.y", t, None, None)
