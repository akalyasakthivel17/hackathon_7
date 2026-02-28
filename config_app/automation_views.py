"""
Smart Automation API Views.

Endpoints:
  GET    /api/automation-rules/              - List all automation rules
  POST   /api/automation-rules/              - Create a new rule
  PUT    /api/automation-rules/<id>/         - Update a rule
  DELETE /api/automation-rules/<id>/         - Delete a rule
  GET    /api/automation-rules/<id>/logs/    - Get execution logs for a rule
  POST   /api/automation-rules/test/<id>/    - Test-fire a rule manually
  POST   /api/automation/trigger/            - Trigger engine (called by Tasks API)

Automation Engine:
  - check_and_execute_rules() is the core engine function
  - Can be imported and called from the Tasks API when task events occur
  - Also exposed as an API endpoint for manual/external triggering
"""
import os
import logging
from datetime import datetime, timezone
from bson import ObjectId
import requests
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .db import get_collection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_TRIGGER_TYPES = [
    "status_changed",
    "task_assigned",
    "task_created",
    "due_date_approaching",
    "task_overdue",
    "priority_changed",
]

VALID_ACTION_TYPES = [
    "notify",
    "assign",
    "change_status",
    "add_comment",
    "teams_message",
    "create_subtask",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_rule(rule):
    """Convert a MongoDB document to a JSON-serializable dict."""
    if rule is None:
        return None
    rule["id"] = str(rule.pop("_id"))
    for key in ("created_at", "updated_at"):
        if key in rule and isinstance(rule[key], datetime):
            rule[key] = rule[key].isoformat()
    return rule


def _serialize_log(log):
    """Convert an automation log document to JSON-serializable dict."""
    if log is None:
        return None
    log["id"] = str(log.pop("_id"))
    if "rule_id" in log and isinstance(log["rule_id"], ObjectId):
        log["rule_id"] = str(log["rule_id"])
    for key in ("executed_at",):
        if key in log and isinstance(log[key], datetime):
            log[key] = log[key].isoformat()
    return log


def _validate_rule_data(data, partial=False):
    """Validate rule data. Returns (cleaned_data, error_message)."""
    errors = []

    if not partial:
        # Full validation for creation
        if not data.get("name"):
            errors.append("name is required")
        if not data.get("project_id"):
            errors.append("project_id is required")
        if not data.get("trigger_type"):
            errors.append("trigger_type is required")
        if not data.get("action_type"):
            errors.append("action_type is required")

    if data.get("trigger_type") and data["trigger_type"] not in VALID_TRIGGER_TYPES:
        errors.append(f"trigger_type must be one of: {', '.join(VALID_TRIGGER_TYPES)}")

    if data.get("action_type") and data["action_type"] not in VALID_ACTION_TYPES:
        errors.append(f"action_type must be one of: {', '.join(VALID_ACTION_TYPES)}")

    if errors:
        return None, "; ".join(errors)

    return data, None


# ---------------------------------------------------------------------------
# Teams Webhook Helper
# ---------------------------------------------------------------------------

def send_teams_message(message, title=None):
    """
    Send a message to Microsoft Teams via incoming webhook.
    Returns (success: bool, error_message: str or None).
    """
    webhook_url = os.getenv("TEAMS_WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning("TEAMS_WEBHOOK_URL not configured in .env")
        return False, "Teams webhook URL not configured"

    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": title or "🤖 Automation Alert",
                            "weight": "Bolder",
                            "size": "Medium",
                        },
                        {
                            "type": "TextBlock",
                            "text": message,
                            "wrap": True,
                        },
                    ],
                },
            }
        ],
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 202):
            return True, None
        else:
            error = f"Teams webhook returned {resp.status_code}: {resp.text[:200]}"
            logger.error(error)
            return False, error
    except requests.exceptions.Timeout:
        logger.error("Teams webhook request timed out")
        return False, "Teams webhook request timed out"
    except requests.exceptions.RequestException as e:
        logger.error(f"Teams webhook error: {str(e)}")
        return False, str(e)


# ---------------------------------------------------------------------------
# Automation Engine — Core Logic
# ---------------------------------------------------------------------------

