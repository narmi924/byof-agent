"""Competing users and killed real processes preserve publication transactions and source IDs.

Children use the application DB role and real socket HTTP reader/writer only. For claimed
work, tests first prove the live lease blocks recovery, then advance only that test row's
lease expiry; neither business clocks nor physical facts are edited to accelerate recovery.
"""

import importlib.metadata
import json
import os
import subprocess
import sys
import sysconfig
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_dynamic_factory_postgres import snapshot
from test_publication_postgres import publishing as publishing

from packages.auth import AccessError, Grant, Principal
from packages.domain.models import Release
from packages.persistence import Membership, User, connect
from packages.planning.publication import Publication, commit_publication, deliver_one
from packages.planning.service import approve
from packages.planning.store import ApprovalRecord
from services.factory_sim.storage import SourceAction

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = ("SQLAlchemy", "psycopg", "httpx", "ortools", "pydantic", "langgraph", "numpy")

CHILD = r"""
import json
import os
import sys
from pathlib import Path
from threading import Event
from urllib.parse import urlparse

from sqlalchemy import event
from sqlalchemy.orm import Session

from packages.auth import Grant, Principal
from packages.integrations.factory_http import FactoryExecution, FactoryHTTP
from packages.persistence import connect
from packages.planning.publication import Publication, commit_publication, deliver_one

config = json.loads(sys.stdin.readline())
engine = connect(os.environ["BYOF_PROCESS_DATABASE_URL"])
assert engine.url.database == "byof_test"
assert urlparse(config["origin"]).hostname == "127.0.0.1"
reader = FactoryHTTP(config["origin"], config["reader_token"])
writer = FactoryExecution(config["origin"], config["writer_token"])

def log(kind, operation_id):
    with Path(config["http_log"]).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, "operation_id": operation_id,
                                 "pid": os.getpid()}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

def park(operation_id=None):
    Path(config["marker"]).write_text(json.dumps({
        "phase": config["phase"], "operation_id": operation_id, "pid": os.getpid()
    }), encoding="utf-8")
    if not Event().wait(45):
        raise RuntimeError("Parent did not terminate the owned child at its barrier")

def inserted(connection, cursor, statement, parameters, context, executemany):
    if statement.lstrip().startswith("INSERT INTO byof.publications"):
        park(parameters.get("release_id") if isinstance(parameters, dict) else None)

class ObservedReader:
    def capabilities(self):
        return reader.capabilities()

    def action(self, factory_id, run_id, operation_id):
        assert factory_id == config["factory_id"]
        log("query", operation_id)
        receipt = reader.action(factory_id, run_id, operation_id)
        if config["phase"] == "after_claim":
            assert receipt is None
            park(operation_id)
        return receipt

class ObservedWriter:
    def submit(self, submission):
        assert submission.factory_id == config["factory_id"]
        log("submit", submission.operation_id)
        receipt = writer.submit(submission)
        if config["phase"] == "after_source_acceptance":
            assert receipt.source_state == "ACTIVE"
            park(submission.operation_id)
        return receipt

try:
    if config["phase"] == "before_local_commit":
        event.listen(engine, "after_cursor_execute", inserted)
    actor = Principal(user_id=config["user_id"], username=config["username"],
                      grants=(Grant(factory_id=config["factory_id"], role="planner"),))
    release = commit_publication(
        engine, actor, config["factory_id"], config["candidate_id"],
        request_id=config["request_id"], candidate_hash=config["candidate_hash"],
    )
    if config["phase"] == "after_local_commit":
        park(release.operation_id)
    worked = deliver_one(engine, ObservedReader(), ObservedWriter())
    if config["phase"] == "after_receipt_commit":
        park(release.operation_id)
    with Session(engine) as db:
        row = db.get(Publication, release.release_id)
        print(json.dumps({"status": "completed", "pid": os.getpid(), "worked": worked,
                          "release_id": row.release_id, "state": row.state,
                          "source_state": row.document["source_state"]}), flush=True)
except Exception as exc:
    # SQL exceptions can contain connection details; only the error class/code crosses this pipe.
    print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                      "code": getattr(exc, "code", None)}), flush=True)
    sys.exit(81)
finally:
    reader.close()
    writer.close()
    engine.dispose()
"""


