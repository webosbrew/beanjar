#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the real formatting hooks in isolated repositories."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
# what install-hooks.py needs on PATH (the CI's lint job has them all)
MISSING = [name for name in ("pre-commit", "ruff", "clang-format") if not shutil.which(name)]


@pytest.mark.skipif(bool(MISSING), reason=f"not installed: {', '.join(MISSING)}")
class TestHooks:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        base = tmp_path
        self.repo = base / "repo"
        self.repo.mkdir()
        inherited = base / "inherited-hooks"
        inherited.mkdir()
        commit_msg = inherited / "commit-msg"
        commit_msg.write_text("#!/bin/sh\nprintf 'called\\n' >> .git/commit-msg-calls\n")
        commit_msg.chmod(0o755)
        self.original_hook = commit_msg.read_bytes()
        self.commit_msg = commit_msg
        global_config = base / "gitconfig"
        global_config.write_text(f'[core]\n\thooksPath = "{inherited}"\n')
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=str(global_config), GIT_CONFIG_NOSYSTEM="1")
        self.env.pop("PRE_COMMIT_ALLOW_NO_CONFIG", None)
        for name in (
            ".pre-commit-config.yaml",
            ".clang-format",
            "pyproject.toml",
            "scripts/format-hooks/install-hooks.py",
            "scripts/format-hooks/pre-commit.sh",
            "scripts/format-hooks/pre-push.sh",
            "scripts/format-hooks/python-format.py",
            "scripts/format",
        ):
            dest = self.repo / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, dest)
        (self.repo / "build").mkdir()
        (self.repo / ".gitignore").write_text("build/\n")
        self.run_cmd("git", "init", "-q")
        self.run_cmd("git", "config", "user.email", "fixture@example.invalid")
        self.run_cmd("git", "config", "user.name", "Formatting fixture")
        self.run_cmd("python3", "scripts/format-hooks/install-hooks.py")
        self.run_cmd("git", "add", ".")
        self.run_cmd("git", "commit", "-qm", "fixture")

    def run_cmd(self, *args, ok=True):
        result = subprocess.run(args, cwd=self.repo, env=self.env, text=True, capture_output=True)
        if ok:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def test_reformat_blocks_commit_and_preserves_commit_msg(self):
        source = self.repo / "sample.py"
        source.write_text("x=1\n")
        self.run_cmd("git", "add", "sample.py")
        result = self.run_cmd("git", "commit", "-qm", "unformatted", ok=False)
        assert result.returncode != 0
        assert source.read_text() == "x = 1\n"
        assert self.run_cmd("git", "show", ":sample.py").stdout == "x=1\n"
        self.run_cmd("git", "add", "sample.py")
        self.run_cmd("git", "commit", "-qm", "formatted")
        assert (self.repo / ".git/commit-msg-calls").read_text() == "called\ncalled\n"
        assert self.commit_msg.read_bytes() == self.original_hook
        self.run_cmd("python3", "scripts/format-hooks/install-hooks.py")

    def github_remote(self):
        # the gate applies to main on GitHub only: a path with github.com/ in it stands for it
        bare = self.repo.parent / "github.com" / "remote.git"
        bare.parent.mkdir()
        self.run_cmd("git", "init", "-q", "--bare", str(bare))
        self.run_cmd("git", "remote", "add", "origin", str(bare))
        return bare

    def test_pre_push_wants_a_stamp_for_main_on_github(self):
        self.github_remote()
        head = self.run_cmd("git", "rev-parse", "HEAD").stdout.strip()
        result = self.run_cmd("git", "push", "-q", "origin", "HEAD:refs/heads/main", ok=False)
        assert result.returncode != 0
        assert "scripts/push-main" in result.stderr
        assert head in result.stderr
        stamp = self.repo / "build/act/passed" / head
        stamp.parent.mkdir(parents=True)
        stamp.write_text("commit " + head + "\n")
        self.run_cmd("git", "push", "-q", "origin", "HEAD:refs/heads/main")
        # a new commit has no stamp; --no-verify skips the hook as for any hook
        self.run_cmd("git", "commit", "-q", "--allow-empty", "-m", "another")
        self.run_cmd("git", "push", "-q", "origin", "HEAD:refs/heads/main", ok=False)
        self.run_cmd("git", "push", "-q", "--no-verify", "origin", "HEAD:refs/heads/main")

    def test_pre_push_skips_other_hosts_and_branches(self):
        sync = self.repo.parent / "host-sync.git"  # a source sync to one of the project's hosts
        self.run_cmd("git", "init", "-q", "--bare", str(sync))
        self.run_cmd("git", "push", "-q", str(sync), "HEAD:refs/heads/main")
        github = self.repo.parent / "github.com" / "other.git"
        github.parent.mkdir()
        self.run_cmd("git", "init", "-q", "--bare", str(github))
        self.run_cmd("git", "push", "-q", str(github), "HEAD:refs/heads/topic")

    def test_partial_staging_restores_unstaged_edits(self):
        source = self.repo / "sample.py"
        padding = "\n" + "\n".join(f"# padding {n}" for n in range(20)) + "\n"
        source.write_text("x = 1\n" + padding + "y = 1\n")
        self.run_cmd("git", "add", "sample.py")
        self.run_cmd("git", "commit", "-qm", "base")
        staged = "x=2\n" + padding + "y = 1\n"
        source.write_text(staged)
        self.run_cmd("git", "add", "sample.py")
        source.write_text("x=2\n" + padding + "y = 3  # unstaged\n")
        result = self.run_cmd("git", "commit", "-qm", "partial", ok=False)
        assert result.returncode != 0
        assert self.run_cmd("git", "show", ":sample.py").stdout == staged
        assert source.read_text() == "x = 2\n" + padding + "y = 3  # unstaged\n"

    def test_partial_staging_conflict_rolls_back_formatter(self):
        source = self.repo / "sample.py"
        source.write_text("x = 1\n")
        self.run_cmd("git", "add", "sample.py")
        self.run_cmd("git", "commit", "-qm", "base")
        source.write_text("x=2\n")
        self.run_cmd("git", "add", "sample.py")
        source.write_text("x=3 # unstaged\n")
        result = self.run_cmd("git", "commit", "-qm", "conflict", ok=False)
        assert result.returncode != 0
        assert "Rolling back fixes" in result.stdout + result.stderr
        assert source.read_text() == "x=3 # unstaged\n"
        assert self.run_cmd("git", "show", ":sample.py").stdout == "x=2\n"

    def test_scope_and_nonmutating_check(self):
        files = {
            "sample.h": "int f(void){return 1;}\n",
            "runner": "#!/usr/bin/env python3\nx=1\n",
            "nonexec-runner": "#!/usr/bin/python\nx=1\n",
            "vendor/sample.py": "x=1\n",
            "build/sample.py": "x=1\n",
            "scratch/sample.c": "int f(void){return 1;}\n",
            "host/ui/beanjar_ui/viewer/_core_abi.py": "x=1\n",
        }
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (self.repo / "runner").chmod(0o755)
        self.run_cmd("git", "add", "-f", *files)
        result = self.run_cmd("scripts/format", "check", ok=False)
        assert result.returncode != 0
        for name, content in files.items():
            assert (self.repo / name).read_text() == content
        result = self.run_cmd("scripts/format", "all", ok=False)
        assert result.returncode != 0
        assert (self.repo / "runner").read_text() == "#!/usr/bin/env python3\nx = 1\n"
        assert "int f(void)\n{" in (self.repo / "sample.h").read_text()
        assert (self.repo / "nonexec-runner").read_text() == "#!/usr/bin/python\nx = 1\n"
        for name in list(files)[3:]:
            assert (self.repo / name).read_text() == files[name]
        self.run_cmd("scripts/format", "check")