def check_and_execute_rules(trigger_type, event_data):
    """
    Core automation engine function.

    Called when a task event occurs. Checks all enabled rules matching
    the trigger_type, evaluates trigger conditions, and executes actions.

    Args:
        trigger_type (str): One of VALID_TRIGGER_TYPES
        event_data (dict): Event context, e.g.:
            {
                "task_id": "...",
                "project_id": "...",
                "task_title": "...",
                "old_status": "To Do",
                "new_status": "In Review",
                "assignee_id": "...",
                "assignee_name": "...",
                "priority": "High",
                "user_id": "...",       # who triggered the event
                "user_name": "...",
            }

    Returns:
        list: Results of all executed rules
    """
    if trigger_type not in VALID_TRIGGER_TYPES:
        logger.warning(f"Unknown trigger_type: {trigger_type}")
        return []

    rules_collection = get_collection("automation_rules")
    logs_collection = get_collection("automation_logs")

    # Find all enabled rules matching this trigger type
    try:
        matching_rules = list(rules_collection.find({
            "trigger_type": trigger_type,
            "enabled": True,
        }))
    except Exception as e:
        logger.error(f"Error fetching automation rules: {str(e)}")
        return []

    results = []

    for rule in matching_rules:
        # Check if trigger_value conditions match
        if not _evaluate_trigger(rule, event_data):
            continue

        # Execute the action
        success, action_result = _execute_action(rule, event_data)

        # Log the execution
        log_entry = {
            "rule_id": rule["_id"],
            "rule_name": rule.get("name", ""),
            "trigger_type": trigger_type,
            "action_type": rule.get("action_type", ""),
            "event_data": {k: str(v) for k, v in event_data.items()},  # stringify for storage
            "success": success,
            "result": action_result,
            "executed_at": datetime.now(timezone.utc),
        }

        try:
            logs_collection.insert_one(log_entry)
        except Exception as e:
            logger.error(f"Error saving automation log: {str(e)}")

        results.append({
            "rule_id": str(rule["_id"]),
            "rule_name": rule.get("name", ""),
            "action_type": rule.get("action_type", ""),
            "success": success,
            "result": action_result,
        })

    return results


def _evaluate_trigger(rule, event_data):
    """
    Check if the event_data matches the rule's trigger_value conditions.
    Returns True if the rule should fire, False otherwise.
    """
    trigger_value = rule.get("trigger_value", {})
    if not trigger_value:
        # No specific conditions — always match
        return True

    trigger_type = rule.get("trigger_type", "")

    try:
        if trigger_type == "status_changed":
            # Check if status matches the expected value
            expected_status = trigger_value.get("status", "")
            if expected_status and event_data.get("new_status") != expected_status:
                return False

        elif trigger_type == "task_assigned":
            # Check if assigned to specific user (optional)
            expected_user = trigger_value.get("assignee_id", "")
            if expected_user and event_data.get("assignee_id") != expected_user:
                return False

        elif trigger_type == "task_created":
            # Check optional conditions like priority
            expected_priority = trigger_value.get("priority", "")
            if expected_priority and event_data.get("priority") != expected_priority:
                return False

        elif trigger_type == "priority_changed":
            expected_priority = trigger_value.get("priority", "")
            if expected_priority and event_data.get("priority") != expected_priority:
                return False

        elif trigger_type in ("due_date_approaching", "task_overdue"):
            # These are time-based — always match when triggered by cron
            pass

    except Exception as e:
        logger.error(f"Error evaluating trigger for rule {rule.get('name')}: {str(e)}")
        return False

    # Check project_id scope if specified in the rule
    rule_project = rule.get("project_id", "")
    if rule_project and event_data.get("project_id") and rule_project != event_data["project_id"]:
        return False

    return True