def _approve(context, *, actor=None, request_id="approval-before-process"):
    source, _, _, default_actor, candidate_id, candidate_hash = context
    return approve(
        source[3],
        actor or default_actor,
        source[2].factory_id,
        candidate_id,
        request_id=request_id,
        candidate_hash=candidate_hash,
        action_scope="publish_plan",
        decision="APPROVED",
    )


def _source_actions(source):
    with Session(source[4]) as db:
        return list(
            db.scalars(
                select(SourceAction).where(
                    SourceAction.factory_id == source[2].factory_id,
                    SourceAction.kind == "plan.submit",
                )
            )
        )


def _publications(source):
    with Session(source[3]) as db:
        return list(
            db.scalars(select(Publication).where(Publication.factory_id == source[2].factory_id))
        )


def _log(path):
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )


def _python_command(program):
    # Windows venv python.exe redirects to another PID. The base interpreter executes
    # directly; isolated startup uses only this locked environment's package directories.
    libraries = list(dict.fromkeys((sysconfig.get_path("purelib"), sysconfig.get_path("platlib"))))
    roots = {str(Path(path).resolve()) for path in libraries}
    expected = {}
    for name in DEPENDENCIES:
        distribution = importlib.metadata.distribution(name)
        root = str(Path(distribution.locate_file("")).resolve())
        assert root in roots, "Publication tests must use their locked project environment"
        expected[name] = {"version": distribution.version, "root": root}
    bootstrap = (
        "import sys\n"
        f"sys.path[:0] = {[str(ROOT), *libraries]!r}\n"
        "import importlib.metadata as metadata\n"
        "from pathlib import Path\n"
        f"assert tuple(sys.version_info[:3]) == {tuple(sys.version_info[:3])!r}\n"
        f"expected = {expected!r}\n"
        "for name, pinned in expected.items():\n"
        "    distribution = metadata.distribution(name)\n"
        "    assert distribution.version == pinned['version']\n"
        "    assert str(Path(distribution.locate_file('')).resolve()) == pinned['root']\n"
        "sys.stdout.reconfigure(encoding='utf-8')\n"
    )
    return [sys._base_executable, "-I", "-S", "-u", "-c", bootstrap + program]


def _start(context, tmp_path, phase):
    source, _, _, actor, candidate_id, candidate_hash = context
    marker = tmp_path / f"{phase}-{uuid4().hex}.json"
    config = {
        "phase": phase,
        "marker": str(marker),
        "http_log": str(tmp_path / "http-attempts.jsonl"),
        "factory_id": source[2].factory_id,
        "candidate_id": candidate_id,
        "candidate_hash": candidate_hash,
        "request_id": "owned-process-publication",
        "user_id": actor.user_id,
        "username": actor.username,
        "origin": str(source[0].base_url),
        "reader_token": source[1]["reader"],
        "writer_token": source[1]["writer"],
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    }
    env["BYOF_PROCESS_DATABASE_URL"] = source[3].url.render_as_string(hide_password=False)
    env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        _python_command(CHILD),
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(config) + "\n")
    process.stdin.flush()
    return process, marker


def _terminate(process):
    if process.poll() is None:
        process.kill()
    output, _ = process.communicate(timeout=10)
    return output


