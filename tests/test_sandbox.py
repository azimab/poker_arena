import shutil
import subprocess
import time

import pytest

from poker_arena import (
    Action,
    DEFAULT_CONFIG,
    Observation,
    SandboxedBot,
    SandboxError,
    SandboxTimeout,
    Street,
)
from poker_arena.sandbox import build_image, image_exists

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "info"], capture_output=True).returncode != 0,
    reason="needs a running docker daemon",
)


@pytest.fixture(scope="session", autouse=True)
def sandbox_image():
    if not image_exists():
        build_image()


COUNTING_BOT = """
from poker_arena import Action

class CountingBot:
    name = "counting"

    def __init__(self):
        self.calls = 0

    def act(self, obs):
        self.calls += 1
        return Action.raise_to(obs.min_raise_to) if self.calls == 1 else Action.call()

def create_bot():
    return CountingBot()
"""

# Bots that probe the sandbox report what they found by raising: the message
# comes back to the parent inside the SandboxError.
PROBE_BOT = """
class ProbeBot:
    name = "probe"

    def act(self, obs):
        raise RuntimeError(probe())

def create_bot():
    return ProbeBot()
"""

MINER_BOT = """
import time
from poker_arena import Action

class MinerBot:
    name = "miner"

    def act(self, obs):
        # Burns CPU in bursts short enough to dodge any single act_timeout,
        # the way a bot pacing itself to evade detection would.
        end = time.process_time() + 0.15
        while time.process_time() < end:
            pass
        return Action.call()

def create_bot():
    return MinerBot()
"""

HANG_BOT = """
class HangBot:
    name = "hang"

    def act(self, obs):
        while True:
            pass

def create_bot():
    return HangBot()
"""

NOISY_BOT = """
from poker_arena import Action

class NoisyBot:
    name = "noisy"

    def act(self, obs):
        print("thinking out loud")
        return Action.call()

def create_bot():
    return NoisyBot()
"""

GARBAGE_BOT = """
class GarbageBot:
    name = "garbage"

    def act(self, obs):
        return "fold"

def create_bot():
    return GarbageBot()
"""

BROKEN_BOT = """
def create_bot():
    raise ValueError("nope")
"""

# Reports the gap it observed between its own previous reply and this
# observation, in milliseconds, as the raise amount.
STOPWATCH_BOT = """
import time
from poker_arena import Action

class StopwatchBot:
    name = "stopwatch"

    def __init__(self):
        self.replied_at = None

    def act(self, obs):
        now = time.monotonic()
        gap = 0 if self.replied_at is None else int((now - self.replied_at) * 1000)
        self.replied_at = time.monotonic()
        return Action.raise_to(gap)

def create_bot():
    return StopwatchBot()
"""


def write(tmp_path, source, filename="bot.py"):
    path = tmp_path / filename
    path.write_text(source)
    return path


def probe(tmp_path, body, **kwargs):
    """Run `body` (a probe() function) inside the sandbox, return what it reported."""
    source = body + "\n" + PROBE_BOT
    kwargs.setdefault("pace", 0)
    with SandboxedBot(write(tmp_path, source), **kwargs) as bot:
        with pytest.raises(SandboxError) as excinfo:
            bot.act(_obs())
    return str(excinfo.value)


def _obs(**overrides):
    fields = dict(
        seat=0,
        button=0,
        street=Street.PREFLOP,
        hole=("As", "Kd"),
        board=(),
        stacks=(9_900, 9_800),
        committed=(100, 200),
        street_bets=(100, 200),
        pot=300,
        to_call=100,
        can_check=False,
        can_raise=True,
        min_raise_to=400,
        max_raise_to=9_900,
        sb=50,
        bb=100,
        history=(),
    )
    fields.update(overrides)
    return Observation(**fields)


def test_runs_a_valid_bot_and_preserves_state_across_calls(tmp_path):
    with SandboxedBot(write(tmp_path, COUNTING_BOT), pace=0) as bot:
        assert bot.name == "counting"
        assert bot.act(_obs()) == Action.raise_to(400)
        assert bot.act(_obs()) == Action.call()


# --- filesystem -------------------------------------------------------------


