# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The launch tests start units: stop them however the run ends."""

import pytest
import testunits


@pytest.fixture(autouse=True, scope="session")
def _units():
    """The test units stopped however the run ends (testunits): installed when the tests run, not
    when they are collected."""
    testunits.install()
