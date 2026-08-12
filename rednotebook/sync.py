# -----------------------------------------------------------------------
# Copyright (c) 2024 Simon Glass
#
# RedNotebook is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# RedNotebook is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <https://www.gnu.org/licenses/>.
# -----------------------------------------------------------------------

"""Git-based cloud sync for RedNotebook journals.

Provides automatic synchronisation of journal data across multiple machines
using git as the transport and merge engine. Edits to different days within
the same month file are merged automatically by git. When the same day is
edited on two machines, both versions are kept by appending the remote text.

Usage:
    Enable sync in the configuration with syncEnabled=1 and set
    syncRemoteUrl to a git remote (e.g. a private GitHub/GitLab repo).
    The journal data directory becomes a git repository. On each save,
    changes are committed and pushed. On each open, remote changes are
    pulled and merged.
"""

import logging
import os
import re
import shutil
import subprocess

import yaml

try:
    from yaml import CLoader as Loader
    from yaml import CSafeDumper as Dumper
except ImportError:
    from yaml import Dumper, Loader


# Merge marker for text appended from a remote machine
MERGE_MARKER = "--- synced from remote ---\n"


def _run_git(data_dir, *args, check=True):
    """Run a git command in the data directory.

    Args:
        data_dir: Path to the journal data directory (the git working tree).
        *args: Git subcommand and arguments.
        check: If True, raise on non-zero exit.

    Returns:
        subprocess.CompletedProcess with stdout/stderr captured as text.
    """
    cmd = ["git", "-C", data_dir] + list(args)
    logging.debug("sync: running %s", " ".join(cmd))
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False,
    )
    if check and result.returncode != 0:
        logging.error("sync: git command failed: %s\n%s", " ".join(cmd), result.stderr)
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr,
        )
    return result


