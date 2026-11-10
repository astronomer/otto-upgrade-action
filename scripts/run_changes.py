"""What this run changed in the project, measured against a pre-run snapshot.

  run_changes.py snapshot <project> <workdir>
      Before the run edits anything: record the project's current state
      (tracked and untracked, ignored files excluded) as a git tree id in
      <workdir>/pre-tree. Writes local objects only; nothing is pushed.
  run_changes.py stage <project> <workdir>
      Stage, in the real index, every path that differs from the snapshot:
      modified, added (tracked or not), or deleted.
  run_changes.py diff <project> <workdir>
      Print the patch from the snapshot to the project as it is now.
  run_changes.py revert <project> <workdir> <copy>
      In <copy>, a copy of <project>, put every changed path back to its
      snapshot state, so the copy is "the project before this run".

The verifier's baseline uses `revert` and open-pr.sh uses `stage`, so both
agree on what the run did, and anything the workflow put in place before the
action (a gitignored .env, a secret file, an edited Dockerfile) counts as the
starting state on both sides. Without a snapshot, HEAD is the reference.

Exit 0 on success, 2 when <project> is not in a git work tree (an unexpected
error exits 1 with a traceback).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile


def _git(project: str, *args: str, index: str | None = None, stdin: bytes | None = None) -> bytes:
    # Paths are data here, never patterns: `a[b].py` must not also match `ab.py`.
    env = {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}
    if index is not None:
        env["GIT_INDEX_FILE"] = index
    return subprocess.run(  # noqa: S603 — fixed argv
        ["git", "-C", project, *args],  # noqa: S607
        check=True, capture_output=True, env=env, input=stdin,
    ).stdout


def _tree_of_worktree(project: str, workdir: str, base: str) -> str:
    """Tree id of the project as it is on disk now, on top of <base>.

    Works in a scratch index kept in <workdir>, seeded the first time from the
    real index. Its stat cache means each later call re-hashes only files that
    changed, untracked ones included, instead of the whole project; a one-tree
    `read-tree -m` keeps that cache for entries matching <base>. Copies keep
    the mtime, which git's racy-entry check relies on.
    """
    index = os.path.join(workdir, "worktree.index")
    if not os.path.exists(index):
        real_index = _git(project, "rev-parse", "--path-format=absolute",
                          "--git-path", "index").decode().strip()
        if os.path.exists(real_index):
            shutil.copy2(real_index, index)
    _git(project, "read-tree", "-i", "-m", base, index=index)
    _git(project, "add", "-A", "--", ".", index=index)
    return _git(project, "write-tree", index=index).decode().strip()


def _reference(workdir: str) -> str:
    try:
        with open(os.path.join(workdir, "pre-tree"), encoding="utf-8") as fh:
            return fh.read().strip() or "HEAD"
    except OSError:
        return "HEAD"


# Bytecode that importing DAGs writes is never part of the upgrade.
_BYTECODE = (":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.py[co]")


def _diff(project: str, ref: str, now: str, *args: str) -> bytes:
    # Pathspec magic is off under GIT_LITERAL_PATHSPECS, so this one call
    # takes patterns.
    return _git(project, "--no-literal-pathspecs", "diff", *args, "--relative",
                ref, now, "--", ".", *_BYTECODE)


def changed(project: str, workdir: str) -> list[str]:
    ref = _reference(workdir)
    raw = _diff(project, ref, _tree_of_worktree(project, workdir, ref),
                "--name-only", "--no-renames", "-z")
    return [p.decode() for p in raw.split(b"\0") if p]


def stage(project: str, workdir: str) -> None:
    present, gone = [], []
    for rel in changed(project, workdir):
        (present if os.path.lexists(os.path.join(project, rel)) else gone).append(rel)
    if present:
        _git(project, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul",
             stdin=b"".join(p.encode() + b"\0" for p in present))
    # A path that's gone is a deletion to stage only if the index has it; one
    # that was untracked at snapshot time has nothing to stage.
    if gone:
        # Filtered here rather than passed as arguments: a large change set
        # would exceed the argument-length limit.
        index_paths = set(_git(project, "ls-files", "-z").split(b"\0"))
        tracked_paths = [p.encode() for p in gone if p.encode() in index_paths]
        if tracked_paths:
            _git(project, "update-index", "--force-remove", "-z", "--stdin",
                 stdin=b"".join(p + b"\0" for p in tracked_paths))


def diff(project: str, workdir: str) -> bytes:
    ref = _reference(workdir)
    return _diff(project, ref, _tree_of_worktree(project, workdir, ref))


def _remove(path: str, stop: str) -> None:
    """Remove a file, symlink or directory, then any parents it left empty."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.remove(path)
    parent = os.path.dirname(path)
    while parent != stop and parent.startswith(stop) and os.path.isdir(parent) \
            and not os.listdir(parent):
        os.rmdir(parent)
        parent = os.path.dirname(parent)


def revert(project: str, workdir: str, copy: str) -> None:
    ref = _reference(workdir)
    paths = changed(project, workdir)
    copy = os.path.abspath(copy)
    for rel in paths:
        _remove(os.path.join(copy, rel), copy)
    in_snapshot = set(_git(project, "ls-tree", "-r", "-z", "--name-only", ref).split(b"\0"))
    in_ref = [p.encode() for p in paths if p.encode() in in_snapshot]
    if not in_ref:
        return
    # checkout-index from a scratch index of the snapshot: restores file modes
    # and symlinks and applies the repo's checkout filters, as a checkout does.
    # It writes <prefix><path from the repo root>, so the project's own prefix
    # is stripped when moving the files into the copy.
    project_prefix = _git(project, "rev-parse", "--show-prefix").decode().strip()
    with tempfile.TemporaryDirectory() as tmp:
        index = os.path.join(tmp, "index")
        out = os.path.join(tmp, "out") + os.sep
        _git(project, "read-tree", ref, index=index)
        _git(project, "checkout-index", "-z", "--stdin", f"--prefix={out}", index=index,
             stdin=b"".join(p + b"\0" for p in in_ref))
        restored = os.path.join(out, project_prefix)
        for rel in (p.decode() for p in in_ref):
            target = os.path.join(copy, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(os.path.join(restored, rel), target)


def main() -> int:
    mode, project, workdir = sys.argv[1:4]
    try:
        _git(project, "rev-parse", "--verify", "-q", "HEAD")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return 2
    if mode == "snapshot":
        # A reused WORKDIR (self-hosted runner) may hold an earlier run's index.
        stale = os.path.join(workdir, "worktree.index")
        if os.path.exists(stale):
            os.remove(stale)
        tree = _tree_of_worktree(project, workdir, "HEAD")
        with open(os.path.join(workdir, "pre-tree"), "w", encoding="utf-8") as fh:
            fh.write(tree + "\n")
    elif mode == "stage":
        stage(project, workdir)
    elif mode == "diff":
        sys.stdout.buffer.write(diff(project, workdir))
    elif mode == "revert":
        revert(project, workdir, sys.argv[4])
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
