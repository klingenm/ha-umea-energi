"""Credentials, meters and hourly consumption on top of umea-energi-api-client."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from operator import attrgetter
from typing import Any

from homeassistant.util.ssl import client_context
from umea_energi_api_client import (
    ApiError,
    ContractType,
    Credentials,
    MeterPoint,
    Resolution,
    TokenExpiredError,
    UmeaEnergiClient,
)

from .const import (
    API_TIMEZONE,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_EXPIRES_AT,
    Utility,
)


def create_client() -> UmeaEnergiClient:
    """A client for the production API.

    The library opens an httpx client per request; a prebuilt SSL context keeps that from loading
    CA certificates (blocking I/O) in the event loop each time.
    """
    return UmeaEnergiClient(httpx_args={"verify": client_context(), "timeout": 30})


def credentials_to_data(credentials: Credentials) -> dict[str, str | None]:
    """Config entry data for credentials."""
    expires = credentials.refresh_token_expires_at
    return {
        CONF_ACCESS_TOKEN: credentials.access_token,
        CONF_REFRESH_TOKEN: credentials.refresh_token,
        CONF_REFRESH_TOKEN_EXPIRES_AT: expires.isoformat() if expires else None,
    }


def credentials_from_data(data: Mapping[str, Any]) -> Credentials:
    """Credentials stored by `credentials_to_data`."""
    expires = data.get(CONF_REFRESH_TOKEN_EXPIRES_AT)
    return Credentials(
        access_token=data[CONF_ACCESS_TOKEN],
        refresh_token=data.get(CONF_REFRESH_TOKEN),
        refresh_token_expires_at=datetime.fromisoformat(expires) if expires else None,
    )


@dataclass(frozen=True, slots=True)
class Meter:
    """A meter point the integration fetches consumption for."""

    utility: Utility
    meter_point: MeterPoint

    @property
    def key(self) -> str:
        """Identifies the meter across config entries."""
        return f"{self.utility}_{self.meter_point.meter_point_no}"

    @property
    def location(self) -> str:
        """Where the meter is, for names: its address, or else its meter point number."""
        return self.meter_point.address or self.meter_point.meter_point_no


def meters_from_points(points: list[MeterPoint]) -> list[Meter]:
    """Electricity and district heating meters among a customer's meter points.

    An electricity supply contract shares its meter point with the grid contract at the same
    address; the grid contract is used when there is one.
    """
    grid = {p.meter_point_no for p in points if p.contract_type == ContractType.ELECTRICITY_GRID}
    meters: dict[tuple[Utility, str], Meter] = {}
    for point in points:
        match point.contract_type:
            case ContractType.ELECTRICITY_GRID:
                utility = Utility.ELECTRICITY
            case ContractType.ELECTRICITY if point.meter_point_no not in grid:
                utility = Utility.ELECTRICITY
            case ContractType.DISTRICT_HEATING:
                utility = Utility.DISTRICT_HEATING
            case _:
                continue
        meters.setdefault((utility, point.meter_point_no), Meter(utility, point))
    return list(meters.values())


@dataclass(frozen=True, slots=True)
class Reading:
    """Consumption during the hour starting at `start` (UTC), in kWh."""

    start: datetime
    value: float


def parse_readings(data: Any) -> list[Reading]:
    """Readings from a consumption response (`{"consumption": [{"period", "value"}]}`)."""
    items = data.get("consumption") if isinstance(data, dict) else None
    readings: list[Reading] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        period, value = item.get("period"), item.get("value")
        if not isinstance(period, str) or isinstance(value, bool):
            continue
        if not isinstance(value, int | float):
            continue
        try:
            start = datetime.fromisoformat(period)
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=API_TIMEZONE)
        readings.append(Reading(start=start.astimezone(UTC), value=float(value)))
    return sorted(readings, key=attrgetter("start"))


class UmeaEnergiSession:
    """Calls the API with fresh credentials, and reports them when a refresh replaces them.

    Raises `TokenExpiredError` when the session can't be renewed and the user must log in again.
    """

    def __init__(
        self,
        client: UmeaEnergiClient,
        credentials: Credentials,
        on_credentials_changed: Callable[[Credentials], None],
    ) -> None:
        self._client = client
        self._credentials = credentials
        self._on_credentials_changed = on_credentials_changed

    async def async_meters(self, customer_code: str) -> list[Meter]:
        """The customer's electricity and district heating meters."""
        points = await self._call(lambda token: self._client.meter_points(token, customer_code))
        return meters_from_points(points)

    async def async_hourly_consumption(
        self, meter: Meter, start: datetime, end: datetime
    ) -> list[Reading]:
        """Hourly consumption from `start` up to `end` (aware datetimes), oldest first."""
        first_day = start.astimezone(API_TIMEZONE).date()
        # The API takes whole days; ask through the end of `end`'s day.
        last_day = end.astimezone(API_TIMEZONE).date() + timedelta(days=1)
        data = await self._call(
            lambda token: self._client.consumption(
                token, meter.meter_point, first_day, last_day, Resolution.HOUR
            )
        )
        return [r for r in parse_readings(data) if start <= r.start < end]

    async def _call[T](self, request: Callable[[str], Awaitable[T]]) -> T:
        credentials = await self._fresh_credentials()
        try:
            return await request(credentials.access_token)
        except ApiError as err:
            if err.status_code != 401:
                raise
        # The token was rejected before its expiry; refresh once and retry.
        credentials = await self._fresh_credentials(force=True)
        try:
            return await request(credentials.access_token)
        except ApiError as err:
            if err.status_code == 401:
                raise TokenExpiredError("The API rejected a freshly refreshed token") from err
            raise

    async def _fresh_credentials(self, *, force: bool = False) -> Credentials:
        try:
            credentials = await self._client.fresh_credentials(self._credentials, force=force)
        except ApiError as err:
            if err.status_code in (400, 401, 403):
                raise TokenExpiredError(f"The refresh token was rejected: {err}") from err
            raise
        if credentials != self._credentials:
            self._credentials = credentials
            self._on_credentials_changed(credentials)
        return credentials
