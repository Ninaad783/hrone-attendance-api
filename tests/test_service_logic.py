import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_punch_out_edge_cases():
    # 404 unknown employee
    with patch.object(Collection, "find_one", return_value=None):
        res = client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
        assert res.status_code == 404
        assert "Employee not found" in res.json()["detail"]

    # 404 no punch-in found
    emp_mock = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    def find_one_side_effect(filter, *args, **kwargs):
        if "emp_code" in filter and len(filter) == 1:
            return emp_mock
        return None

    with patch.object(Collection, "find_one", side_effect=find_one_side_effect):
        res = client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
        assert res.status_code == 404
        assert "No punch-in found" in res.json()["detail"]

    # 409 already punched out
    record_already_out = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc),
    }
    def find_one_already_out(filter, *args, **kwargs):
        if "punch_in" in filter:
            return record_already_out
        return emp_mock

    with patch.object(Collection, "find_one", side_effect=find_one_already_out):
        res = client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
        assert res.status_code == 409

    # 422 punched_at <= punch_in
    open_record = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": None,
    }
    def find_one_open(filter, *args, **kwargs):
        if "punch_in" in filter:
            return open_record
        return emp_mock

    with patch.object(Collection, "find_one", side_effect=find_one_open):
        res = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": int(datetime(2026, 7, 6, 3, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        })
        assert res.status_code == 422

    # 422 punched_at > 24 hours after punch_in
    with patch.object(Collection, "find_one", side_effect=find_one_open):
        res = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": int(datetime(2026, 7, 7, 5, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)
        })
        assert res.status_code == 422


def test_strict_epoch_millis_rejection():
    # String timestamps should be rejected with 422
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": "1783312500000"
    })
    assert res.status_code == 422

    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": "1783312500000"
    })
    assert res.status_code == 422

    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": "1783312500000",
        "reason": "testing string rejection",
        "regularized_by": "admin"
    })
    assert res.status_code == 422


def test_regularize_attendance_rules():
    emp_mock = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    record_mock = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    def find_one_regularize(filter, *args, **kwargs):
        if "date" in filter:
            return record_mock
        return emp_mock

    # Setting ABSENT with punch_in must be 422
    with patch.object(Collection, "find_one", side_effect=find_one_regularize):
        res = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "punch_in": int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000),
            "reason": "Correcting to absent",
            "regularized_by": "manager"
        })
        assert res.status_code == 422

    # Request that changes nothing must be 422
    with patch.object(Collection, "find_one", side_effect=find_one_regularize):
        res = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "PRESENT",
            "reason": "No actual changes made",
            "regularized_by": "manager"
        })
        assert res.status_code == 422

    # punch_in changing to different attendance date must be 422
    with patch.object(Collection, "find_one", side_effect=find_one_regularize):
        res = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000),
            "reason": "Change to tomorrow",
            "regularized_by": "manager"
        })
        assert res.status_code == 422

    # Happy path regularize update
    updated_record = {**record_mock, "status": "WFH"}
    with patch.object(Collection, "find_one", side_effect=find_one_regularize), \
         patch.object(Collection, "find_one_and_update", return_value=updated_record):
        res = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "WFH",
            "reason": "Worked from home approved",
            "regularized_by": "manager"
        })
        assert res.status_code == 200
        assert res.json()["status"] == "WFH"

    # Concurrent modification conflict returns 409
    with patch.object(Collection, "find_one", side_effect=find_one_regularize), \
         patch.object(Collection, "find_one_and_update", return_value=None):
        res = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "WFH",
            "reason": "Worked from home approved",
            "regularized_by": "manager"
        })
        assert res.status_code == 409


def test_explain_endpoint_validation():
    # Missing required month for employee_monthly -> 422
    res = client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
    assert res.status_code == 422

    # Missing required emp_code for employee_monthly -> 422
    res = client.get("/admin/explain/employee_monthly?month=2026-07")
    assert res.status_code == 422

    # Missing required month for department_summary -> 422
    res = client.get("/admin/explain/department_summary")
    assert res.status_code == 422

    # Missing required department, from, to for department_trend -> 422
    res = client.get("/admin/explain/department_trend?department=Engineering")
    assert res.status_code == 422
