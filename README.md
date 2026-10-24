# Umeå Energi for Home Assistant

Custom integration that loads your electricity and district heating consumption from
[Umeå Energi](https://www.umeaenergi.se) into Home Assistant, using
[umea-energi-api-client](https://github.com/klingenm/umea-energi-api-client).

## Features

- Private and business customers.
- Sign in with **BankID** (QR code or BankID on the same device) or with an **emailed login code**
  (with or without a password).
- Hourly consumption is imported as long-term statistics for the **energy dashboard**:
  - `umea_energi:electricity_consumption_<customer>`
  - `umea_energi:district_heating_consumption_<customer>`
- Sensors for each utility: latest full day's consumption, consumption this month, and the time of
  the latest reading.
- Configurable update interval (default 60 minutes). Readings are published with a delay, so the
  most recent hours appear later.
- When the session can no longer be refreshed, Home Assistant asks you to sign in again.

In the energy dashboard, add the electricity statistic under *Electricity grid → Grid consumption*.
District heating has no dedicated category; it can be added as an individual device or, since it is
measured in kWh, under *Gas consumption*.

## Installation

Copy `custom_components/umea_energi` into your Home Assistant `config/custom_components/`
directory (or add this repository to HACS as a custom repository), restart, and add
**Umeå Energi** under *Settings → Devices & services*.

## Development

Tool versions (Python, uv, ruff) are pinned in `mise.toml`.

```sh
mise install     # install pinned tools
mise run sync    # create .venv and install dependencies
mise run check   # lint, typecheck, test
mise run hass    # run Home Assistant on http://localhost:8123 with the integration loaded
mise run change  # describe a change for the next release
```

Changes are tracked with [knope](https://knope.tech) change files in `.changeset/`.
`mise run release` bumps the version, updates the changelog, pushes, and creates a GitHub
release, which HACS picks up as a new version.

Run `mise tasks` to list all available tasks.
