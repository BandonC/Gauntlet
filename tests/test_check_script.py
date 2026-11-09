"""scripts/check.sh and its hook, run against a fake python so no real checks nest."""

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).parent.parent / "scripts"
FAKE_PYTHON = """#!/bin/sh
echo "$*" >> "$CHECK_LOG"
case "$*" in
  "$CHECK_FAIL") exit 3 ;;
esac
exit 0
"""
RUFF = "-m ruff check arena tests"
FORMAT = "-m ruff format --check arena tests"
MYPY = "-m mypy arena"
PYTEST = "-m pytest -q -m not slow and not db"
DB = "-m pytest -m db -q"
GOOD_URL = "postgresql://arena:arena@localhost:5433/arena_test"


def find_bash() -> str | None:
    if sys.platform == "win32":  # plain `bash` can be the WSL launcher here
        git = shutil.which("git")
        parents = pathlib.Path(git).parents if git else []
        found = [p / "bin" / "bash.exe" for p in parents if (p / "bin" / "bash.exe").exists()]
        return str(found[0]) if found else None
    return shutil.which("bash")


BASH = find_bash()
pytestmark = pytest.mark.skipif(not BASH, reason="needs Git Bash or bash")


@pytest.fixture
def env(tmp_path):
    # Git sets GIT_DIR and friends while a hook runs; left in, these tests would run git
    # against the real repository.
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    clean.pop("DATABASE_URL", None)
    fake = tmp_path / "fake-python"
    fake.write_text(FAKE_PYTHON, newline="\n")
    fake.chmod(0o755)
    clean.update(PYTHON=fake.as_posix(), CHECK_LOG=(tmp_path / "log").as_posix(), CHECK_FAIL="")
    return clean


def check(env, **extra):
    script = (SCRIPTS / "check.sh").as_posix()
    done = subprocess.run([BASH, script], env=env | extra, capture_output=True, text=True)
    return done


def steps(env):
    log = pathlib.Path(env["CHECK_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def test_every_step_runs_and_db_tests_need_a_database_url(env):
    assert check(env).returncode == 0
    assert steps(env) == [RUFF, FORMAT, MYPY, PYTEST]


def test_db_tests_run_on_a_test_database(env):
    assert check(env, DATABASE_URL=GOOD_URL).returncode == 0
    assert steps(env) == [RUFF, FORMAT, MYPY, PYTEST, DB]


@pytest.mark.parametrize(
    ("failing", "ran"),
    [
        (RUFF, [RUFF]),
        (FORMAT, [RUFF, FORMAT]),
        (MYPY, [RUFF, FORMAT, MYPY]),
        (PYTEST, [RUFF, FORMAT, MYPY, PYTEST]),
        (DB, [RUFF, FORMAT, MYPY, PYTEST, DB]),
    ],
)
def test_the_first_failure_fails_the_script_and_stops_it(env, failing, ran):
    out = check(env, CHECK_FAIL=failing, DATABASE_URL=GOOD_URL)
    assert out.returncode == 3
    assert steps(env) == ran


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://arena:arena@localhost:5434/arena",
        "postgresql://arena:arena@localhost:5433/arena_test_extra",
        "postgresql://arena:arena@localhost:5434/arena?x=/arena_test",
    ],
)
def test_it_refuses_any_other_database_before_running_anything(env, url):
    out = check(env, DATABASE_URL=url)
    assert out.returncode == 1 and "refusing" in out.stderr
    assert steps(env) == []


def git(repo, env, *args):
    who = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false"]
    return subprocess.run(["git", *who, *args], cwd=repo, env=env, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, env):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "check.sh", root / "scripts" / "check.sh")
    assert git(root, env, "init", "-q").returncode == 0
    (root / "a.txt").write_text("a")
    assert git(root, env, "add", "a.txt").returncode == 0
    return root


def install(repo, env):
    script = (SCRIPTS / "install-hooks.sh").as_posix()
    return subprocess.run([BASH, script], cwd=repo, env=env, capture_output=True, text=True)


def test_the_hook_blocks_a_commit_when_a_check_fails(repo, env):
    assert install(repo, env).returncode == 0
    assert git(repo, env | {"CHECK_FAIL": PYTEST}, "commit", "-qm", "bad").returncode != 0
    assert git(repo, env, "rev-parse", "--verify", "-q", "HEAD").returncode != 0


def test_the_hook_lets_a_passing_commit_through(repo, env):
    assert install(repo, env).returncode == 0
    assert git(repo, env, "commit", "-qm", "good").returncode == 0
    assert steps(env) == [RUFF, FORMAT, MYPY, PYTEST]


def test_installing_twice_is_fine_but_someone_elses_hook_is_left_alone(repo, env):
    assert install(repo, env).returncode == 0
    assert install(repo, env).returncode == 0
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    out = install(repo, env)
    assert out.returncode == 1 and "leaving it alone" in out.stderr
    assert hook.read_text() == "#!/bin/sh\nexit 0\n"
