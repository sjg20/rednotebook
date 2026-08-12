"""Tests for the git-based sync module."""

import os
import subprocess
import tempfile

import yaml

from rednotebook import sync


def _git(repo, *args):
    """Helper to run git commands in a test repo."""
    subprocess.run(
        ["git", "-C", repo] + list(args),
        capture_output=True, text=True, check=True,
    )


def _write_month(data_dir, filename, data):
    """Write a YAML month file."""
    with open(os.path.join(data_dir, filename), "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True)


def _read_month(data_dir, filename):
    """Read a YAML month file."""
    with open(os.path.join(data_dir, filename), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _make_repo(path):
    """Create a git repo with an initial commit."""
    os.makedirs(path, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    # Create an initial file so we have a commit
    with open(os.path.join(path, ".gitignore"), "w") as f:
        f.write("*.new.txt\n*.old.txt\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")


class TestHelpers:
    def test_is_month_file(self):
        assert sync._is_month_file("2024-03.txt")
        assert sync._is_month_file("1999-12.txt")
        assert not sync._is_month_file("notes.txt")
        assert not sync._is_month_file("2024-03.new.txt")
        assert not sync._is_month_file(".gitignore")

    def test_merge_yaml_day_text_identical(self):
        text = "Hello world"
        assert sync._merge_yaml_day_text(text, text) == text

    def test_merge_yaml_day_text_different(self):
        result = sync._merge_yaml_day_text("Local text", "Remote text")
        assert "Local text" in result
        assert "Remote text" in result
        assert sync.MERGE_MARKER in result

    def test_merge_yaml_day_text_subset(self):
        # If one contains the other, return the larger one
        short = "Hello"
        long = "Hello\n\nMore text"
        assert sync._merge_yaml_day_text(long, short) == long
        assert sync._merge_yaml_day_text(short, long) == long

    def test_merge_yaml_content_text_only(self):
        local = {"text": "Local entry"}
        remote = {"text": "Remote entry"}
        merged = sync._merge_yaml_content(local, remote)
        assert "Local entry" in merged["text"]
        assert "Remote entry" in merged["text"]

    def test_merge_yaml_content_categories(self):
        local = {"text": "Same", "tags": {"work": None}}
        remote = {"text": "Same", "tags": {"personal": None}, "mood": {"happy": None}}
        merged = sync._merge_yaml_content(local, remote)
        assert merged["text"] == "Same"
        assert "work" in merged["tags"]
        assert "personal" in merged["tags"]
        assert "happy" in merged["mood"]


class TestInitRepo:
    def test_init_new_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            os.makedirs(data_dir)
            _write_month(data_dir, "2024-03.txt", {1: {"text": "Hello"}})

            assert sync.init_repo(data_dir)
            assert sync._is_git_repo(data_dir)
            assert sync._has_commits(data_dir)

    def test_init_existing_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            _make_repo(data_dir)

            # Should succeed without error
            assert sync.init_repo(data_dir)

    def test_gitignore_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            os.makedirs(data_dir)
            sync.init_repo(data_dir)

            gitignore = os.path.join(data_dir, ".gitignore")
            assert os.path.exists(gitignore)
            with open(gitignore) as f:
                content = f.read()
            assert "*.new.txt" in content
            assert "*.old.txt" in content
            assert "*.CONFLICT_BACKUP*.txt" in content


class TestCommitChanges:
    def test_commit_new_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            _make_repo(data_dir)
            _write_month(data_dir, "2024-03.txt", {1: {"text": "Hello"}})

            assert sync.commit_changes(data_dir, "Test commit")

    def test_nothing_to_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            _make_repo(data_dir)

            assert not sync.commit_changes(data_dir)

    def test_commit_default_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            _make_repo(data_dir)
            _write_month(data_dir, "2024-03.txt", {1: {"text": "Hello"}})

            assert sync.commit_changes(data_dir)

            # Verify commit message
            result = subprocess.run(
                ["git", "-C", data_dir, "log", "-1", "--format=%s"],
                capture_output=True, text=True, check=True,
            )
            assert result.stdout.strip() == "Journal update"


class TestSetRemote:
    def test_add_remote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            _make_repo(data_dir)

            assert sync.set_remote(data_dir, "/tmp/fake-remote.git")
            assert sync._has_remote(data_dir)

    def test_update_remote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            _make_repo(data_dir)

            sync.set_remote(data_dir, "/tmp/old-remote.git")
            assert sync.set_remote(data_dir, "/tmp/new-remote.git")

            result = subprocess.run(
                ["git", "-C", data_dir, "remote", "get-url", "origin"],
                capture_output=True, text=True, check=True,
            )
            assert result.stdout.strip() == "/tmp/new-remote.git"


class TestPullAndMerge:
    def _make_pair(self, tmpdir):
        """Create a 'remote' bare repo and a 'local' clone."""
        remote_dir = os.path.join(tmpdir, "remote.git")
        local_dir = os.path.join(tmpdir, "local")

        # Create the bare remote
        os.makedirs(remote_dir)
        _git(remote_dir, "init", "--bare")

        # Create local repo and push to remote
        _make_repo(local_dir)
        _git(local_dir, "remote", "add", "origin", remote_dir)
        _write_month(local_dir, "2024-03.txt", {1: {"text": "Day 1 entry"}})
        _git(local_dir, "add", "-A")
        _git(local_dir, "commit", "-m", "Add March data")
        _git(local_dir, "push", "-u", "origin", "master")

        return remote_dir, local_dir

    def test_pull_no_remote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            _make_repo(data_dir)
            # No remote configured - should succeed silently
            assert sync.pull_and_merge(data_dir)

    def test_pull_no_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            remote_dir, local_dir = self._make_pair(tmpdir)
            assert sync.pull_and_merge(local_dir)

    def test_pull_new_remote_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            remote_dir, local_dir = self._make_pair(tmpdir)

            # Simulate another machine pushing changes via a second clone
            other_dir = os.path.join(tmpdir, "other")
            _git(tmpdir, "clone", remote_dir, "other")
            _git(other_dir, "config", "user.email", "test@test.com")
            _git(other_dir, "config", "user.name", "Test")
            _write_month(other_dir, "2024-03.txt", {
                1: {"text": "Day 1 entry"},
                5: {"text": "Day 5 from other machine"},
            })
            _git(other_dir, "add", "-A")
            _git(other_dir, "commit", "-m", "Add day 5")
            _git(other_dir, "push")

            # Now pull into local
            assert sync.pull_and_merge(local_dir)

            # Verify day 5 is present
            data = _read_month(local_dir, "2024-03.txt")
            assert 5 in data
            assert "Day 5 from other machine" in data[5]["text"]

    def test_pull_conflict_different_days(self):
        """Edits to different days in the same month should auto-merge."""
        with tempfile.TemporaryDirectory() as tmpdir:
            remote_dir, local_dir = self._make_pair(tmpdir)

            # Other machine adds day 10
            other_dir = os.path.join(tmpdir, "other")
            _git(tmpdir, "clone", remote_dir, "other")
            _git(other_dir, "config", "user.email", "test@test.com")
            _git(other_dir, "config", "user.name", "Test")
            _write_month(other_dir, "2024-03.txt", {
                1: {"text": "Day 1 entry"},
                10: {"text": "Day 10 from other"},
            })
            _git(other_dir, "add", "-A")
            _git(other_dir, "commit", "-m", "Add day 10")
            _git(other_dir, "push")

            # Local adds day 15
            _write_month(local_dir, "2024-03.txt", {
                1: {"text": "Day 1 entry"},
                15: {"text": "Day 15 from local"},
            })
            _git(local_dir, "add", "-A")
            _git(local_dir, "commit", "-m", "Add day 15")

            # Pull should merge cleanly (different days)
            assert sync.pull_and_merge(local_dir)

            data = _read_month(local_dir, "2024-03.txt")
            assert 1 in data
            assert 10 in data
            assert 15 in data

    def test_pull_conflict_same_day(self):
        """Edits to the same day should be resolved by appending."""
        with tempfile.TemporaryDirectory() as tmpdir:
            remote_dir, local_dir = self._make_pair(tmpdir)

            # Other machine modifies day 1
            other_dir = os.path.join(tmpdir, "other")
            _git(tmpdir, "clone", remote_dir, "other")
            _git(other_dir, "config", "user.email", "test@test.com")
            _git(other_dir, "config", "user.name", "Test")
            _write_month(other_dir, "2024-03.txt", {
                1: {"text": "Day 1 edited on other machine"},
            })
            _git(other_dir, "add", "-A")
            _git(other_dir, "commit", "-m", "Edit day 1")
            _git(other_dir, "push")

            # Local also modifies day 1
            _write_month(local_dir, "2024-03.txt", {
                1: {"text": "Day 1 edited locally"},
            })
            _git(local_dir, "add", "-A")
            _git(local_dir, "commit", "-m", "Edit day 1 locally")

            # Pull should resolve conflict
            assert sync.pull_and_merge(local_dir)

            data = _read_month(local_dir, "2024-03.txt")
            # Both versions should be present
            assert "edited locally" in data[1]["text"]
            assert "edited on other machine" in data[1]["text"]


class TestSync:
    def test_full_sync_no_remote(self):
        """Sync without a remote should just commit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, "data")
            os.makedirs(data_dir)
            _write_month(data_dir, "2024-03.txt", {1: {"text": "Hello"}})

            sync.init_repo(data_dir)
            _git(data_dir, "config", "user.email", "test@test.com")
            _git(data_dir, "config", "user.name", "Test")

            # Modify a file
            _write_month(data_dir, "2024-03.txt", {
                1: {"text": "Hello"},
                2: {"text": "New day"},
            })

            assert sync.sync(data_dir)

            # Verify committed
            assert not sync._has_changes(data_dir)

    def test_full_sync_with_remote(self):
        """Full sync cycle with a remote repo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            remote_dir = os.path.join(tmpdir, "remote.git")
            local_dir = os.path.join(tmpdir, "local")

            os.makedirs(remote_dir)
            _git(remote_dir, "init", "--bare")

            # Set up local
            _make_repo(local_dir)
            _git(local_dir, "remote", "add", "origin", remote_dir)
            _write_month(local_dir, "2024-03.txt", {1: {"text": "Hello"}})
            _git(local_dir, "add", "-A")
            _git(local_dir, "commit", "-m", "initial data")
            _git(local_dir, "push", "-u", "origin", "master")

            # Make a local change
            _write_month(local_dir, "2024-03.txt", {
                1: {"text": "Hello"},
                2: {"text": "New day"},
            })

            assert sync.sync(local_dir)

            # Verify pushed - clone and check
            verify_dir = os.path.join(tmpdir, "verify")
            _git(tmpdir, "clone", remote_dir, "verify")
            data = _read_month(verify_dir, "2024-03.txt")
            assert 2 in data
            assert data[2]["text"] == "New day"


class TestPullOnOpen:
    def test_not_a_git_repo(self):
        """Should return True for non-git directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            assert sync.pull_on_open(tmpdir)

    def test_no_remote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_repo(tmpdir)
            assert sync.pull_on_open(tmpdir)

    def test_pulls_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            remote_dir = os.path.join(tmpdir, "remote.git")
            local_dir = os.path.join(tmpdir, "local")

            os.makedirs(remote_dir)
            _git(remote_dir, "init", "--bare")

            _make_repo(local_dir)
            _git(local_dir, "remote", "add", "origin", remote_dir)
            _write_month(local_dir, "2024-03.txt", {1: {"text": "Hello"}})
            _git(local_dir, "add", "-A")
            _git(local_dir, "commit", "-m", "initial")
            _git(local_dir, "push", "-u", "origin", "master")

            # Simulate remote change
            other_dir = os.path.join(tmpdir, "other")
            _git(tmpdir, "clone", remote_dir, "other")
            _git(other_dir, "config", "user.email", "test@test.com")
            _git(other_dir, "config", "user.name", "Test")
            _write_month(other_dir, "2024-03.txt", {
                1: {"text": "Hello"},
                3: {"text": "From other"},
            })
            _git(other_dir, "add", "-A")
            _git(other_dir, "commit", "-m", "day 3")
            _git(other_dir, "push")

            assert sync.pull_on_open(local_dir)
            data = _read_month(local_dir, "2024-03.txt")
            assert 3 in data
