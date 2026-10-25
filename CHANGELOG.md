# Changelog
## 0.2.0 (2026-10-25)

### Breaking Changes

#### Work with the production API, per meter point

Built on umea-energi-api-client 0.1.1, which logs in, refreshes tokens and fetches consumption the way the production API actually works (the previous version used Umeå Energi's test environment).

- Consumption is fetched per meter point, found from your contracts. Each meter gets its own device, sensors and statistics (`umea_energi:<utility>_consumption_<meter point>`), replacing the per-customer ones; remove and re-add the integration when upgrading.
- Email login needs your Mina sidor password; the API emails a one-time code after the password is accepted.
- The BankID QR code refreshes every second, and an abandoned BankID login is cancelled.
- Logins have one customer, so the customer picker is gone.

## 0.1.0 (2026-10-24)

### Features

#### Initial release

Unofficial Home Assistant integration for Umeå Energi. Sign in with BankID or an emailed login code (private or business customers) and load hourly electricity and district heating consumption into the energy dashboard, with sensors for the latest day, the current month and the latest reading.
