from ouroboros.openclaw.ux import parse_channel_command


def test_parse_repo_set_command() -> None:
    parsed = parse_channel_command("/ouro repo set /repo/demo")
    assert parsed is not None
    assert parsed.action == "set_repo"
    assert parsed.repo == "/repo/demo"


def test_parse_status_and_queue_commands() -> None:
    status = parse_channel_command("/ouro status")
    queue = parse_channel_command("/ouro queue")
    assert status is not None and status.action == "status"
    assert queue is not None and queue.action == "status"


def test_parse_new_and_answer_commands() -> None:
    new = parse_channel_command("/ouro new work on feature x")
    answer = parse_channel_command("/ouro answer use stripe")
    assert new is not None
    assert new.action == "message"
    assert new.mode == "new"
    assert new.message == "work on feature x"
    assert answer is not None
    assert answer.mode == "answer"
    assert answer.message == "use stripe"


def test_parse_non_command_returns_none() -> None:
    assert parse_channel_command("work on feature x") is None


def test_parse_malformed_commands_return_invalid_usage() -> None:
    new = parse_channel_command("/ouro new")
    answer = parse_channel_command("/ouro answer")
    status = parse_channel_command("/ouro status extra")
    assert new is not None and new.action == "invalid"
    assert answer is not None and answer.action == "invalid"
    assert status is not None and status.action == "invalid"
