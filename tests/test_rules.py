import pytest
from datetime import datetime, timezone, timedelta, time, date
from decimal import Decimal
from app.main import (
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
    get_attendance_date,
    round_half_up,
    ms_to_utc_dt,
    dt_to_epoch_ms,
    EmployeeIn,
    PunchInIn,
    RegularizeIn,
    IST
)

def test_r1_attendance_date():
    # Regular shift 09:30 - 18:30
    # Punch-in at 09:35 IST on 2026-07-06 -> UTC 2026-07-06 04:05:00
    punch = datetime(2026, 7, 6, 4, 5, 0, tzinfo=timezone.utc)
    assert get_attendance_date(punch, "09:30", "18:30") == "2026-07-06"

    # Overnight shift 22:00 - 06:00
    # Punch-in at 21:55 IST on 2026-07-06 -> UTC 2026-07-06 16:25:00
    punch_night = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert get_attendance_date(punch_night, "22:00", "06:00") == "2026-07-06"

    # Overnight shift 22:00 - 06:00
    # Punch-in at 01:10 IST on 2026-07-07 -> UTC 2026-07-06 19:40:00
    # Since 01:10 IST < 06:00 IST (shift_end), it belongs to previous day shift (2026-07-06)!
    punch_early_morning = datetime(2026, 7, 6, 19, 40, 0, tzinfo=timezone.utc)
    assert get_attendance_date(punch_early_morning, "22:00", "06:00") == "2026-07-06"

    # Punch-in at 07:00 IST on 2026-07-07 -> UTC 2026-07-07 01:30:00
    # Since 07:00 >= 06:00, belongs to today's shift (2026-07-07)
    punch_after_end = datetime(2026, 7, 7, 1, 30, 0, tzinfo=timezone.utc)
    assert get_attendance_date(punch_after_end, "22:00", "06:00") == "2026-07-07"


def test_r2_late_minutes():
    # Shift 09:30 IST
    # 09:40:00 -> elapsed = 10 min 00 sec -> not strictly more than 10:00 -> 0
    dt_940 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc) # 09:40:00 IST
    assert compute_late_minutes(dt_940, "2026-07-06", "09:30") == 0

    # 09:40:01 -> elapsed = 601 sec -> strictly > 600 sec -> floor(601/60) = 10
    dt_940_01 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_940_01, "2026-07-06", "09:30") == 10

    # 10:15:59 -> elapsed = 45m 59s -> strictly > 600 sec -> 45
    dt_1015 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_1015, "2026-07-06", "09:30") == 45

    # Early arrival 09:15:00 -> 0
    dt_early = datetime(2026, 7, 6, 3, 45, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_early, "2026-07-06", "09:30") == 0


def test_r3_overtime():
    # Shift 09:30 - 18:30 on 2026-07-06
    # Punch-out at 18:55 (25 min OT) -> < 30 min -> 0
    dt_1855 = datetime(2026, 7, 6, 13, 25, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_1855, "2026-07-06", "09:30", "18:30") == 0

    # Punch-out at 19:00 (30 min OT) -> >= 30 min -> 30
    dt_1900 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_1900, "2026-07-06", "09:30", "18:30") == 30

    # Punch-out at 19:10 (40 min OT) -> >= 30 min -> 40
    dt_1910 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_1910, "2026-07-06", "09:30", "18:30") == 40

    # Overnight shift: 22:00 - 06:00 on 2026-07-06
    # Shift end is 2026-07-07 06:00:00 IST.
    # Punch out at 2026-07-07 06:40:00 IST (UTC 2026-07-07 01:10:00) -> 40 min OT
    dt_overnight_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_overnight_out, "2026-07-06", "22:00", "06:00") == 40


def test_r4_work_hours():
    # Sample 1: 03:58:00 to 13:05:00 UTC -> 9 hours 7 minutes = 32820 seconds -> 9.11666... -> 9.12
    p_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    p_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_work_hours(p_in, p_out) == 9.12

    # Half-up rounding check: 2.555 hours -> 2.56
    # 2.555 * 3600 = 9198 seconds
    p_in2 = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
    p_out2 = p_in2 + timedelta(seconds=9198)
    assert compute_work_hours(p_in2, p_out2) == 2.56


def test_r5_half_day():
    assert compute_half_day(None) is False
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(3.50) is True
    assert compute_half_day(9.12) is False


def test_employee_validation():
    # Valid employee
    emp = EmployeeIn(
        emp_code="EMP0001",
        name="Test",
        email="test@example.com",
        department="Engineering",
        shift_start="09:30",
        shift_end="18:30",
        joined_on="2026-01-01"
    )
    assert emp.emp_code == "EMP0001"

    # Equal shift_start and shift_end must fail
    with pytest.raises(ValueError):
        EmployeeIn(
            emp_code="EMP0001",
            name="Test",
            email="test@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="09:30",
            joined_on="2026-01-01"
        )

    # Invalid emp_code pattern
    with pytest.raises(ValueError):
        EmployeeIn(
            emp_code="INVALID",
            name="Test",
            email="test@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="18:30",
            joined_on="2026-01-01"
        )
