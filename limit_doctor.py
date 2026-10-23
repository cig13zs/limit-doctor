#!/usr/bin/env python3
"""Runs skills/limit-doctor/limit_doctor.py, which lives with the skill so a skill install gets the script too."""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "skills" / "limit-doctor" / "limit_doctor.py"), run_name="__main__")
