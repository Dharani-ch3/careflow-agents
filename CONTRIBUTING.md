# Contributing

Thanks for taking a look. This is a portfolio project, but issues and pull requests are
welcome, especially ones that make the agents safer or the code easier to follow.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
make install        # pip install -e ".[dev]" and pre-commit hooks
make check          # lint, typecheck, test and evals, the same as CI
```

## A few house rules

- **Synthetic data only.** Never commit real patient information, not even "anonymized"
  samples. `seed.py` is the place to add made-up records.
- **New behaviour comes with a test**, and new request types or policy changes also come
  with eval scenarios (see `docs/architecture.md`, "Adding a new request type").
- **Policy goes in code**, in `planner.py` or the verifier, not in prompts.
- **Writes only happen in `commit`.** If you find yourself calling a write tool anywhere
  else, there's probably a better shape for the change.
- Keep commits small, with messages that say *why* (`verifier: re-read slot before
  commit to catch double-booking`), not just *what*.

## Before you open a PR

- [ ] `make check` passes locally
- [ ] Docs are updated if behaviour changed
- [ ] No secrets, `.env` files or `data/` folders in the diff
