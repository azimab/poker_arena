from __future__ import annotations

import json
import select
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn

from .types import Action, ActionRecord, ActionType, Observation, Street

IMAGE = "poker-arena-sandbox:latest"

DEFAULT_CPU_SECONDS = 5.0
DEFAULT_MEMORY_MB = 256
DEFAULT_ACT_TIMEOUT = 1.0
DEFAULT_PIDS = 64
DEFAULT_TMPFS_MB = 16
MAX_SOURCE_BYTES = 1024 * 1024
LOAD_TIMEOUT = 30.0

_TIMEOUT = object()


class SandboxError(Exception):
    """A sandboxed bot failed to load, crashed, or was killed for crossing a limit."""


class SandboxTimeout(SandboxError):
    """A sandboxed bot did not return an action within the per-decision budget."""


def build_image(image: str = IMAGE, context: Path | None = None) -> None:
    context = context or Path(__file__).resolve().parent.parent
    subprocess.run(["docker", "build", "-t", image, str(context)], check=True)


def image_exists(image: str = IMAGE) -> bool:
    return _docker("image", "inspect", image).returncode == 0


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _decode_observation(d: dict) -> Observation:
    d = dict(d)
    d["street"] = Street(d["street"])
    for key in ("hole", "board", "stacks", "committed", "street_bets"):
        d[key] = tuple(d[key])
    d["history"] = tuple(
        ActionRecord(Street(r["street"]), r["seat"], ActionType(r["type"]), r["amount"])
        for r in d["history"]
    )
    return Observation(**d)


def _run_argv(name: str, cpu_seconds: float, memory_mb: int, pids: int, tmpfs_mb: int) -> list[str]:
    return [
        "docker", "run", "--rm", "--interactive", "--init",
        "--name", name,
        # No network device at all -- not a named network, not even loopback-to-host.
        "--network", "none",
        # Nothing from the host is mounted; the only writable path is a private
        # tmpfs that dies with the container.
        "--read-only",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={tmpfs_mb}m",
        "--user", "65534:65534",
        "--ipc", "private",
        "--shm-size", "8m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(pids),
        "--memory", f"{memory_mb}m",
        "--memory-swap", f"{memory_mb}m",
        "--cpus", "1.0",
        "--ulimit", f"cpu={max(1, int(cpu_seconds))}",
        "--ulimit", "core=0",
        "--ulimit", "nofile=128:128",
        IMAGE,
    ]


class SandboxedBot:
    """Runs an untrusted bot submission in a throwaway container. See README for the limits."""

    def __init__(
        self,
        path: str | Path,
        cpu_seconds: float = DEFAULT_CPU_SECONDS,
        memory_mb: int = DEFAULT_MEMORY_MB,
        act_timeout: float = DEFAULT_ACT_TIMEOUT,
        pids: int = DEFAULT_PIDS,
        tmpfs_mb: int = DEFAULT_TMPFS_MB,
        pace: float | None = None,
    ):
        path = Path(path)
        if not path.is_file():
            raise SandboxError(f"{path} is not a file")
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise SandboxError(f"{path} is larger than the {MAX_SOURCE_BYTES}-byte submission limit")
        try:
            source = path.read_text()
        except UnicodeDecodeError as exc:
            raise SandboxError(f"{path} is not valid utf-8 python source: {exc}") from exc
        if not image_exists():
            raise SandboxError(
                f"sandbox image {IMAGE!r} is missing -- build it with "
                f"poker_arena.sandbox.build_image() or `docker build -t {IMAGE} .`"
            )

        self.act_timeout = act_timeout
        # Masking the opponent's think time only works if the cadence covers the
        # longest a decision can take, so anything shorter than act_timeout leaks.
        self.pace = act_timeout if pace is None else pace
        self._budget = cpu_seconds
        self._spent = 0.0
        self._replied_at: float | None = None
        self._name = f"poker-arena-{uuid.uuid4().hex[:12]}"
        self._proc = subprocess.Popen(
            _run_argv(self._name, cpu_seconds, memory_mb, pids, tmpfs_mb),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # The submission crosses the boundary as text on stdin: the container
        # never sees the host path it came from.
        self._send({"source": source})
        line = self._read_line(LOAD_TIMEOUT)
        if line is _TIMEOUT:
            self._fail(f"sandbox did not finish loading the bot within {LOAD_TIMEOUT}s")
        if not line:
            self._fail("sandbox container exited before loading the bot")
        hello = self._parse(line)
        if "error" in hello:
            self._fail(hello["error"])
        self.name = hello["name"]

    def act(self, obs: Observation) -> Action:
        if self._proc.poll() is not None:
            self._fail("sandbox container is no longer running")

        self._wait_for_cadence()
        started = time.monotonic()
        self._send(asdict(obs))
        line = self._read_line(self.act_timeout)
        self._spent += time.monotonic() - started
        self._replied_at = time.monotonic()

        if line is _TIMEOUT:
            self._fail(
                f"{self.name!r} did not return a decision within {self.act_timeout}s",
                SandboxTimeout,
            )
        if not line:
            self._fail(f"{self.name!r} sandbox container exited unexpectedly")

        reply = self._parse(line)
        if "error" in reply:
            self._fail(reply["error"])
        if self._spent > self._budget:
            self._fail(f"{self.name!r} exceeded its {self._budget}s match time budget")
        return Action(ActionType(reply["type"]), reply["amount"])

    def close(self) -> None:
        self._destroy()

    def __enter__(self) -> "SandboxedBot":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _wait_for_cadence(self) -> None:
        """Deliver every observation a fixed interval after the bot's own last reply, so
        the gap cannot be used to time how long the opponent spent thinking."""
        if not self.pace or self._replied_at is None:
            return
        remaining = self.pace - (time.monotonic() - self._replied_at)
        if remaining > 0:
            time.sleep(remaining)

    def _send(self, payload: dict) -> None:
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except OSError:
            self._fail("sandbox container exited unexpectedly")

    def _read_line(self, timeout: float):
        ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
        return self._proc.stdout.readline() if ready else _TIMEOUT

    def _parse(self, line: str) -> dict:
        try:
            reply = json.loads(line)
        except json.JSONDecodeError:
            reply = None
        if not isinstance(reply, dict):
            self._fail(f"sandbox sent malformed output: {line.strip()[:200]!r}")
        return reply

    def _fail(self, message: str, kind: type[SandboxError] = SandboxError) -> NoReturn:
        # Destroy first: this can be called on a live container, and the stderr
        # read below only ends at the EOF that tearing it down produces.
        self._destroy()
        stderr = self._proc.stderr.read() if self._proc.stderr else ""
        raise kind(f"{message}\n{stderr}" if stderr.strip() else message)

    def _destroy(self) -> None:
        """Killing the local `docker run` client would orphan the container, so the
        container has to be torn down by name."""
        _docker("rm", "--force", self._name)
        if self._proc.poll() is None:
            self._proc.kill()
        self._proc.wait()