def _is_git_repo(data_dir):
    """Check whether data_dir is already a git repository."""
    result = _run_git(data_dir, "rev-parse", "--is-inside-work-tree", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _has_commits(data_dir):
    """Check whether the repo has at least one commit."""
    result = _run_git(data_dir, "rev-parse", "HEAD", check=False)
    return result.returncode == 0


def _has_remote(data_dir, name="origin"):
    """Check whether a remote with the given name exists."""
    result = _run_git(data_dir, "remote", "get-url", name, check=False)
    return result.returncode == 0


def _has_changes(data_dir):
    """Check whether there are uncommitted changes (staged or unstaged)."""
    result = _run_git(data_dir, "status", "--porcelain")
    return bool(result.stdout.strip())


def _get_conflicted_files(data_dir):
    """Return a list of files with merge conflicts."""
    result = _run_git(data_dir, "diff", "--name-only", "--diff-filter=U", check=False)
    if result.returncode != 0:
        return []
    return [f for f in result.stdout.strip().splitlines() if f]


def _is_month_file(filename):
    """Check whether a filename matches the YYYY-MM.txt pattern."""
    return bool(re.match(r"\d{4}-\d{2}\.txt$", os.path.basename(filename)))


def _merge_yaml_day_text(local_text, remote_text):
    """Merge two versions of a day's text.

    If the texts are identical, return one copy. Otherwise concatenate
    them with a marker so the user can reconcile later.
    """
    if local_text == remote_text:
        return local_text
    # Check if one already contains the other (previous merge)
    if remote_text in local_text:
        return local_text
    if local_text in remote_text:
        return remote_text
    return local_text + "\n\n" + MERGE_MARKER + remote_text


def _merge_yaml_content(local_content, remote_content):
    """Merge two YAML day-content dicts (text + categories).

    Categories from both sides are combined. Text is merged with
    _merge_yaml_day_text().
    """
    merged = dict(local_content)

    # Merge text
    local_text = local_content.get("text", "")
    remote_text = remote_content.get("text", "")
    merged["text"] = _merge_yaml_day_text(local_text, remote_text)

    # Merge categories: keep all keys from both sides
    for key, value in remote_content.items():
        if key == "text":
            continue
        if key not in merged:
            merged[key] = value
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            # Merge sub-entries within a category
            merged[key] = {**merged[key], **value}

    return merged


def _resolve_month_file(data_dir, filename):
    """Resolve a merge conflict in a month YAML file.

    Reads the local (ours), remote (theirs), and base versions, then
    merges day-by-day. Days that exist only on one side are kept.
    Days that differ are merged with _merge_yaml_content().

    Returns True if the conflict was resolved, False otherwise.
    """
    filepath = os.path.join(data_dir, filename)

    try:
        # Extract the three merge versions
        base_result = _run_git(data_dir, "show", f":1:{filename}", check=False)
        ours_result = _run_git(data_dir, "show", f":2:{filename}", check=False)
        theirs_result = _run_git(data_dir, "show", f":3:{filename}", check=False)

        base_data = yaml.load(base_result.stdout, Loader=Loader) if base_result.returncode == 0 else {}
        ours_data = yaml.load(ours_result.stdout, Loader=Loader) if ours_result.returncode == 0 else {}
        theirs_data = yaml.load(theirs_result.stdout, Loader=Loader) if theirs_result.returncode == 0 else {}

        base_data = base_data or {}
        ours_data = ours_data or {}
        theirs_data = theirs_data or {}
    except yaml.YAMLError as exc:
        logging.error("sync: failed to parse YAML during conflict resolution: %s", exc)
        return False

    # Merge day by day
    all_days = set(ours_data.keys()) | set(theirs_data.keys())
    merged = {}

    for day in sorted(all_days):
        ours_day = ours_data.get(day)
        theirs_day = theirs_data.get(day)

        if ours_day and not theirs_day:
            merged[day] = ours_day
        elif theirs_day and not ours_day:
            merged[day] = theirs_day
        elif ours_day == theirs_day:
            merged[day] = ours_day
        else:
            # Both sides modified the same day - merge content
            merged[day] = _merge_yaml_content(ours_day, theirs_day)

    # Write the resolved file
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(merged, f, Dumper=Dumper, allow_unicode=True)
        _run_git(data_dir, "add", filename)
        return True
    except OSError as exc:
        logging.error("sync: failed to write resolved file %s: %s", filepath, exc)
        return False


def _write_gitignore(data_dir):
    """Ensure a .gitignore exists that excludes temporary/backup files."""
    gitignore_path = os.path.join(data_dir, ".gitignore")
    patterns = [
        "*.new.txt",
        "*.old.txt",
        "*.CONFLICT_BACKUP*.txt",
    ]
    existing = ""
    if os.path.exists(gitignore_path):
        with open(gitignore_path, encoding="utf-8") as f:
            existing = f.read()

    missing = [p for p in patterns if p not in existing]
    if missing:
        with open(gitignore_path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            for p in missing:
                f.write(p + "\n")


def init_repo(data_dir):
    """Initialise the data directory as a git repository if needed.

    Creates the repo, adds a .gitignore, and makes an initial commit
    if one does not already exist.

    Returns True if the repo is ready, False on failure.
    """
    try:
        if not _is_git_repo(data_dir):
            _run_git(data_dir, "init")
            logging.info("sync: initialised git repo in %s", data_dir)

        _write_gitignore(data_dir)

        if not _has_commits(data_dir):
            _run_git(data_dir, "add", "-A")
            _run_git(data_dir, "commit", "-m", "Initial journal commit")
            logging.info("sync: created initial commit")

        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        logging.error("sync: failed to initialise repo: %s", exc)
        return False


def set_remote(data_dir, url, name="origin"):
    """Set or update the git remote URL.

    Returns True on success.
    """
    try:
        if _has_remote(data_dir, name):
            _run_git(data_dir, "remote", "set-url", name, url)
        else:
            _run_git(data_dir, "remote", "add", name, url)
        logging.info("sync: remote '%s' set to %s", name, url)
        return True
    except subprocess.CalledProcessError as exc:
        logging.error("sync: failed to set remote: %s", exc)
        return False


def commit_changes(data_dir, message=None):
    """Stage all changes and create a commit.

    Args:
        data_dir: Path to the journal data directory.
        message: Optional commit message. A default is generated if omitted.

    Returns True if a commit was created, False if there was nothing to
    commit or on error.
    """
    try:
        if not _has_changes(data_dir):
            logging.debug("sync: nothing to commit")
            return False

        _run_git(data_dir, "add", "-A")

        if not message:
            message = "Journal update"

        _run_git(data_dir, "commit", "-m", message)
        logging.info("sync: committed changes")
        return True
    except subprocess.CalledProcessError as exc:
        logging.error("sync: commit failed: %s", exc)
        return False


def pull_and_merge(data_dir, remote="origin", branch=None):
    """Pull from the remote and merge, resolving conflicts in YAML files.

    Args:
        data_dir: Path to the journal data directory.
        remote: Remote name (default "origin").
        branch: Branch to pull. If None, uses the current branch.

    Returns True if the pull succeeded (including conflict resolution),
    False on unresolvable errors.
    """
    if not _has_remote(data_dir, remote):
        logging.debug("sync: no remote '%s' configured, skipping pull", remote)
        return True

    try:
        # Fetch first so we can check if there is anything new
        _run_git(data_dir, "fetch", remote)
    except subprocess.CalledProcessError as exc:
        logging.warning("sync: fetch failed (network unavailable?): %s", exc)
        return False

    # Determine the branch to merge
    if not branch:
        result = _run_git(data_dir, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        branch = result.stdout.strip() if result.returncode == 0 else "main"

    # Check if there is anything to merge
    remote_ref = f"{remote}/{branch}"
    result = _run_git(data_dir, "rev-parse", remote_ref, check=False)
    if result.returncode != 0:
        logging.debug("sync: remote branch %s does not exist yet", remote_ref)
        return True

    # Try the merge
    merge_result = _run_git(data_dir, "merge", remote_ref, check=False)

    if merge_result.returncode == 0:
        logging.info("sync: pull and merge succeeded")
        return True

    # Handle conflicts
    conflicted = _get_conflicted_files(data_dir)
    if not conflicted:
        logging.error("sync: merge failed but no conflicts found:\n%s", merge_result.stderr)
        _run_git(data_dir, "merge", "--abort", check=False)
        return False

    logging.info("sync: resolving %d conflicted file(s)", len(conflicted))

    all_resolved = True
    for filename in conflicted:
        if _is_month_file(filename):
            if _resolve_month_file(data_dir, filename):
                logging.info("sync: resolved conflict in %s", filename)
            else:
                logging.error("sync: failed to resolve conflict in %s", filename)
                all_resolved = False
        else:
            # For non-month files (e.g. .gitignore), keep ours
            _run_git(data_dir, "checkout", "--ours", filename)
            _run_git(data_dir, "add", filename)
            logging.info("sync: kept local version of %s", filename)

    if all_resolved:
        _run_git(data_dir, "commit", "--no-edit")
        logging.info("sync: merge conflict resolution committed")
        return True
    else:
        _run_git(data_dir, "merge", "--abort", check=False)
        logging.error("sync: could not resolve all conflicts, merge aborted")
        return False


def push(data_dir, remote="origin", branch=None):
    """Push committed changes to the remote.

    Args:
        data_dir: Path to the journal data directory.
        remote: Remote name (default "origin").
        branch: Branch to push. If None, pushes the current branch.

    Returns True on success, False on failure.
    """
    if not _has_remote(data_dir, remote):
        logging.debug("sync: no remote '%s' configured, skipping push", remote)
        return True

    try:
        cmd = ["push", "-u", remote]
        if branch:
            cmd.append(branch)
        _run_git(data_dir, *cmd)
        logging.info("sync: pushed to %s", remote)
        return True
    except subprocess.CalledProcessError as exc:
        logging.warning("sync: push failed: %s", exc)
        return False


def sync(data_dir, remote="origin", branch=None):
    """Perform a full sync cycle: commit, pull+merge, push.

    This is the main entry point for the sync system. Call it after
    saving journal data to disk.

    Args:
        data_dir: Path to the journal data directory.
        remote: Remote name.
        branch: Branch name. If None, uses the current branch.

    Returns True if sync completed successfully, False on any failure.
    """
    logging.info("sync: starting sync cycle")

    if not _is_git_repo(data_dir):
        if not init_repo(data_dir):
            return False

    # 1. Commit any local changes
    commit_changes(data_dir)

    # 2. Pull and merge remote changes
    if not pull_and_merge(data_dir, remote, branch):
        return False

    # 3. Push our changes
    if not push(data_dir, remote, branch):
        return False

    logging.info("sync: sync cycle complete")
    return True


def pull_on_open(data_dir, remote="origin", branch=None):
    """Pull remote changes before opening the journal.

    Similar to sync() but without committing or pushing - just fetch
    and merge so the user sees the latest data from other machines.

    Returns True on success.
    """
    if not _is_git_repo(data_dir):
        return True  # Not a sync-enabled journal

    if not _has_remote(data_dir, remote):
        return True

    logging.info("sync: pulling latest changes before opening journal")

    # Commit any uncommitted local changes first (e.g. from a crash)
    if _has_changes(data_dir):
        commit_changes(data_dir, "Auto-commit before pull (uncommitted changes found)")

    return pull_and_merge(data_dir, remote, branch)
