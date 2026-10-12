"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; process env wins)
"""
from __future__ import annotations

import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from bson.json_util import dumps as bson_dumps
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
ABSENCE = ("ABSENT", "LEAVE")
ALL_STATUSES = PRESENCE + ABSENCE
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def ensure_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_list"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in"
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        pass
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time / rounding helpers
# --------------------------------------------------------------------------- #
def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def truncate_seconds(dt: datetime) -> datetime:
    return as_utc(dt).replace(microsecond=0)


def now_utc() -> datetime:
    return truncate_seconds(datetime.now(timezone.utc))


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return int(truncate_seconds(dt).timestamp() * 1000)


def ms_to_dt(ms: int) -> datetime:
    return truncate_seconds(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def round_half_up(value: float | int | Decimal, places: int) -> float:
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def hm_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch-in, with overnight exception."""
    pi = truncate_seconds(punch_in).astimezone(IST)
    if is_overnight(shift_start, shift_end):
        clock = pi.hour * 60 + pi.minute
        if clock < hm_minutes(shift_end):
            return (pi.date() - timedelta(days=1)).isoformat()
    return pi.date().isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    h, m = map(int, shift_start.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    if is_overnight(shift_start, shift_end):
        d = d + timedelta(days=1)
    h, m = map(int, shift_end.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; minutes from shift start."""
    start = shift_start_dt(date_str, shift_start)
    pi = truncate_seconds(punch_in)
    delta = (pi - start).total_seconds()
    if delta <= 600:
        return 0
    return int(delta // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    seconds = int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end; 0 unless >= 30."""
    end = shift_end_dt(date_str, shift_start, shift_end)
    minutes = int((truncate_seconds(punch_out) - end).total_seconds() // 60)
    if minutes < 30:
        return 0
    return minutes


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def parse_ymd(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def derived_for(emp: dict, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], date_str: str) -> dict:
    if status in ABSENCE:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "punch_in": None,
            "punch_out": None,
        }
    late = compute_late_minutes(punch_in, date_str, emp["shift_start"]) if punch_in else 0
    if punch_out is None or punch_in is None:
        return {
            "work_hours": None,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
            "punch_in": punch_in,
            "punch_out": None,
        }
    wh = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, date_str, emp["shift_start"], emp["shift_end"]),
        "half_day": compute_half_day(wh),
        "punch_in": punch_in,
        "punch_out": punch_out,
    }


def ping_mongo() -> bool:
    try:
        db.command("ping")
        return True
    except PyMongoError:
        return False


def require_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def same_instant(a: Optional[datetime], b: Optional[datetime]) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return dt_to_ms(a) == dt_to_ms(b)


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for key, val in (entry.get("changes") or {}).items():
        item = {}
        for side in ("from", "to"):
            raw = val.get(side) if isinstance(val, dict) else None
            if key in ("punch_in", "punch_out") and isinstance(raw, datetime):
                item[side] = dt_to_ms(raw)
            else:
                item[side] = raw
        changes[key] = item
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_employee(doc: dict) -> dict:
    out = {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }
    return out


def serialize_attendance(doc: dict) -> dict:
    history = doc.get("history") or []
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(h) for h in history],
    }


def bson_json(doc: Any) -> Any:
    return json.loads(bson_dumps(doc))