def _execute_action(rule, event_data):
    """
    Execute the rule's action.
    Returns (success: bool, result_message: str).
    """
    action_type = rule.get("action_type", "")
    action_value = rule.get("action_value", {})
    task_title = event_data.get("task_title", "Unknown Task")

    try:
        if action_type == "notify":
            # Create an in-app notification in the notifications collection
            notification = {
                "user_id": action_value.get("user_id", event_data.get("user_id", "")),
                "title": action_value.get("title", f"Automation: {rule.get('name', '')}"),
                "message": action_value.get(
                    "message",
                    f"Task '{task_title}' triggered rule '{rule.get('name', '')}'",
                ),
                "type": "automation",
                "read": False,
                "task_id": event_data.get("task_id", ""),
                "project_id": event_data.get("project_id", ""),
                "created_at": datetime.now(timezone.utc),
            }
            get_collection("notifications").insert_one(notification)
            return True, f"Notification sent to user {notification['user_id']}"

        elif action_type == "assign":
            # Store assignment change request — Tasks API should pick this up
            assignment = {
                "task_id": event_data.get("task_id", ""),
                "new_assignee_id": action_value.get("user_id", ""),
                "triggered_by_rule": str(rule["_id"]),
                "status": "pending",
                "created_at": datetime.now(timezone.utc),
            }
            get_collection("automation_actions").insert_one(assignment)
            return True, f"Task assignment queued for user {action_value.get('user_id', '')}"

        elif action_type == "change_status":
            # Store status change request — Tasks API should pick this up
            status_change = {
                "task_id": event_data.get("task_id", ""),
                "new_status": action_value.get("status", ""),
                "triggered_by_rule": str(rule["_id"]),
                "status": "pending",
                "created_at": datetime.now(timezone.utc),
            }
            get_collection("automation_actions").insert_one(status_change)
            return True, f"Status change to '{action_value.get('status', '')}' queued"

        elif action_type == "add_comment":
            # Add a comment to the task
            comment = {
                "task_id": event_data.get("task_id", ""),
                "user_id": "system",
                "content": action_value.get(
                    "comment",
                    f"[Auto] Rule '{rule.get('name', '')}' triggered on this task.",
                ),
                "type": "automation",
                "created_at": datetime.now(timezone.utc),
            }
            get_collection("comments").insert_one(comment)
            return True, "Auto-comment added to task"

        elif action_type == "teams_message":
            # Send message to Teams via webhook
            message = action_value.get(
                "message",
                f"📋 *{task_title}* — Rule '{rule.get('name', '')}' triggered.\n"
                f"Trigger: {rule.get('trigger_type', '')}\n"
                f"By: {event_data.get('user_name', 'Unknown')}",
            )
            title = action_value.get("title", f"🤖 TaskFlow Automation")
            success, error = send_teams_message(message, title=title)
            if success:
                return True, "Teams message sent successfully"
            else:
                return False, f"Teams message failed: {error}"

        elif action_type == "create_subtask":
            # Store subtask creation request — Tasks API should pick this up
            subtask = {
                "parent_task_id": event_data.get("task_id", ""),
                "title": action_value.get("title", f"Follow-up: {task_title}"),
                "description": action_value.get("description", ""),
                "assignee_id": action_value.get("assignee_id", ""),
                "priority": action_value.get("priority", "Medium"),
                "triggered_by_rule": str(rule["_id"]),
                "status": "pending",
                "created_at": datetime.now(timezone.utc),
            }
            get_collection("automation_actions").insert_one(subtask)
            return True, "Subtask creation queued"

        else:
            return False, f"Unknown action_type: {action_type}"

    except Exception as e:
        logger.error(f"Error executing action {action_type} for rule {rule.get('name')}: {str(e)}")
        return False, f"Execution error: {str(e)}"


# ---------------------------------------------------------------------------
# Rule CRUD API Views
# ---------------------------------------------------------------------------