def test_the_host_filesystem_is_not_visible(tmp_path):
    # The submission itself crosses as text on stdin, so even the directory it
    # was read from must not exist inside the container.
    body = f"""
import os
def probe():
    return repr([
        os.path.exists({str(tmp_path)!r}),
        os.path.exists("/Users"),
        os.path.exists("/var/run/docker.sock"),
    ])
"""
    assert "[False, False, False]" in probe(tmp_path, body)


def test_the_root_filesystem_is_read_only(tmp_path):
    # /var/tmp is mode 1777, so a write there fails with EROFS only because the
    # rootfs is read-only -- probing a root-owned path would pass either way,
    # just from running as nobody.
    body = """
import errno
def probe():
    try:
        open("/var/tmp/evil", "w").write("x")
    except OSError as exc:
        return f"blocked: {errno.errorcode[exc.errno]}"
    return "WROTE"
"""
    assert "blocked: EROFS" in probe(tmp_path, body)


def test_scratch_space_is_private_and_capped(tmp_path):
    body = """
def probe():
    try:
        with open("/tmp/big", "wb") as f:
            for _ in range(64):
                f.write(b"x" * 1024 * 1024)
    except OSError as exc:
        return f"capped: {exc.errno}"
    return "UNCAPPED"
"""
    assert "capped" in probe(tmp_path, body, tmpfs_mb=8)


# --- network ----------------------------------------------------------------


def test_there_is_no_network_interface_to_use(tmp_path):
    body = """
import socket
def probe():
    out = []
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2)
        out.append("CONNECTED")
    except OSError as exc:
        out.append(f"blocked: {type(exc).__name__}")
    try:
        socket.gethostbyname("example.com")
        out.append("RESOLVED")
    except OSError:
        out.append("no-dns")
    return repr(out)
"""
    result = probe(tmp_path, body)
    assert "blocked" in result and "no-dns" in result
    assert "CONNECTED" not in result and "RESOLVED" not in result


def test_a_listening_socket_cannot_be_reached_from_outside(tmp_path):
    body = """
import socket
def probe():
    s = socket.socket()
    s.bind(("0.0.0.0", 9999))
    s.listen(1)
    return "bound but unreachable: " + repr(s.getsockname())
"""
    # Binding inside the container's own empty netns is allowed and useless --
    # there is no interface and no port publishing, so nothing can dial in.
    assert "unreachable" in probe(tmp_path, body)
    assert subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True)


# --- privilege / escape -----------------------------------------------------


def test_the_bot_runs_unprivileged_with_no_capabilities(tmp_path):
    body = """
import os
def probe():
    status = dict(
        line.split(":", 1) for line in open("/proc/self/status").read().splitlines() if ":" in line
    )
    return repr({
        "uid": os.getuid(),
        "caps": status["CapEff"].strip(),
        "nnp": status["NoNewPrivs"].strip(),
    })
"""
    result = probe(tmp_path, body)
    assert "'uid': 65534" in result
    assert "'caps': '0000000000000000'" in result
    assert "'nnp': '1'" in result


def test_privilege_escalation_is_refused(tmp_path):
    body = """
import os
def probe():
    try:
        os.setuid(0)
    except OSError as exc:
        return f"refused: {exc.errno}"
    return "ROOT"
"""
    result = probe(tmp_path, body)
    assert "refused" in result and "ROOT" not in result


def test_the_bot_cannot_see_other_processes(tmp_path):
    body = """
import os
def probe():
    pids = sorted(int(p) for p in os.listdir("/proc") if p.isdigit())
    return repr(pids)
"""
    # A private PID namespace: only the container's own init and the worker, so
    # there is no engine process to read hole cards out of.
    result = probe(tmp_path, body)
    assert result.count(",") < 4, result


# --- resource exhaustion ----------------------------------------------------


def test_a_memory_bomb_is_killed(tmp_path):
    body = """
def probe():
    blocks = []
    for _ in range(64):
        blocks.append(bytearray(32 * 1024 * 1024))
    return "ALLOCATED"
"""
    assert "ALLOCATED" not in probe(tmp_path, body, memory_mb=128)


