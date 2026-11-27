#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""scripts/push-main's control flow, with scripts/act-check stubbed."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


class TestPushMain:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.repo = tmp_path / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/push-main", self.repo / "scripts/push-main")
        self.remote = tmp_path / "remote.git"
        self.run_cmd("git", "init", "-q", "--bare", str(self.remote), cwd=tmp_path)
        self.run_cmd("git", "init", "-q")
        self.run_cmd("git", "config", "user.email", "fixture@example.invalid")
        self.run_cmd("git", "config", "user.name", "Fixture")
        self.run_cmd("git", "remote", "add", "origin", str(self.remote))
        self.run_cmd("git", "commit", "-q", "--allow-empty", "-m", "first")
        self.sha = self.run_cmd("git", "rev-parse", "HEAD").stdout.strip()

    def run_cmd(self, *args, ok=True, cwd=None):
        result = subprocess.run(args, cwd=cwd or self.repo, text=True, capture_output=True)
        if ok:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def stub_check(self, status):
        check = self.repo / "scripts/act-check"
        check.write_text(
            "#!/bin/sh\n"
            'echo "$ACT_CHECK_COMMIT $*" >> calls\n'
            "echo 'act-check results:'\n"
            "echo '  tests-lint passed'\n"
            f"exit {status}\n"
        )
        check.chmod(0o755)

    def remote_main(self):
        result = self.run_cmd(
            "git", "rev-parse", "-q", "--verify", "refs/heads/main", cwd=self.remote, ok=False
        )
        return result.stdout.strip()

    def test_failure_pushes_nothing_and_leaves_no_stamp(self):
        self.stub_check(1)
        result = self.run_cmd("scripts/push-main", ok=False)
        assert result.returncode != 0
        assert "not pushing" in result.stderr
        assert self.remote_main() == ""
        assert not (self.repo / "build/act/passed" / self.sha).exists()

    def test_success_writes_the_stamp_and_pushes(self):
        self.stub_check(0)
        self.run_cmd("scripts/push-main", "--guest")
        assert (self.repo / "calls").read_text() == f"{self.sha} --guest\n"
        stamp = (self.repo / "build/act/passed" / self.sha).read_text()
        assert f"commit {self.sha}" in stamp
        assert "tests-lint passed" in stamp
        assert self.remote_main() == self.sha
