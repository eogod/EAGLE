"""
direct bash execution on the EAGLE host (Kali), no C2 in the loop.
timeout, process-group kill,
head/tail output cut, full log on disk. each request pool by share.
"""

import asyncio
import logging
import os
import re
import signal
import time
import uuid
from dataclasses import dataclass

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class BashError(Exception):
    """config-level problem, api maps it to 4xx by subclass"""


class BashDisabled(BashError):
    pass


class BashRejected(BashError):
    pass


@dataclass
class BashResult:
    """cut output mirrors what goes to llm/db; full log stays on disk"""

    output: str
    exit_code: int | None
    duration_ms: int
    truncated: bool
    full_log_path: str | None


# pid -> proc, chain_id -> {pids}, so cancel-chain and shutdown can kill them
_procs: dict[int, asyncio.subprocess.Process] = {}
_chain_procs: dict[int, set[int]] = {}
# lazy, semaphore is bound per event loop anyway
_semaphore: asyncio.Semaphore | None = None


def check_reject(command: str) -> None:
    """raise BashRejected if command hits the kill/hang deny list"""
    for pattern in get_settings().local_bash.reject:
        if re.search(pattern, command, flags=re.IGNORECASE):
            raise BashRejected(
                f"command matches local bash deny rule '{pattern}', "
                "route it via zero agent / run-agent-command or fix the args"
            )


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """start_new_session makes proc the group leader, kill everyone in it"""
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError, PermissionError, OSError:
        proc.kill()


def _track(pid: int, proc: asyncio.subprocess.Process, chain_id: int | None) -> None:
    _procs[pid] = proc
    if chain_id is not None:
        _chain_procs.setdefault(chain_id, set()).add(pid)


def _untrack(pid: int, chain_id: int | None) -> None:
    _procs.pop(pid, None)
    if chain_id is not None:
        procs = _chain_procs.get(chain_id)
        if procs is not None:
            procs.discard(pid)
            if not procs:
                del _chain_procs[chain_id]


def kill_chain(chain_id: int) -> int:
    """kill running bash procs of one chain, returns count, used by cancel-chain"""
    count = 0
    for pid in list(_chain_procs.get(chain_id, ())):
        proc = _procs.get(pid)
        if proc is not None:
            _kill_group(proc)
            count += 1
    return count


def kill_all() -> None:
    """app lifespan shutdown, no orphan nmap/msfconsole on the host"""
    for proc in list(_procs.values()):
        _kill_group(proc)
    _procs.clear()
    _chain_procs.clear()


def _cut_output(text: str, cap: int, log_path: str | None) -> str:
    """keep head 60% + tail 40%, marker tells the agent where output was cut"""
    if len(text) <= cap:
        return text
    head = cap * 60 // 100
    cut_chars = len(text) - cap
    middle = (
        f"\n[... cut {cut_chars} chars, "
        + (f"full log: {log_path}, " if log_path else "")
        + "grep the file instead of rerunning ...]\n"
    )
    return text[:head].rstrip() + middle + text[-(cap - head) :]


def _spawn(command: str, cwd: str, chain_id: int | None):
    """create_subprocess_shell in its own session, tracked for cancel/shutdown"""
    return asyncio.create_subprocess_shell(  # noqa: S602
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env={**os.environ, "TERM": "dumb"},
        start_new_session=True,
    )


async def _communicate(proc, timeout: int, chain_id: int | None):
    """wait with timeout; kill nothing beyond the group proc on timeout/cancel"""
    marker = ""
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout, marker
    except TimeoutError:
        _kill_group(proc)
        try:
            # reap group, partial output still comes back from the pipe
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            stdout = b""
        logger.info("bash killed by timeout: %s", proc.pid)
        return stdout, f"[TIMEOUT] killed after {timeout}s by process group, "
    except asyncio.CancelledError:
        _kill_group(proc)
        raise
    finally:
        _untrack(proc.pid, chain_id)


# TODO: add a detached service runner (start/stop/health); run_bash kills the group.
async def run_bash(  # noqa: PLR0915
    command: str,
    timeout: int | None = None,
    cwd: str | None = None,
    chain_id: int | None = None,
    log_name: str | None = None,
) -> BashResult:
    """run one shell command to completion, kill process group on timeout,
    full output always written to the log file inside the data dir"""
    global _semaphore  # noqa: PLW0603
    cfg = get_settings().local_bash
    if not cfg.enabled:
        raise BashDisabled(
            "local bash is disabled, set LOCAL_BASH__ENABLED=1 in .env, "
            "or use zero agent via run-command"
        )
    check_reject(command)
    timeout = min(int(timeout or cfg.timeout), cfg.max_timeout)
    workspace = cfg.workspace_dir.resolve()
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(cfg.max_concurrent)

    start = time.monotonic()
    async with _semaphore:
        try:
            proc = await _spawn(command, str(cwd) if cwd else str(workspace), chain_id)
            _track(proc.pid, proc, chain_id)
        except OSError as e:
            return BashResult(
                output=f"[EXEC ERROR] {type(e).__name__}: {e}",
                exit_code=-1,
                duration_ms=0,
                truncated=False,
                full_log_path=None,
            )
        stdout, marker = await _communicate(proc, timeout, chain_id)

    duration_ms = int((time.monotonic() - start) * 1000)
    exit_code = proc.returncode
    text = _ANSI_ESCAPE.sub("", (stdout or b"").decode("utf-8", errors="replace"))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    truncated = len(text) > cfg.max_output_chars
    log_path: str | None = None
    if text:
        log_name = log_name or f"bash_{uuid.uuid4().hex[:12]}.log"
        full_path = workspace / log_name
        try:
            full_path.write_text(text, encoding="utf-8")
            log_path = str(full_path)
        except OSError as e:
            logger.warning("bash full log write failed: %s", e)
        text = _cut_output(text, cfg.max_output_chars, log_path)
    if marker:
        text = marker + text
    return BashResult(
        output=text,
        exit_code=exit_code,
        duration_ms=duration_ms,
        truncated=truncated,
        full_log_path=log_path,
    )