def test_a_fork_bomb_hits_the_pid_limit(tmp_path):
    body = """
import os
def probe():
    n = 0
    for _ in range(500):
        try:
            pid = os.fork()
        except OSError:
            break
        if pid == 0:
            os._exit(0)
        n += 1
    return f"forked {n}"
"""
    result = probe(tmp_path, body, pids=24)
    assert "forked" in result
    assert int(result.split("forked ")[1].split()[0].strip("'\"")) < 24, result


def test_a_bot_that_paces_its_cpu_burn_is_still_stopped(tmp_path):
    # Each call is well under act_timeout on its own; only the match-wide
    # budget catches a bot spreading its compute across many decisions.
    with SandboxedBot(write(tmp_path, MINER_BOT), cpu_seconds=0.3, act_timeout=2, pace=0) as bot:
        bot.act(_obs())
        with pytest.raises(SandboxError, match="budget"):
            for _ in range(5):
                bot.act(_obs())


def test_a_hung_decision_times_out(tmp_path):
    with SandboxedBot(write(tmp_path, HANG_BOT), act_timeout=0.3, pace=0) as bot:
        with pytest.raises(SandboxTimeout):
            bot.act(_obs())


# --- timing side channel ----------------------------------------------------


def test_the_gap_between_decisions_does_not_leak_opponent_think_time(tmp_path):
    # The bot times the gap between its own reply and its next observation. The
    # parent stalls wildly between calls, standing in for an opponent whose think
    # time would otherwise be readable off that gap.
    with SandboxedBot(write(tmp_path, STOPWATCH_BOT), pace=0.5) as bot:
        bot.act(_obs())
        gaps = []
        for stall in (0.0, 0.4, 0.05):
            time.sleep(stall)
            gaps.append(bot.act(_obs()).amount)

    assert all(490 <= gap <= 600 for gap in gaps), gaps
    assert max(gaps) - min(gaps) < 60, gaps


def test_pacing_can_be_turned_off(tmp_path):
    with SandboxedBot(write(tmp_path, STOPWATCH_BOT), pace=0) as bot:
        bot.act(_obs())
        time.sleep(0.3)
        assert bot.act(_obs()).amount >= 250


# --- protocol robustness ----------------------------------------------------


def test_a_bot_that_prints_does_not_corrupt_the_protocol(tmp_path):
    with SandboxedBot(write(tmp_path, NOISY_BOT), pace=0) as bot:
        assert bot.act(_obs()) == Action.call()


def test_a_bot_returning_a_non_action_fails_cleanly(tmp_path):
    with SandboxedBot(write(tmp_path, GARBAGE_BOT), pace=0) as bot:
        with pytest.raises(SandboxError, match="must return an Action"):
            bot.act(_obs())


def test_a_broken_submission_fails_at_construction(tmp_path):
    with pytest.raises(SandboxError, match="nope"):
        SandboxedBot(write(tmp_path, BROKEN_BOT))


# --- lifecycle --------------------------------------------------------------


def test_close_destroys_the_container(tmp_path):
    bot = SandboxedBot(write(tmp_path, COUNTING_BOT), pace=0)
    name = bot._name
    bot.close()
    assert bot._proc.poll() is not None
    listed = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={name}", "-q"],
        capture_output=True,
        text=True,
    )
    assert listed.stdout.strip() == "", "container survived close()"


def test_a_killed_bot_leaves_no_container_behind(tmp_path):
    bot = SandboxedBot(write(tmp_path, HANG_BOT), act_timeout=0.3, pace=0)
    name = bot._name
    with pytest.raises(SandboxTimeout):
        bot.act(_obs())
    listed = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={name}", "-q"],
        capture_output=True,
        text=True,
    )
    assert listed.stdout.strip() == "", "container survived a timeout kill"


def test_plays_a_real_hand_against_an_in_process_bot(tmp_path):
    from poker_arena import play_hand
    from poker_arena.bots import CallBot

    with SandboxedBot(write(tmp_path, COUNTING_BOT), pace=0) as sandboxed:
        result = play_hand([sandboxed, CallBot()], seed=1, button=0, config=DEFAULT_CONFIG)
        assert result.pot > 0
