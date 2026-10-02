"""Recoverable state/log commits for the single-writer paper bot.

A durable .txn journal is the commit decision. Recovery finishes its exact
appends and state replacement without duplicating rows. Readers can briefly
see logs ahead of state during replay; this is not multi-file snapshot isolation.
Only use with the state-file writer lock held. Do not share output logs between
separate state files or edit them while a transaction is pending.
"""
import base64
import copy
import json
import os
from contextvars import ContextVar
from pathlib import Path

_active = ContextVar("paperbot_transaction", default=None)


class PendingCommit(RuntimeError):
    """Stop the process; the next locked startup must finish its journal."""


def journal_path(path):
    return Path(str(Path(path).resolve()) + ".txn")


def atomic_json(path, value):
    path = Path(path)
    temporary = Path(str(path) + ".tmp")
    # Serialize before touching disk so nonfinite/unsupported data fails cleanly.
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def append_text(path, text):
    transaction = _active.get()
    if transaction is not None:
        transaction.append(path, text.encode("utf-8"))
    else:
        with open(path, "ab") as stream:
            stream.write(text.encode("utf-8"))


def log_is_empty(path):
    transaction = _active.get()
    resolved = str(Path(path).resolve())
    if transaction is not None and resolved in transaction.logs:
        record = transaction.logs[resolved]
        return record["offset"] == 0 and not record["data"]
    return not Path(path).exists() or Path(path).stat().st_size == 0


def _apply_log(record):
    path = Path(record["path"])
    offset = record["offset"]
    payload = base64.b64decode(record["data"], validate=True)
    boundary = base64.b64decode(record["boundary"], validate=True)
    if not path.exists() and offset:
        raise PendingCommit(f"Log was removed while a commit was pending: {path}")
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if size < offset or size > offset + len(payload):
            raise PendingCommit(f"Log length conflicts with pending commit: {path}")
        stream.seek(offset - len(boundary))
        if stream.read(len(boundary)) != boundary:
            raise PendingCommit(f"Log boundary conflicts with pending commit: {path}")
        stream.seek(offset)
        existing = stream.read()
        if existing != payload[:len(existing)]:
            raise PendingCommit(f"Log content conflicts with pending commit: {path}")
        stream.seek(0, os.SEEK_END)
        stream.write(payload[len(existing):])
        stream.flush()
        os.fsync(stream.fileno())


def recover(path):
    """Finish a committed cycle; return True if a journal was present."""
    path = Path(path).resolve()
    journal = journal_path(path)
    if not journal.exists():
        return False
    with journal.open(encoding="utf-8") as stream:
        record = json.load(stream)
    if record.get("schema") != 1 or record.get("state_path") != str(path):
        raise PendingCommit("Journal does not match this state file.")
    for log in record["logs"]:
        _apply_log(log)
    atomic_json(path, record["state"])
    journal.unlink()
    return True


class StateTransaction:
    def __init__(self, path, state):
        self.path = Path(path).resolve()
        self.state = state
        self.logs = {}

    def __enter__(self):
        if _active.get() is not None:
            raise RuntimeError("Nested state transactions are not supported.")
        if journal_path(self.path).exists():
            raise PendingCommit("Recover the pending journal before starting another cycle.")
        self.before = copy.deepcopy(self.state)
        self.token = _active.set(self)
        return self

    def append(self, path, data):
        path = Path(path).resolve()
        if path in (self.path, journal_path(self.path), Path(str(self.path) + ".tmp"), Path(str(journal_path(self.path)) + ".tmp")):
            raise ValueError("A log path cannot overlap the state or transaction files.")
        name = str(path)
        if name not in self.logs:
            size = path.stat().st_size if path.exists() else 0
            boundary = b""
            if size:
                with path.open("rb") as stream:
                    stream.seek(max(0, size - 64))
                    boundary = stream.read()
            self.logs[name] = {"path": name, "offset": size, "boundary": boundary, "data": b""}
        self.logs[name]["data"] += data

    def rollback(self):
        self.state.clear()
        self.state.update(self.before)

    def __exit__(self, exc_type, exc_value, traceback):
        _active.reset(self.token)
        if exc_type is not None:
            self.rollback()
            return False
        logs = [{**r, "data": base64.b64encode(r["data"]).decode("ascii"),
                 "boundary": base64.b64encode(r["boundary"]).decode("ascii")} for r in self.logs.values()]
        record = {"schema": 1, "state_path": str(self.path), "state": self.state, "logs": logs}
        try:
            atomic_json(journal_path(self.path), record)
        except BaseException:
            self.rollback()
            if journal_path(self.path).exists():
                raise PendingCommit("Commit journal exists; restart to recover it.") from None
            raise
        try:
            recover(self.path)
        except BaseException as exc:
            self.rollback()
            raise PendingCommit("Durable cycle needs recovery. Restart the bot with the same state/log paths.") from exc
