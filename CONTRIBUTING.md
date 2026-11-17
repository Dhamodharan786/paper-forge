# Contributing

Anyone can propose changes by pull request.

1. Fork the repository and create a branch from `main`.
2. Set up as in the README (Python 3.12, `pip install -r requirements.txt`).
3. Make your change and add or update tests. The suite runs offline: `python -m pytest -q`. It needs no API key.
   Some tests use a real `fluentedge-coding-problems` checkout if `forge.local.toml` points at one, and skip otherwise.
4. Open a pull request describing what changed and why.

Ground rules
- Never commit API keys, `forge.local.toml`, or generated papers (`out/`, `cache/`).
- Paper Forge must never commit, push or open a PR on its own: keep the explicit confirmation for each step in `pr.py`.
- Anything that runs model-written code must keep its timeouts.
