"""
Time Tracking API Views.

Endpoints:
  GET    /api/time-entries/             - List time entries (with filters)
  POST   /api/time-entries/             - Create a new time entry
  PUT    /api/time-entries/<id>/        - Update a time entry
  DELETE /api/time-entries/<id>/        - Delete a time entry
  GET    /api/time-reports/             - Generate time reports
  POST   /api/time-entries/start-timer/ - Start a timer for a task
  POST   /api/time-entries/stop-timer/  - Stop a running timer
"""
from datetime import datetime, date, timezone
from bson import ObjectId
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .db import get_collection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_entry(entry):
    """Convert a MongoDB document to a JSON-serializable dict."""
    if entry is None:
        return None
    entry["id"] = str(entry.pop("_id"))
    # Convert datetime objects to ISO strings
    for key in ("created_at", "updated_at", "timer_start"):
        if key in entry and isinstance(entry[key], datetime):
            entry[key] = entry[key].isoformat()
    # Convert date objects
    if "date" in entry and isinstance(entry["date"], (datetime, date)):
        entry["date"] = entry["date"].isoformat() if isinstance(entry["date"], date) else entry["date"][:10]
    return entry


def _parse_date(date_str):
    """Parse a YYYY-MM-DD string to a date object."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _build_filter(params):
    """Build a MongoDB filter dict from query parameters."""
    query = {}
    if params.get("user_id"):
        query["user_id"] = params["user_id"]
    if params.get("task_id"):
        query["task_id"] = params["task_id"]
    if params.get("project_id"):
        query["project_id"] = params["project_id"]
    if params.get("billable") is not None and params.get("billable") != "":
        query["billable"] = params["billable"].lower() in ("true", "1", "yes")

    # Date range filters
    date_from = _parse_date(params.get("date_from"))
    date_to = _parse_date(params.get("date_to"))
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()

    return query


# ---------------------------------------------------------------------------
# Time Entry CRUD
# ---------------------------------------------------------------------------

class TimeEntryListCreateView(APIView):
    """
    GET  /api/time-entries/  - List entries with optional filters.
    POST /api/time-entries/  - Create a new manual time entry.

    Query params for GET:
        user_id, task_id, project_id, billable, date_from, date_to
    """

    def get(self, request):
        collection = get_collection("time_entries")
        query = _build_filter(request.query_params)
        entries = list(collection.find(query).sort("created_at", -1))
        return Response([_serialize_entry(e) for e in entries])

    def post(self, request):
        data = request.data

        # Validate required fields
        required = ["task_id", "user_id", "hours", "date"]
        missing = [f for f in required if not data.get(f)]
        if missing:
            return Response(
                {"error": f"Missing required fields: {', '.join(missing)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate hours
        try:
            hours = float(data["hours"])
            if hours <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return Response(
                {"error": "hours must be a positive number"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate date format
        parsed_date = _parse_date(data["date"])
        if parsed_date is None:
            return Response(
                {"error": "date must be in YYYY-MM-DD format"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entry = {
            "task_id": data["task_id"],
            "user_id": data["user_id"],
            "project_id": data.get("project_id", ""),
            "hours": hours,
            "date": data["date"],
            "description": data.get("description", ""),
            "billable": bool(data.get("billable", False)),
            "timer_entry": False,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        collection = get_collection("time_entries")
        result = collection.insert_one(entry)
        entry["_id"] = result.inserted_id
        return Response(_serialize_entry(entry), status=status.HTTP_201_CREATED)


class TimeEntryDetailView(APIView):
    """
    PUT    /api/time-entries/<id>/  - Update a time entry.
    DELETE /api/time-entries/<id>/  - Delete a time entry.
    """

    def put(self, request, entry_id):
        collection = get_collection("time_entries")

        # Check existence
        try:
            obj_id = ObjectId(entry_id)
        except Exception:
            return Response({"error": "Invalid entry ID"}, status=status.HTTP_400_BAD_REQUEST)

        existing = collection.find_one({"_id": obj_id})
        if not existing:
            return Response({"error": "Time entry not found"}, status=status.HTTP_404_NOT_FOUND)

        # Build update dict — only update provided fields
        data = request.data
        update_fields = {}

        if "hours" in data:
            try:
                hours = float(data["hours"])
                if hours <= 0:
                    raise ValueError
                update_fields["hours"] = hours
            except (ValueError, TypeError):
                return Response({"error": "hours must be a positive number"}, status=status.HTTP_400_BAD_REQUEST)

        if "date" in data:
            parsed = _parse_date(data["date"])
            if parsed is None:
                return Response({"error": "date must be YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)
            update_fields["date"] = data["date"]

        for field in ("task_id", "user_id", "project_id", "description"):
            if field in data:
                update_fields[field] = data[field]

        if "billable" in data:
            update_fields["billable"] = bool(data["billable"])

        update_fields["updated_at"] = datetime.now(timezone.utc)

        collection.update_one({"_id": obj_id}, {"$set": update_fields})
        updated = collection.find_one({"_id": obj_id})
        return Response(_serialize_entry(updated))

    def delete(self, request, entry_id):
        collection = get_collection("time_entries")

        try:
            obj_id = ObjectId(entry_id)
        except Exception:
            return Response({"error": "Invalid entry ID"}, status=status.HTTP_400_BAD_REQUEST)

        result = collection.delete_one({"_id": obj_id})
        if result.deleted_count == 0:
            return Response({"error": "Time entry not found"}, status=status.HTTP_404_NOT_FOUND)

        return Response({"message": "Time entry deleted"}, status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Timer (Start / Stop)
# ---------------------------------------------------------------------------

class StartTimerView(APIView):
    """
    POST /api/time-entries/start-timer/

    Body: { "task_id": "...", "user_id": "...", "project_id": "..." (optional) }
    Starts a live timer. Saves a record with timer_running=True.
    """

    def post(self, request):
        data = request.data
        if not data.get("task_id") or not data.get("user_id"):
            return Response(
                {"error": "task_id and user_id are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        collection = get_collection("time_entries")

        # Check if user already has a running timer
        running = collection.find_one({
            "user_id": data["user_id"],
            "timer_running": True,
        })
        if running:
            return Response(
                {
                    "error": "You already have a running timer",
                    "running_entry": _serialize_entry(running),
                },
                status=status.HTTP_409_CONFLICT,
            )

        now = datetime.now(timezone.utc)
        entry = {
            "task_id": data["task_id"],
            "user_id": data["user_id"],
            "project_id": data.get("project_id", ""),
            "hours": 0,
            "date": now.strftime("%Y-%m-%d"),
            "description": data.get("description", ""),
            "billable": bool(data.get("billable", False)),
            "timer_running": True,
            "timer_start": now,
            "timer_entry": True,
            "created_at": now,
            "updated_at": now,
        }

        result = collection.insert_one(entry)
        entry["_id"] = result.inserted_id
        return Response(_serialize_entry(entry), status=status.HTTP_201_CREATED)


class StopTimerView(APIView):
    """
    POST /api/time-entries/stop-timer/

    Body: { "task_id": "...", "user_id": "...", "description": "..." (optional) }
    Stops the running timer and calculates hours automatically.
    """

    def post(self, request):
        data = request.data
        if not data.get("task_id") or not data.get("user_id"):
            return Response(
                {"error": "task_id and user_id are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        collection = get_collection("time_entries")

        running = collection.find_one({
            "task_id": data["task_id"],
            "user_id": data["user_id"],
            "timer_running": True,
        })
        if not running:
            return Response(
                {"error": "No running timer found for this task/user"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Calculate elapsed hours
        now = datetime.now(timezone.utc)
        timer_start = running["timer_start"]
        # MongoDB may return naive datetime — make it aware
        if timer_start.tzinfo is None:
            timer_start = timer_start.replace(tzinfo=timezone.utc)
        elapsed = (now - timer_start).total_seconds() / 3600.0
        elapsed = round(elapsed, 2)

        update_fields = {
            "timer_running": False,
            "hours": elapsed,
            "updated_at": now,
        }
        if data.get("description"):
            update_fields["description"] = data["description"]
        if "billable" in data:
            update_fields["billable"] = bool(data["billable"])

        collection.update_one({"_id": running["_id"]}, {"$set": update_fields})
        updated = collection.find_one({"_id": running["_id"]})
        return Response(_serialize_entry(updated))


# ---------------------------------------------------------------------------
# Time Reports
# ---------------------------------------------------------------------------

class TimeReportView(APIView):
    """
    GET /api/time-reports/

    Query params:
        group_by   - "user" | "project" | "billable" | "weekly" | "monthly"
        user_id    - Filter by user
        project_id - Filter by project
        date_from  - Start date (YYYY-MM-DD)
        date_to    - End date (YYYY-MM-DD)
    """

    def get(self, request):
        collection = get_collection("time_entries")
        params = request.query_params
        group_by = params.get("group_by", "user")

        # Base filter — exclude running timers from reports
        base_filter = {"timer_running": {"$ne": True}}
        if params.get("user_id"):
            base_filter["user_id"] = params["user_id"]
        if params.get("project_id"):
            base_filter["project_id"] = params["project_id"]

        date_from = _parse_date(params.get("date_from"))
        date_to = _parse_date(params.get("date_to"))
        if date_from or date_to:
            base_filter["date"] = {}
            if date_from:
                base_filter["date"]["$gte"] = date_from.isoformat()
            if date_to:
                base_filter["date"]["$lte"] = date_to.isoformat()

        entries = list(collection.find(base_filter))

        if group_by == "user":
            report = self._group_by_field(entries, "user_id")
        elif group_by == "project":
            report = self._group_by_field(entries, "project_id")
        elif group_by == "billable":
            report = self._group_by_billable(entries)
        elif group_by == "weekly":
            report = self._group_by_period(entries, "weekly")
        elif group_by == "monthly":
            report = self._group_by_period(entries, "monthly")
        else:
            report = self._group_by_field(entries, "user_id")

        # Summary totals
        total_hours = sum(e.get("hours", 0) for e in entries)
        billable_hours = sum(e.get("hours", 0) for e in entries if e.get("billable"))
        non_billable_hours = total_hours - billable_hours

        return Response({
            "group_by": group_by,
            "total_entries": len(entries),
            "total_hours": round(total_hours, 2),
            "billable_hours": round(billable_hours, 2),
            "non_billable_hours": round(non_billable_hours, 2),
            "breakdown": report,
        })

    def _group_by_field(self, entries, field):
        groups = {}
        for e in entries:
            key = e.get(field, "unknown")
            if key not in groups:
                groups[key] = {"total_hours": 0, "billable_hours": 0, "entry_count": 0}
            groups[key]["total_hours"] += e.get("hours", 0)
            if e.get("billable"):
                groups[key]["billable_hours"] += e.get("hours", 0)
            groups[key]["entry_count"] += 1

        # Round values
        for k in groups:
            groups[k]["total_hours"] = round(groups[k]["total_hours"], 2)
            groups[k]["billable_hours"] = round(groups[k]["billable_hours"], 2)
        return groups

    def _group_by_billable(self, entries):
        result = {
            "billable": {"total_hours": 0, "entry_count": 0},
            "non_billable": {"total_hours": 0, "entry_count": 0},
        }
        for e in entries:
            key = "billable" if e.get("billable") else "non_billable"
            result[key]["total_hours"] += e.get("hours", 0)
            result[key]["entry_count"] += 1

        for k in result:
            result[k]["total_hours"] = round(result[k]["total_hours"], 2)
        return result

    def _group_by_period(self, entries, period):
        groups = {}
        for e in entries:
            d = _parse_date(e.get("date", ""))
            if d is None:
                continue
            if period == "weekly":
                # ISO week: "2026-W09"
                key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
            else:
                # Monthly: "2026-02"
                key = d.strftime("%Y-%m")

            if key not in groups:
                groups[key] = {"total_hours": 0, "billable_hours": 0, "entry_count": 0}
            groups[key]["total_hours"] += e.get("hours", 0)
            if e.get("billable"):
                groups[key]["billable_hours"] += e.get("hours", 0)
            groups[key]["entry_count"] += 1

        for k in groups:
            groups[k]["total_hours"] = round(groups[k]["total_hours"], 2)
            groups[k]["billable_hours"] = round(groups[k]["billable_hours"], 2)
        return dict(sorted(groups.items()))