# --------------------------------------------------------------------------- #
# Aggregation builders (shared with /admin/explain)
# --------------------------------------------------------------------------- #
def _present_contrib(status_field: str, half_field: str, date_field: str, weekdays_only: bool) -> dict:
    present = {"$in": [status_field, list(PRESENCE)]}
    half = {
        "$cond": [
            {"$eq": [{"$ifNull": [half_field, False]}, True]},
            0.5,
            1,
        ]
    }
    if weekdays_only:
        dow = {
            "$dayOfWeek": {
                "$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d"}
            }
        }
        cond = {"$and": [present, {"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}
        inner = {"$cond": [cond, half, 0]}
    else:
        inner = {"$cond": [present, half, 0]}
    return {
        "$cond": [
            {"$in": [{"$type": date_field}, ["missing", "null"]]},
            0,
            inner,
        ]
    }


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    start, end = month_bounds(month)
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    month_start_date = datetime(y, m, 1)
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lte": ["$date", end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "working_days": {
                    "$size": {
                        "$filter": {
                            "input": {
                                "$map": {
                                    "input": {"$range": [0, last]},
                                    "as": "i",
                                    "in": {
                                        "$dateAdd": {
                                            "startDate": month_start_date,
                                            "unit": "day",
                                            "amount": "$$i",
                                        }
                                    },
                                }
                            },
                            "as": "d",
                            "cond": {
                                "$and": [
                                    {"$gte": [{"$dayOfWeek": "$$d"}, 2]},
                                    {"$lte": [{"$dayOfWeek": "$$d"}, 6]},
                                    {
                                        "$gte": [
                                            {
                                                "$dateToString": {
                                                    "format": "%Y-%m-%d",
                                                    "date": "$$d",
                                                }
                                            },
                                            "$joined_on",
                                        ]
                                    },
                                ]
                            },
                        }
                    }
                },
                "present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                _present_contrib("$$this.status", "$$this.half_day", "$$this.date", True),
                            ]
                        },
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.overtime_minutes", 0]},
                        }
                    }
                },
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    start, end = month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lte": ["$date", end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emps": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": _present_contrib("$logs.status", "$logs.half_day", "$logs.date", True)
                },
                "work_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_n": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            "$logs.late_minutes",
                            0,
                        ]
                    }
                },
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {"$match": {"$expr": {"$gt": [{"$size": "$emps"}, 0]}}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emps"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_n", 0]},
                        {"$round": [{"$divide": ["$work_sum", "$work_n"]}, 2]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list[dict]:
    start, end = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$set": {"emp_code": "$_id", "name": "$emp.name", "department": "$emp.department"}},
    ]
    if department:
        pipeline.append({"$match": {"department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def department_trend_pipeline(department: str, from_s: str, to_s: str) -> list[dict]:
    from_dt = parse_ymd(from_s)
    to_dt = parse_ymd(to_s)
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$replaceWith": {"date": from_dt}},
        {
            "$densify": {
                "field": "date",
                "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {
                    "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                    "dept": department,
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", "$$dept"]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}}},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {"$match": {"emp.department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": _present_contrib("$status", "$half_day", "$date", False)
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {
            "$set": {
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$stats.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$stats.late_count", 0]}, 0]},
                "dow": {"$dayOfWeek": "$date"},
            }
        },
        {
            "$set": {
                "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"range": [-6, 0], "unit": "day"},
                    }
                },
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": [{"$type": "$moving_avg_7d"}, "double"]},
                        {"$round": ["$moving_avg_7d", 4]},
                        {
                            "$cond": [
                                {"$eq": [{"$type": "$moving_avg_7d"}, "int"]},
                                {"$round": ["$moving_avg_7d", 4]},
                                {
                                    "$cond": [
                                        {"$eq": [{"$type": "$moving_avg_7d"}, "long"]},
                                        {"$round": ["$moving_avg_7d", 4]},
                                        {
                                            "$cond": [
                                                {"$eq": [{"$type": "$moving_avg_7d"}, "decimal"]},
                                                {"$round": ["$moving_avg_7d", 4]},
                                                None,
                                            ]
                                        },
                                    ]
                                },
                            ]
                        },
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


def attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: Optional[StrictInt] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def validate_month(month: str) -> str:
    if not MONTH_RE.match(month):
        raise HTTPException(422, "month must be YYYY-MM")
    return month


def validate_date_str(value: str, name: str) -> str:
    if not DATE_RE.match(value):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        parse_ymd(value)
    except ValueError:
        raise HTTPException(422, f"invalid {name}")
    return value


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    if not ping_mongo():
        raise HTTPException(503, "MongoDB unavailable")
    try:
        ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc(),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = require_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    derived = derived_for(emp, body.status, ts, None, date_str)
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": derived["punch_in"],
        "punch_out": None,
        "work_hours": None,
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = require_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = as_utc(rec["punch_in"])
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - pin).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at more than 24 hours after punch_in")
    derived = derived_for(emp, rec["status"], rec["punch_in"], ts, rec["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        validate_date_str(date_from, "date_from")
    if date_to:
        validate_date_str(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    q = attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _change_entry(from_v: Any, to_v: Any) -> Optional[dict]:
    if isinstance(from_v, datetime) and isinstance(to_v, datetime):
        if same_instant(from_v, to_v):
            return None
        return {"from": from_v, "to": to_v}
    if from_v == to_v:
        return None
    return {"from": from_v, "to": to_v}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = ...,
):
    validate_date_str(date, "date")
    emp = require_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "attendance record not found")

    new_status = body.status if body.status is not None else rec["status"]
    punch_in_provided = body.punch_in is not None
    punch_out_provided = body.punch_out is not None

    if new_status in ABSENCE:
        if punch_in_provided or punch_out_provided:
            raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        new_pin = None
        new_pout = None
    else:
        new_pin = ms_to_dt(body.punch_in) if punch_in_provided else rec.get("punch_in")
        new_pout = ms_to_dt(body.punch_out) if punch_out_provided else rec.get("punch_out")
        if new_pin is None:
            raise HTTPException(422, "presence status requires punch_in")
        new_pin = truncate_seconds(new_pin)
        if attendance_date(new_pin, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the attendance date")
        if new_pout is not None:
            new_pout = truncate_seconds(new_pout)
            if new_pout <= as_utc(new_pin):
                raise HTTPException(422, "punch_out must be after punch_in")
            if (as_utc(new_pout) - as_utc(new_pin)).total_seconds() > 24 * 3600:
                raise HTTPException(422, "punch_out more than 24 hours after punch_in")

    derived = derived_for(emp, new_status, new_pin, new_pout, date)
    new_doc = {
        "status": new_status,
        "punch_in": derived["punch_in"],
        "punch_out": derived["punch_out"],
        "work_hours": derived["work_hours"],
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
    }

    old_view = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day", False)),
    }
    changes: dict[str, dict] = {}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        entry = _change_entry(old_view[key], new_doc[key])
        if entry:
            changes[key] = entry
    if not changes:
        raise HTTPException(422, "no fields changed")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    old_history = rec.get("history") or []
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(old_history)]},
        },
        {"$set": new_doc, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "concurrent update")
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    validate_month(month)
    require_employee(emp_code)
    rows = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    if not rows:
        raise HTTPException(404, "employee not found")
    row = rows[0]
    working_days = int(row.get("working_days") or 0)
    present_days = round_half_up(row.get("present_days") or 0, 2)
    attendance_pct = None if working_days == 0 else round_half_up(present_days / working_days * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(row.get("leave_days") or 0),
        "late_count": int(row.get("late_count") or 0),
        "total_late_minutes": int(row.get("total_late_minutes") or 0),
        "total_overtime_minutes": int(row.get("total_overtime_minutes") or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    validate_month(month)
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(month, department)):
        avg = row.get("avg_work_hours")
        if avg is not None:
            avg = round_half_up(avg, 2)
        items.append(
            {
                "department": row["department"],
                "headcount": int(row["headcount"]),
                "present_days": round_half_up(row.get("present_days") or 0, 2),
                "avg_work_hours": avg,
                "late_count": int(row.get("late_count") or 0),
                "total_late_minutes": int(row.get("total_late_minutes") or 0),
                "leave_count": int(row.get("leave_count") or 0),
                "on_duty_count": int(row.get("on_duty_count") or 0),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    validate_month(month)
    items = []
    for row in db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)):
        items.append(
            {
                "rank": int(row["rank"]),
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": int(row["total_late_minutes"]),
                "late_count": int(row["late_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    validate_date_str(from_, "from")
    validate_date_str(to, "to")
    if to < from_:
        raise HTTPException(422, "to must be >= from")
    span = (parse_ymd(to) - parse_ymd(from_)).days + 1
    if span > 92:
        raise HTTPException(422, "range exceeds 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "unknown department")
    items = []
    for row in db.employees.aggregate(department_trend_pipeline(department, from_, to)):
        rate = row.get("attendance_rate")
        mavg = row.get("moving_avg_7d")
        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": int(row["headcount"]),
                "present_count": round_half_up(row.get("present_count") or 0, 2),
                "late_count": int(row.get("late_count") or 0),
                "attendance_rate": None if rate is None else round_half_up(rate, 4),
                "moving_avg_7d": None if mavg is None else round_half_up(mavg, 4),
            }
        )
    return {"department": department, "items": items}


def _explain_aggregate(collection_name: str, pipeline: list[dict]) -> dict:
    raw = db.command(
        {
            "explain": {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
    )
    return bson_json(raw)


def _explain_find(filter_q: dict, skip: int, limit: int) -> dict:
    raw = db.command(
        {
            "explain": {
                "find": "attendance_logs",
                "filter": filter_q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": limit,
            },
            "verbosity": "executionStats",
        }
    )
    return bson_json(raw)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from:
            validate_date_str(date_from, "date_from")
        if date_to:
            validate_date_str(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        q = attendance_list_query(emp_code, date_from, date_to, status)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": _explain_find(q, (page - 1) * page_size, page_size),
        }
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        validate_month(month)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": _explain_aggregate("employees", employee_monthly_pipeline(emp_code, month)),
        }
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        validate_month(month)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": _explain_aggregate("employees", department_summary_pipeline(month, department)),
        }
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        validate_month(month)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": _explain_aggregate(
                "attendance_logs", late_leaderboard_pipeline(month, limit, department)
            ),
        }
    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        validate_date_str(from_, "from")
        validate_date_str(to, "to")
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": _explain_aggregate(
                "employees", department_trend_pipeline(department, from_, to)
            ),
        }
    raise HTTPException(422, "unknown endpoint")
