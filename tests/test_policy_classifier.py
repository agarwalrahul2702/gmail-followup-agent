from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.classifier import Classification, classify
from app.followup_policy import (
    calculate_followup1_due,
    calculate_followup2_due,
    due_at,
    move_weekend_to_monday,
)
from app.gmail_service import Message, external_reply
from app.rules import Rules


def msg(
    body="I attached my resume for the backend engineer role.", subject="Backend role", **kwargs
):
    return Message(
        "original",
        "thread",
        datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
        {
            "from": "owner@example.com",
            "to": "Recruiter <hr@example.org>",
            "subject": subject,
            "message-id": "<original@example.com>",
            **kwargs,
        },
        body,
        [],
        True,
    )


@pytest.mark.parametrize("day,expected", [(14, 14), (19, 21), (20, 21)])
def test_weekends(day, expected):
    assert move_weekend_to_monday(datetime(2026, 9, day)).day == expected


def test_original_dates():
    original = datetime(2026, 9, 14, 8, tzinfo=timezone.utc)
    assert calculate_followup1_due(original, "UTC").day == 17
    assert calculate_followup2_due(original, "UTC").day == 21
    assert due_at(original, "Asia/Kolkata", 3).hour == 13


@pytest.mark.parametrize(
    "body,category,status",
    [
        ("I attached my resume for the backend engineer role.", "RESUME_OUTREACH", "ACTIVE"),
        ("Could you refer me for the software engineer opening?", "REFERRAL_REQUEST", "ACTIVE"),
        ("My interview availability is Monday", "EXCLUDED", "EXCLUDED"),
        ("Thank you for scheduling the interview", "EXCLUDED", "EXCLUDED"),
        ("Thanks for considering me", "EXCLUDED", "EXCLUDED"),
        ("Could you provide rejection feedback?", "EXCLUDED", "EXCLUDED"),
        ("Is there an engineering opportunity?", "JOB_HELP_REQUEST", "REVIEW_REQUIRED"),
    ],
)
def test_classification(body, category, status):
    value = classify(msg(body), Rules(), [0])
    assert (value.category, value.status) == (category, status)


def test_reply_and_alias():
    original = msg()
    reply = msg("Response")
    reply.id, reply.sent = "reply", False
    reply.headers = {"from": "hr@example.org"}
    assert external_reply([original, reply], original, "owner@example.com")
    reply.sent = True
    assert not external_reply([original, reply], original, "owner@example.com")


def test_low_confidence():
    assert (
        Classification(
            eligible=True, category="REFERRAL_REQUEST", confidence=0.79, reason="Unsure"
        ).status
        == "REVIEW_REQUIRED"
    )
    assert (
        Classification(
            eligible=True, category="EXCLUDED", confidence=0.95, reason="Excluded"
        ).status
        == "EXCLUDED"
    )


def test_hybrid_budget_and_failure(monkeypatch):
    import app.classifier as module

    monkeypatch.setattr(
        module,
        "settings",
        lambda: SimpleNamespace(
            classifier_mode="hybrid", openai_api_key="fake", openai_model="test"
        ),
    )
    client = Mock()
    client.responses.parse.side_effect = RuntimeError("offline")
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "OpenAI", factory)
    budget = [1]
    assert (
        classify(msg("Is there an engineering opportunity?"), Rules(), budget).status
        == "REVIEW_REQUIRED"
    )
    assert budget == [0]
    classify(msg("Is there an engineering opportunity?"), Rules(), budget)
    assert factory.call_count == 1


def test_custom_rules():
    assert classify(msg(), Rules(exclude_phrases=["backend"]), [0]).status == "EXCLUDED"
    with pytest.raises(ValueError):
        Rules(followup_days=[7, 3])
    with pytest.raises(ValueError):
        Rules(template1="{signature.__class__}")
