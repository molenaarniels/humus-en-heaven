"""Pytest bootstrap: make the repo root importable so tests can do
``import soil_model`` / ``import notify`` regardless of pytest's import mode.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
