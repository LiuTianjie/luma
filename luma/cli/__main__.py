"""Allow ``python -m luma.cli``; installed node agents are launched this way."""
from .main import main

raise SystemExit(main())