def _barrier(process, marker):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if marker.exists():
            try:
                value = json.loads(marker.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
            else:
                assert value["pid"] == process.pid and process.poll() is None
                return value
        if process.poll() is not None:
            output, _ = process.communicate(timeout=5)
            pytest.fail("Owned worker exited before crash barrier: " + output)
        time.sleep(0.02)
    pytest.fail("Owned worker did not reach crash barrier within 15 seconds")


def _restart(context, tmp_path):
    process, _ = _start(context, tmp_path, "recover")
    try:
        output, _ = process.communicate(timeout=20)
        assert process.returncode == 0, output
        result = json.loads(output.strip())
        assert result["status"] == "completed"
        return result
    finally:
        if process.poll() is None:
            _terminate(process)


def test_owned_interpreter_pid_and_locked_imports_can_be_verified_without_database(tmp_path):
    marker = tmp_path / "offline-worker.json"
    probe = (
        "import json, os\n"
        "from threading import Event\n"
        "from packages.planning import publication\n"
        "from ortools.sat.python import cp_model\n"
        "import psycopg\n"
        "print(json.dumps({'pid': os.getpid(), 'executable': sys.executable, "
        "'dependencies': expected}), flush=True)\n"
        f"Path({str(marker)!r}).write_text(json.dumps({{'pid': os.getpid(), "
        "'phase': 'offline', 'operation_id': None}), encoding='utf-8')\n"
        "Event().wait(45)\n"
    )
    process = subprocess.Popen(
        _python_command(probe),
        cwd=ROOT,
        env={
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        observed = _barrier(process, marker)
        assert observed["pid"] == process.pid
        output = _terminate(process)
        assert process.returncode != 0 and process.poll() is not None
        result = json.loads(output)
        assert result["pid"] == process.pid
        assert Path(result["executable"]).resolve() == Path(sys._base_executable).resolve()
        assert set(result["dependencies"]) == set(DEPENDENCIES)
    finally:
        if process.poll() is None:
            _terminate(process)


def test_distinct_users_approve_and_publish_concurrently_only_one_submission_can_take_effect(
    publishing,
):
    source, reader, writer, first, candidate_id, candidate_hash = publishing
    engine, factory, second_id = source[3], source[2].factory_id, "second-publisher-" + uuid4().hex
    with Session(engine) as db, db.begin():
        db.add(
            User(user_id=second_id, username=second_id, password_hash="not-a-login", active=True)
        )
        db.flush()
        db.add(Membership(user_id=second_id, factory_id=factory, role="planner"))
    second = Principal(
        user_id=second_id, username=second_id, grants=(Grant(factory_id=factory, role="planner"),)
    )
    ready = Barrier(2, timeout=10)

    def publish(actor):
        approval = _approve(publishing, actor=actor, request_id="approve-" + actor.user_id)
        ready.wait()
        try:
            result = commit_publication(
                engine,
                actor,
                factory,
                candidate_id,
                request_id="publish-" + actor.user_id,
                candidate_hash=candidate_hash,
            )
            return actor, approval, result
        except AccessError as exc:
            return actor, approval, exc

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(publish, (first, second)))
        won = [value for value in outcomes if isinstance(value[2], Release)]
        lost = [value for value in outcomes if isinstance(value[2], AccessError)]
        assert len(won) == len(lost) == 1
        assert lost[0][2].code == "UNRESOLVED_PUBLICATION"
        assert won[0][2].release_id in str(lost[0][2])
        rows = _publications(source)
        assert len(rows) == 1 and rows[0].requester_id == won[0][0].user_id
        assert rows[0].document["source_state"] == "PENDING_SOURCE"
        assert _source_actions(source) == []
        with Session(engine) as db:
            approvals = list(
                db.scalars(select(ApprovalRecord).where(ApprovalRecord.factory_id == factory))
            )
            assert {row.approval_id for row in approvals} == {
                value[1].approval_id for value in outcomes
            }
        assert deliver_one(engine, reader, writer)
        assert not deliver_one(engine, reader, writer)
        actions = _source_actions(source)
        assert len(actions) == 1 and actions[0].operation_id == won[0][2].operation_id
        assert snapshot(source).active_plan_hash == candidate_hash
        assert snapshot(source).actuals == ()
        accepted = _publications(source)[0]
        assert accepted.state == "DONE" and accepted.document["source_state"] == "ACTIVE"
        assert accepted.document["execution_state"] == "NOT_STARTED"
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        assert owner.url.database == "byof_test"
        with owner.begin() as db:
            db.execute(delete(Membership).where(Membership.user_id == second_id))
            db.execute(delete(User).where(User.user_id == second_id))
        owner.dispose()


@pytest.mark.parametrize(
    "phase",
    [
        "before_local_commit",
        "after_local_commit",
        "after_claim",
        "after_source_acceptance",
        "after_receipt_commit",
    ],
)
def test_killed_process_preserves_atomic_outbox_and_recovery_queries_original_source_operation(
    publishing, tmp_path, phase
):
    source, _, _, actor, candidate_id, candidate_hash = publishing
    engine = source[3]
    approval = _approve(publishing)
    initial = snapshot(source)
    process, marker = _start(publishing, tmp_path, phase)
    child_pid = process.pid
    log_path = tmp_path / "http-attempts.jsonl"
    try:
        stopped = _barrier(process, marker)
        rows = _publications(source)
        if phase == "before_local_commit":
            assert rows == [], "Uncommitted outbox INSERT must be invisible to other connections"
            assert _source_actions(source) == []
        else:
            assert len(rows) == 1 and rows[0].release_id == stopped["operation_id"]
            expected = (
                "DONE"
                if phase == "after_receipt_commit"
                else "DELIVERING"
                if phase in {"after_claim", "after_source_acceptance"}
                else "QUEUED"
            )
            assert rows[0].state == expected
            assert rows[0].document["local_state"] == "LOCAL_COMMITTED"
            assert rows[0].document["source_state"] == (
                "ACTIVE" if phase == "after_receipt_commit" else "PENDING_SOURCE"
            )
            assert (rows[0].source_receipt is not None) == (phase == "after_receipt_commit")
        actions = _source_actions(source)
        accepted_remotely = phase in {"after_source_acceptance", "after_receipt_commit"}
        assert len(actions) == int(accepted_remotely)
        original_receipt = actions[0].result["receipt_id"] if actions else None
        _terminate(process)
        assert process.returncode != 0 and process.poll() is not None
    finally:
        if process.poll() is None:
            _terminate(process)
    with Session(engine) as db:
        persisted = db.get(ApprovalRecord, approval.approval_id)
        assert persisted.document == approval.model_dump(mode="json")
    if phase in {"after_claim", "after_source_acceptance"}:
        before = _log(log_path)
        fenced = _restart(publishing, tmp_path)
        assert fenced["pid"] != child_pid and not fenced["worked"]
        assert _log(log_path) == before, "Unexpired lease must prevent both query and submit"
        with Session(engine) as db, db.begin():
            row = db.get(Publication, stopped["operation_id"], with_for_update=True)
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    recovered = _restart(publishing, tmp_path)
    assert recovered["pid"] != child_pid
    assert recovered["state"] == "DONE" and recovered["source_state"] == "ACTIVE"
    final_rows = _publications(source)
    assert len(final_rows) == 1
    row = final_rows[0]
    assert row.document["approval_ids"] == [approval.approval_id]
    assert row.candidate_id == candidate_id and row.requester_id == actor.user_id
    assert row.document["candidate_hash"] == candidate_hash
    assert row.document["execution_state"] == "NOT_STARTED"
    if phase != "before_local_commit":
        assert row.release_id == stopped["operation_id"] == recovered["release_id"]
    actions = _source_actions(source)
    assert len(actions) == 1 and actions[0].operation_id == row.release_id
    if original_receipt is not None:
        assert row.source_receipt["receipt_id"] == original_receipt
    attempts = _log(log_path)
    assert sum(value["kind"] == "submit" for value in attempts) == 1
    assert {value["operation_id"] for value in attempts} == {row.release_id}
    assert attempts[0]["kind"] == "query"
    if phase == "after_source_acceptance":
        assert [value["kind"] for value in attempts] == ["query", "submit", "query"]
    final = snapshot(source)
    assert final.active_plan_hash == candidate_hash and final.actuals == initial.actuals == ()
    assert final.inventory == initial.inventory and final.reservations == initial.reservations
    assert final.snapshot_clock == initial.snapshot_clock
    assert not _restart(publishing, tmp_path)["worked"]
    assert len(_publications(source)) == len(_source_actions(source)) == 1
    assert sum(value["kind"] == "submit" for value in _log(log_path)) == 1
