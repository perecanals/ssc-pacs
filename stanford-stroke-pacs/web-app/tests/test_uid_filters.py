"""UID header filters match substrings and combine with other browsing filters."""

import pytest


@pytest.mark.parametrize("endpoint,field,fragment,uid", [
    ("/api/studies", "studyinstanceuid", "3.4", "1.2.3.4.5"),
    ("/api/series", "studyinstanceuid", "3.4", "1.2.3.4.5"),
    ("/api/series", "seriesinstanceuid", "5.6", "1.2.3.4.5.6"),
])
def test_uid_substring_filter(logged_in_client, endpoint, field, fragment, uid):
    response = logged_in_client.get(endpoint, params={field: fragment})
    assert response.status_code == 200
    body = response.json()
    rows = body["items" if endpoint == "/api/studies" else "series"]
    assert body["total"] == len(rows) == 1
    assert rows[0][field] == uid

    response = logged_in_client.get(endpoint, params={field: "missing-uid"})
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_series_uid_filters_combine(logged_in_client):
    params = {"studyinstanceuid": "3.4", "seriesinstanceuid": "5.6"}
    response = logged_in_client.get("/api/series", params=params)
    assert response.status_code == 200
    assert response.json()["total"] == 1
    params["studyinstanceuid"] = "2.2.2"
    response = logged_in_client.get("/api/series", params=params)
    assert response.status_code == 200
    assert response.json()["total"] == 0
