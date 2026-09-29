"""The ``luma`` command line interface."""
from __future__ import annotations

from .main import main
from .parser import build_parser

__all__ = ["build_parser", "main"]
