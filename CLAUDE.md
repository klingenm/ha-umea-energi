# ha-umea-energi

Home Assistant custom integration (`umea_energi`) that signs in to Umeå Energi's session API
(BankID or emailed one-time code) and loads electricity and district heating consumption.
API access goes through [umea-energi-api-client](https://github.com/klingenm/umea-energi-api-client)
(published on PyPI).

## Layout

- `custom_components/umea_energi/` — the integration
  - `api.py` — session wrapper over the generated client: BankID/email login, token refresh,
    hourly consumption. Keep Home Assistant-specific code out of it.
  - `config_flow.py` — account type (private/business) and sign-in method, BankID progress step,
    email code, customer choice, reauth, options (update interval)
  - `coordinator.py` — fetches consumption, imports statistics, summarizes for sensors
  - `statistics.py` — external long-term statistics (`umea_energi:<utility>_consumption_<customer>`)
  - `sensor.py` — per-utility sensors (latest day, this month, latest reading)
  - `translations/` — `en.json` and `sv.json`; keep keys in sync
- `tests/` — pytest with `pytest-homeassistant-custom-component`
- `config/` — local dev instance for `mise run hass` (`custom_components` is a symlink)
- `pyproject.toml` — dependencies and ruff/ty/pytest config (not a package)
- `mise.toml` — pinned tool versions (python, uv, ruff), env, and project tasks
- `mise.lock` — exact tool versions and checksums per platform (`mise lock`; never edit by hand)

## Tooling

- **mise** manages tool versions and tasks. See the `mise` skill. Never install tools any other way.
- **uv** manages Python dependencies and the `.venv`. Use `uv add <pkg>` / `uv add --dev <pkg>`;
  never edit `uv.lock` by hand or use `pip`. Runtime dependencies must also be listed in
  `requirements` in `manifest.json`.
- API gaps belong in `umea-energi-api-client` (generated from OpenAPI), not vendored here. To work
  on both at once, temporarily `uv add --editable ../umea-energi-api-client`, and switch back to
  the PyPI release before committing.
- **GitButler** (`but`) handles version control. See the `gitbutler` skill. Don't use git write commands.

## Commands

```sh
mise run sync       # install deps into .venv
mise run format     # format + autofix lint
mise run check      # lint + typecheck + test — run before every commit
mise run test -- -k <name>
mise run hass       # run Home Assistant locally with the integration loaded
```

## Conventions

- Python 3.14+, targeting the Home Assistant version pinned via `pytest-homeassistant-custom-component`.
  Follow current HA core idioms: `probatio` schemas (not voluptuous), `@override`,
  `ConfigEntry[...]` runtime data, translation keys instead of hard-coded names.
- Type checking is ty via `uv check` (pinned in the `typecheck` task); warnings fail the check.
  Annotate every function, including tests.
- Ruff line length is 100.
- Don't hit the real API in tests: mock HTTP with `httpx.MockTransport` in `test_api.py`, and
  patch `UmeaEnergiApi` methods in Home Assistant tests.
- Commit messages follow `type(scope): summary`.

## Unverified API assumptions

The OpenAPI document leaves these open; check them against the real API before relying on them:

- `emailStep1` (email + password) and `emailWithKey` (email only) are assumed to email a code
  that `emailStep2` exchanges for a session.
- `qrCode` from BankID may be an image or a QR payload; `config_flow._qr_code_markdown` handles both.
- `refresh-token` reads the refresh token from the `refreshToken` cookie; its response body is
  undocumented, so `api._tokens_from_refresh_payload` accepts a session object or a bare token.
- Consumption without `MeterPointNo` is assumed to cover all of the customer's meter points, and
  naive timestamps are assumed to be Swedish local time.
