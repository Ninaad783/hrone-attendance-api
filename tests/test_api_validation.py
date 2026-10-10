import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
from app.main import app

client = TestClient(app)

def test_health_success():
    with patch("app.main.admin_db.command", return_value={"ok": 1}):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

def test_health_failure():
    with patch("app.main.admin_db.command", side_effect=Exception("Connection refused")):
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {"detail": "Database unavailable"}

def test_create_employee_validation_errors():
    # Bad emp_code
    res = client.post("/employees", json={
        "emp_code": "WRONG",
        "name": "A",
        "email": "a@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # Equal shift_start and shift_end
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "A",
        "email": "a@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # Invalid email
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "A",
        "email": "not-an-email",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # Invalid date
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "A",
        "email": "a@example.com",
        "department": "Engineering",
        "joined_on": "2026-02-31"
    })
    assert res.status_code == 422

def test_attendance_list_date_range_validation():
    # date_from > date_to must return 422
    res = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res.status_code == 422

    # Invalid page number < 1
    res = client.get("/attendance?page=0")
    assert res.status_code == 422

    # Invalid page_size > 100
    res = client.get("/attendance?page_size=101")
    assert res.status_code == 422

def test_punch_in_validation():
    # Missing emp_code
    res = client.post("/attendance/punch-in", json={})
    assert res.status_code == 422

    # Out of range punched_at (seconds instead of epoch ms)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1718000000 # 10 digits
    })
    assert res.status_code == 422

    # Invalid status
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "status": "INVALID_STATUS"
    })
    assert res.status_code == 422

def test_regularize_validation():
    # Changing nothing (missing reason or regularized_by)
    res = client.patch("/attendance/EMP0001/2026-07-06", json={})
    assert res.status_code == 422

    # Reason too short
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "reason": "bad",
        "regularized_by": "admin"
    })
    assert res.status_code == 422
