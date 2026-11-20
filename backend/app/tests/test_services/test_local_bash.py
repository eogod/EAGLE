"""
Module for direct bash executor checks: happy path, exit codes, timeout
process-group kill, deny list and output cut with full log on disk.
Pure executor tests, service/persistence layer is covered by api flows.
"""

import pytest

from app.cmd.local_bash import BashDisabled, BashRejected, run_bash
from app.core.config import get_settings


@pytest.fixture(name="bash_on")
def fixture_bash_on(tmp_path, monkeypatch):
    """enable local bash for the scope of the test with a small workspace"""
    monkeypatch.setenv("LOCAL_BASH__ENABLED", "true")
    monkeypatch.setenv("LOCAL_BASH__WORKSPACE_DIR", str(tmp_path / "bash"))
    monkeypatch.setenv("LOCAL_BASH__TIMEOUT", "30")
    monkeypatch.setenv("LOCAL_BASH__MAX_TIMEOUT", "60")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("LOCAL_BASH__ENABLED")
    monkeypatch.delenv("LOCAL_BASH__WORKSPACE_DIR")
    monkeypatch.delenv("LOCAL_BASH__TIMEOUT")
    monkeypatch.delenv("LOCAL_BASH__MAX_OUTPUT_CHARS", raising=False)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_run_bash_returns_output_and_writes_full_log(bash_on, tmp_path) -> None:
    result = await run_bash("echo hello-eagle")

    assert result.exit_code == 0
    assert "hello-eagle" in result.output
    assert result.truncated is False
    assert result.full_log_path is not None
    log = tmp_path / "bash" / result.full_log_path.rsplit("/", 1)[-1]
    # full uncut log always survives for blue team
    assert log.read_text(encoding="utf-8").strip() == "hello-eagle"


@pytest.mark.asyncio
async def test_run_bash_non_zero_exit_code_maps_to_fail_result(
    bash_on,
) -> None:
    result = await run_bash("false")

    assert result.exit_code != 0


@pytest.mark.asyncio(loop_scope="session")
async def test_run_bash_timeout_kills_the_process_group(bash_on) -> None:
    result = await run_bash("sleep 5", timeout=1)
    assert result.exit_code != 0
    assert "[TIMEOUT] killed after 1s" in result.output
    assert result.duration_ms < 5_000  # noqa: PLR2004  # sanity bound not a contract


@pytest.mark.asyncio
async def test_run_bash_reject_list_blocks_kill_and_hangers(bash_on) -> None:
    for bad in (
        "rm -rf /",
        "rm -rf /*",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "shutdown -h now",
        "ssh user@10.0.0.5",
        "impacket-psexec 10.0.0.5",
        "nc -lvp 4444",
        "socat TCP-LISTEN:4444,fork -",
    ):
        with pytest.raises(BashRejected):
            await run_bash(bad)


@pytest.mark.asyncio
async def test_run_bash_reject_list_allows_common_tooling(bash_on) -> None:
    # agent should not be limited too much: probe/list tools stay legal
    result = await run_bash("nc -z 127.0.0.1 5432; echo ok")

    assert result.exit_code == 0
    assert "ok" in result.output


@pytest.mark.asyncio
async def test_run_bash_disabled_by_default(monkeypatch) -> None:
    # a local .env may enable bash; force the disabled state for this check
    monkeypatch.setenv("LOCAL_BASH__ENABLED", "0")
    get_settings.cache_clear()

    with pytest.raises(BashDisabled):
        await run_bash("echo hi")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_run_bash_output_cut_keeps_head_and_tail(
    bash_on, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCAL_BASH__MAX_OUTPUT_CHARS", "100")
    get_settings.cache_clear()

    result = await run_bash("echo start; yes eagle | head -c 300; echo end")

    assert result.truncated is True
    assert len(result.output) < 400  # noqa: PLR2004  # cap + cut marker
    assert "start" in result.output  # head kept
    assert "cut" in result.output  # marker
    assert "end" in result.output  # tail kept
    get_settings.cache_clear()
