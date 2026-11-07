# Copyright (c) 2026 netcare GmbH. All rights reserved.
# SPDX-License-Identifier: MIT
"""FITNESSE_TOOLSETS is required at import; tests that don't choose one get every mode, no area."""
import os

DEFAULT_TEST_TOOLSETS = "all"
os.environ.setdefault("FITNESSE_TOOLSETS", DEFAULT_TEST_TOOLSETS)
