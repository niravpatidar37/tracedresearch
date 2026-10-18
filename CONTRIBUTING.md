# Contributing

Thanks for taking a look. Small, focused pull requests are easiest to review.

1. Set up: `uv venv --python 3.12 .venv && uv sync --frozen`.
2. Before you push, run what CI runs:
   ```bash
   uv run ruff check src tests scripts assets && uv run ruff format --check src tests scripts assets
   uv run mypy
   uv run pytest -q          # no secrets needed: every LLM call in the tests is recorded/fake
   ```
3. A change to a security or budget control needs a test that fails when the control is removed
   (see `scripts/mutation_check.py`).
4. Never commit secrets, `.env` files, LLM caches or model weights. CI runs gitleaks.
5. Numbers in the README must come from committed result files under `results/`.

Security issues: please follow [SECURITY.md](SECURITY.md) instead of opening an issue.
