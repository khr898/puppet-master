import json

from puppet_master import attempts


def test_unknown_task_does_not_give_up(tmp_path):
    assert attempts.give_up(tmp_path, "k1") is False


def test_two_failures_give_up_and_a_pass_does_not_count(tmp_path):
    attempts.record(tmp_path, "k1", passed=False)
    assert attempts.give_up(tmp_path, "k1") is False
    attempts.record(tmp_path, "k1", passed=True)
    assert attempts.give_up(tmp_path, "k1") is False
    attempts.record(tmp_path, "k1", passed=False)
    assert attempts.give_up(tmp_path, "k1") is True


def test_tasks_are_counted_separately_and_persist_on_disk(tmp_path):
    attempts.record(tmp_path, "a", passed=False)
    attempts.record(tmp_path, "a", passed=False)
    attempts.record(tmp_path, "b", passed=False)
    data = json.loads((tmp_path / ".puppet-master" / "attempts.json").read_text())
    assert data == {"a": {"failed": 2}, "b": {"failed": 1}}
    assert attempts.give_up(tmp_path, "a") and not attempts.give_up(tmp_path, "b")


def test_reset_forgets_the_task(tmp_path):
    attempts.record(tmp_path, "a", passed=False)
    attempts.record(tmp_path, "a", passed=False)
    attempts.reset(tmp_path, "a")
    assert attempts.give_up(tmp_path, "a") is False
    attempts.reset(tmp_path, "never-seen")  # no error


def test_a_corrupt_file_is_treated_as_empty(tmp_path):
    (tmp_path / ".puppet-master").mkdir()
    (tmp_path / ".puppet-master" / "attempts.json").write_text("{not json")
    assert attempts.give_up(tmp_path, "a") is False
    attempts.record(tmp_path, "a", passed=False)
    assert attempts.give_up(tmp_path, "a") is False