class AutomationRuleListCreateView(APIView):
    """
    GET  /api/automation-rules/  - List all automation rules.
    POST /api/automation-rules/  - Create a new automation rule.

    GET query params:
        project_id  - Filter by project
        trigger_type - Filter by trigger type
        enabled     - Filter by enabled status (true/false)
    """

    def get(self, request):
        try:
            collection = get_collection("automation_rules")
            query = {}

            if request.query_params.get("project_id"):
                query["project_id"] = request.query_params["project_id"]
            if request.query_params.get("trigger_type"):
                query["trigger_type"] = request.query_params["trigger_type"]
            if request.query_params.get("enabled") is not None and request.query_params.get("enabled") != "":
                query["enabled"] = request.query_params["enabled"].lower() in ("true", "1", "yes")

            rules = list(collection.find(query).sort("created_at", -1))
            return Response([_serialize_rule(r) for r in rules])

        except Exception as e:
            logger.error(f"Error listing automation rules: {str(e)}")
            return Response(
                {"error": f"Failed to list rules: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def post(self, request):
        try:
            data = request.data
            cleaned, error = _validate_rule_data(data)
            if error:
                return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)

            now = datetime.now(timezone.utc)
            rule = {
                "name": data["name"],
                "project_id": data["project_id"],
                "description": data.get("description", ""),
                "trigger_type": data["trigger_type"],
                "trigger_value": data.get("trigger_value", {}),
                "action_type": data["action_type"],
                "action_value": data.get("action_value", {}),
                "enabled": data.get("enabled", True),
                "created_by": data.get("created_by", ""),
                "created_at": now,
                "updated_at": now,
            }

            collection = get_collection("automation_rules")
            result = collection.insert_one(rule)
            rule["_id"] = result.inserted_id
            return Response(_serialize_rule(rule), status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Error creating automation rule: {str(e)}")
            return Response(
                {"error": f"Failed to create rule: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class AutomationRuleDetailView(APIView):
    """
    GET    /api/automation-rules/<id>/  - Get a single rule.
    PUT    /api/automation-rules/<id>/  - Update a rule.
    DELETE /api/automation-rules/<id>/  - Delete a rule.
    """

    def get(self, request, rule_id):
        try:
            obj_id = ObjectId(rule_id)
        except Exception:
            return Response({"error": "Invalid rule ID"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            collection = get_collection("automation_rules")
            rule = collection.find_one({"_id": obj_id})
            if not rule:
                return Response({"error": "Rule not found"}, status=status.HTTP_404_NOT_FOUND)
            return Response(_serialize_rule(rule))
        except Exception as e:
            logger.error(f"Error fetching rule {rule_id}: {str(e)}")
            return Response(
                {"error": f"Failed to fetch rule: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def put(self, request, rule_id):
        try:
            obj_id = ObjectId(rule_id)
        except Exception:
            return Response({"error": "Invalid rule ID"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            collection = get_collection("automation_rules")
            existing = collection.find_one({"_id": obj_id})
            if not existing:
                return Response({"error": "Rule not found"}, status=status.HTTP_404_NOT_FOUND)

            data = request.data
            cleaned, error = _validate_rule_data(data, partial=True)
            if error:
                return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)

            # Only update provided fields
            update_fields = {}
            for field in ("name", "description", "project_id", "trigger_type",
                          "trigger_value", "action_type", "action_value", "created_by"):
                if field in data:
                    update_fields[field] = data[field]

            if "enabled" in data:
                update_fields["enabled"] = bool(data["enabled"])

            update_fields["updated_at"] = datetime.now(timezone.utc)

            collection.update_one({"_id": obj_id}, {"$set": update_fields})
            updated = collection.find_one({"_id": obj_id})
            return Response(_serialize_rule(updated))

        except Exception as e:
            logger.error(f"Error updating rule {rule_id}: {str(e)}")
            return Response(
                {"error": f"Failed to update rule: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def delete(self, request, rule_id):
        try:
            obj_id = ObjectId(rule_id)
        except Exception:
            return Response({"error": "Invalid rule ID"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            collection = get_collection("automation_rules")
            result = collection.delete_one({"_id": obj_id})
            if result.deleted_count == 0:
                return Response({"error": "Rule not found"}, status=status.HTTP_404_NOT_FOUND)

            # Also clean up logs for this rule
            get_collection("automation_logs").delete_many({"rule_id": obj_id})

            return Response({"message": "Rule and its logs deleted"}, status=status.HTTP_204_NO_CONTENT)

        except Exception as e:
            logger.error(f"Error deleting rule {rule_id}: {str(e)}")
            return Response(
                {"error": f"Failed to delete rule: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ---------------------------------------------------------------------------
# Rule Execution Logs
# ---------------------------------------------------------------------------

class AutomationRuleLogsView(APIView):
    """
    GET /api/automation-rules/<id>/logs/  - Get execution logs for a rule.

    Query params:
        limit  - Number of logs to return (default: 50)
        success - Filter by success (true/false)
    """

    def get(self, request, rule_id):
        try:
            obj_id = ObjectId(rule_id)
        except Exception:
            return Response({"error": "Invalid rule ID"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Verify rule exists
            rule = get_collection("automation_rules").find_one({"_id": obj_id})
            if not rule:
                return Response({"error": "Rule not found"}, status=status.HTTP_404_NOT_FOUND)

            query = {"rule_id": obj_id}
            if request.query_params.get("success") is not None and request.query_params.get("success") != "":
                query["success"] = request.query_params["success"].lower() in ("true", "1")

            limit = min(int(request.query_params.get("limit", 50)), 200)

            logs_collection = get_collection("automation_logs")
            logs = list(logs_collection.find(query).sort("executed_at", -1).limit(limit))

            return Response({
                "rule_id": rule_id,
                "rule_name": rule.get("name", ""),
                "total_logs": logs_collection.count_documents({"rule_id": obj_id}),
                "logs": [_serialize_log(l) for l in logs],
            })

        except Exception as e:
            logger.error(f"Error fetching logs for rule {rule_id}: {str(e)}")
            return Response(
                {"error": f"Failed to fetch logs: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ---------------------------------------------------------------------------
# Trigger Endpoint (called by Tasks API or external events)
# ---------------------------------------------------------------------------

class AutomationTriggerView(APIView):
    """
    POST /api/automation/trigger/

    Called internally by the Tasks API when a task event occurs,
    or can be called manually for testing.

    Body:
    {
        "trigger_type": "status_changed",
        "event_data": {
            "task_id": "...",
            "project_id": "...",
            "task_title": "Fix Login Bug",
            "old_status": "To Do",
            "new_status": "In Review",
            "user_id": "...",
            "user_name": "John"
        }
    }
    """

    def post(self, request):
        data = request.data

        trigger_type = data.get("trigger_type", "")
        if not trigger_type:
            return Response(
                {"error": "trigger_type is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if trigger_type not in VALID_TRIGGER_TYPES:
            return Response(
                {"error": f"trigger_type must be one of: {', '.join(VALID_TRIGGER_TYPES)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        event_data = data.get("event_data", {})
        if not event_data:
            return Response(
                {"error": "event_data is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            results = check_and_execute_rules(trigger_type, event_data)
            return Response({
                "trigger_type": trigger_type,
                "rules_matched": len(results),
                "results": results,
            })
        except Exception as e:
            logger.error(f"Error in automation trigger: {str(e)}")
            return Response(
                {"error": f"Automation engine error: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ---------------------------------------------------------------------------
# Test-Fire a Rule (for demo / debugging)
# ---------------------------------------------------------------------------

class AutomationTestRuleView(APIView):
    """
    POST /api/automation-rules/test/<id>/

    Test-fires a specific rule with sample event data.
    Useful for demo and debugging.

    Body (optional — uses defaults if not provided):
    {
        "event_data": {
            "task_id": "test_task",
            "task_title": "Test Task",
            "project_id": "test_project",
            ...
        }
    }
    """

    def post(self, request, rule_id):
        try:
            obj_id = ObjectId(rule_id)
        except Exception:
            return Response({"error": "Invalid rule ID"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            rule = get_collection("automation_rules").find_one({"_id": obj_id})
            if not rule:
                return Response({"error": "Rule not found"}, status=status.HTTP_404_NOT_FOUND)

            # Use provided event_data or build defaults
            event_data = request.data.get("event_data", {})
            if not event_data:
                # Build sample event data based on trigger type
                event_data = {
                    "task_id": "test_task_001",
                    "project_id": rule.get("project_id", "test_project"),
                    "task_title": "Test Task (Automation Test)",
                    "user_id": "test_user",
                    "user_name": "Test User",
                }

                trigger_value = rule.get("trigger_value", {})
                if rule["trigger_type"] == "status_changed":
                    event_data["new_status"] = trigger_value.get("status", "In Review")
                    event_data["old_status"] = "To Do"
                elif rule["trigger_type"] == "task_assigned":
                    event_data["assignee_id"] = trigger_value.get("assignee_id", "test_user")
                    event_data["assignee_name"] = "Test Assignee"
                elif rule["trigger_type"] == "task_created":
                    event_data["priority"] = trigger_value.get("priority", "High")
                elif rule["trigger_type"] == "priority_changed":
                    event_data["priority"] = trigger_value.get("priority", "Urgent")

            # Force-execute regardless of enabled status
            success, result = _execute_action(rule, event_data)

            # Log it as a test execution
            log_entry = {
                "rule_id": rule["_id"],
                "rule_name": rule.get("name", ""),
                "trigger_type": rule.get("trigger_type", ""),
                "action_type": rule.get("action_type", ""),
                "event_data": {k: str(v) for k, v in event_data.items()},
                "success": success,
                "result": result,
                "test_execution": True,
                "executed_at": datetime.now(timezone.utc),
            }
            get_collection("automation_logs").insert_one(log_entry)

            return Response({
                "rule_id": rule_id,
                "rule_name": rule.get("name", ""),
                "test_execution": True,
                "success": success,
                "result": result,
                "event_data_used": event_data,
            })

        except Exception as e:
            logger.error(f"Error test-firing rule {rule_id}: {str(e)}")
            return Response(
                {"error": f"Test execution failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
