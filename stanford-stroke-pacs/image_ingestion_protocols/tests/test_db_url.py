"""The DB URL must survive passwords with URL metacharacters (regression for
the 2026-09-02 ``could not translate host name "!@localhost"`` failure after
the DB password was rotated to one containing ``@`` and ``!``)."""

import pytest

from execute_image_ingestion_protocol import build_db_url


@pytest.mark.parametrize(
    "password",
    ["plain", "p@ss!word", "with/slash#hash?query", "trailing@!", "sp ace:colon"],
)
def test_build_db_url_round_trips_password(password):
    url = build_db_url("ssc_user", password, "localhost", "5432", "stanford-stroke")
    assert url.username == "ssc_user"
    assert url.password == password
    assert url.host == "localhost"
    assert url.port == 5432
    assert url.database == "stanford-stroke"
    assert url.drivername == "postgresql+psycopg2"


def test_build_db_url_omits_port_when_unset():
    assert build_db_url("u", "p", "h", "", "d").port is None
