import pytest
from app.main import (
    build_employee_monthly_pipeline,
    build_department_summary_pipeline,
    build_late_leaderboard_pipeline,
    build_department_trend_pipeline,
)

def test_employee_monthly_pipeline_structure():
    pipeline = build_employee_monthly_pipeline("EMP0001", "2026-07")
    assert len(pipeline) == 3
    # First stage must be $match on emp_code and date range
    assert "$match" in pipeline[0]
    assert pipeline[0]["$match"]["emp_code"] == "EMP0001"
    assert pipeline[0]["$match"]["date"]["$gte"] == "2026-07-01"
    assert pipeline[0]["$match"]["date"]["$lte"] == "2026-07-31"
    # Second stage projects dow and fields
    assert "$project" in pipeline[1]
    # Third stage groups
    assert "$group" in pipeline[2]

def test_department_summary_pipeline_structure():
    pipeline = build_department_summary_pipeline("2026-07", "Engineering")
    assert "$match" in pipeline[0]
    assert pipeline[0]["$match"]["department"] == "Engineering"
    assert pipeline[0]["$match"]["joined_on"]["$lte"] == "2026-07-31"
    # Must have $lookup from attendance_logs
    assert "$lookup" in pipeline[1]
    assert pipeline[1]["$lookup"]["from"] == "attendance_logs"
    # Sorting by department
    assert pipeline[-1]["$sort"]["department"] == 1

def test_late_leaderboard_pipeline_structure():
    pipeline = build_late_leaderboard_pipeline("2026-07", 10, "Engineering")
    # Must match date and late_minutes > 0
    assert pipeline[0]["$match"]["late_minutes"]["$gt"] == 0
    # Must group by emp_code
    assert pipeline[1]["$group"]["_id"] == "$emp_code"
    # Must use $setWindowFields with $rank
    set_window = next((stage["$setWindowFields"] for stage in pipeline if "$setWindowFields" in stage), None)
    assert set_window is not None
    assert "$rank" in set_window["output"]["rank"]
    # Must filter rank <= 10
    match_rank = next((stage["$match"] for stage in pipeline if "$match" in stage and "rank" in stage["$match"]), None)
    assert match_rank is not None
    assert match_rank["rank"]["$lte"] == 10

def test_department_trend_pipeline_structure():
    dept_emps = [
        {"emp_code": "EMP0001", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0002", "joined_on": "2026-07-10"},
    ]
    pipeline = build_department_trend_pipeline("Engineering", "2026-07-01", "2026-07-15", dept_emps)
    # Stage 0 matches emp_codes and date range
    assert "$match" in pipeline[0]
    assert pipeline[0]["$match"]["emp_code"]["$in"] == ["EMP0001", "EMP0002"]
    # Stage includes $densify
    densify = next((stage["$densify"] for stage in pipeline if "$densify" in stage), None)
    assert densify is not None
    assert densify["field"] == "date_val"
    assert densify["range"]["unit"] == "day"
    # Stage includes 7d moving average window
    window = next((stage["$setWindowFields"] for stage in pipeline if "$setWindowFields" in stage), None)
    assert window is not None
    assert window["output"]["moving_avg_7d_raw"]["window"]["documents"] == [-6, "current"]
