# Contributing to Pickable

Pickable reports how public websites work for AI agents and marketing checks.
Contributions should preserve source evidence, explain uncertainty, and keep
public-site fetching safe. Start with the [README](README.md) for product scope
and [architecture decisions](docs/DECISIONS.md) for implementation context.

## Set up a local checkout

Use Python 3.12 or newer. Node.js 22 matches the browser-state test job in CI.
The application uses the Python standard library; no package installation is
required for the commands below. `requirements.txt` contains optional coverage
tooling.

```bash
git clone https://github.com/yunusarda-sudo/pickable.git
cd pickable
git switch -c fix/describe-your-change
python3 -m venv .venv
source .venv/bin/activate
```

External contributors should fork the repository first, clone their fork, and
open a pull request against `yunusarda-sudo/pickable`'s `main` branch.

## Try the demo

```bash
PICKABLE_PUBLIC_URL=http://127.0.0.1:8765 python3 server.py --demo
```

Open <http://127.0.0.1:8765>. Demo mode uses synthetic scan data; a successful
demo does not establish live provider behavior or a public readiness score.
Hosted analysis and local model integration are separate optional features;
see the README before configuring either.

In another terminal, verify the local server's public interfaces without
starting scans or consuming provider quota:

```bash
python3 scripts/verify_live.py --url http://127.0.0.1:8765
```

Stop the server with Ctrl-C when finished. Live operation uses environment
variables; `server.py` does not load `.env` files automatically. Do not put
credentials in commands you intend to share, screenshots, fixtures, or commits.

## Run the checks

Run these commands from the repository root. They match the two jobs in
[the pull request workflow](.github/workflows/tests.yml):

```bash
python3 -m unittest discover tests/ -q
node --test tests/test_web_state.cjs
```

For a bug fix, add a regression test that fails before the fix and passes
afterward. Use fixtures and mocks for provider responses so automated tests do
not depend on live quotas or network availability. Explain any additional
manual checks in the pull request, including whether they used synthetic or
live data. Passing tests prove only the paths they exercise.

## Choose the right files

| Area | Starting points |
| --- | --- |
| Provider integration | `app/adapters/`, `tests/test_adapters.py` |
| Public-site HTTP safety | `app/safe_fetch.py`, `tests/test_safe_fetch.py` |
| Scan execution and persistence | `app/orchestrator.py`, `app/storage.py` |
| Web UI and state | `web/`, `tests/test_web_state.cjs` |
| HTTP interfaces | `server.py`, `docs/UI_BACKEND_CONTRACT.md` |

Read [the web instructions](web/AGENTS.md) before changing the web UI. Preserve
SSRF checks, destination IP validation, redirect protections, redaction, and
request limits when changing network or API code. Keep `UNKNOWN`, `NA`, and
`WARNING` distinct from `FAIL` in reports.

## Open a pull request

Keep each PR focused on one coherent change. Describe the problem, resulting
behavior, checks you ran, and any unresolved limitations. Update relevant
documentation when public behavior or setup changes. Wait for CI and address
review feedback before merging.

Do not commit local databases, scan history, logs, `.env` files, API keys, or
identifying customer data. Use sanitized fixtures for reproducible reports.
If you find a security issue, do not publish credentials, sensitive evidence,
or working exploitation details in a public issue. Use GitHub's private
vulnerability reporting option if the repository offers it; otherwise contact
the maintainer privately before sharing sensitive details.

Credit real contributors accurately. Use `Co-authored-by` trailers only for
people who contributed to that commit, with an email they have agreed to use
for attribution.
