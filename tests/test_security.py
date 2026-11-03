import hashlib
import hmac
from agent_review_engine.security import validate_signature
from agent_review_engine.events import route_event
from agent_review_engine.models import ReviewVerdict
from agent_review_engine.policy import effective_risk, allowed_to_approve, path_risk


def sign(data: bytes) -> str:
    return "sha256=" + hmac.new(b"secret", data, hashlib.sha256).hexdigest()


def test_signature_accepts_only_correct_sha256():
    body = b'{"hello":"world"}'
    assert validate_signature(body, sign(body), "secret")
    assert not validate_signature(body + b" ", sign(body), "secret")
    assert not validate_signature(body, "sha256=" + "f" * 64, "secret")
    assert not validate_signature(body, None, "secret")
    assert not validate_signature(body, "sha256=badhash", "secret")


def base_event(action: str, draft: bool):
    return {"action": action, "repository": {"full_name": "org/my-repo"},
            "installation": {"id": 5}, "number": 12,
            "pull_request": {"number": 12, "draft": draft, "state": "open",
                             "head": {"sha": "a" * 40}}}


def test_pr_trigger_matrix():
    assert [j.kind for j in route_event("pull_request", base_event("opened", True))] == ["label"]
    assert [j.kind for j in route_event("pull_request", base_event("opened", False))] == ["label", "pr"]
    ready = route_event("pull_request", base_event("ready_for_review", False))
    assert [j.kind for j in ready] == ["pr"] and ready[0].force
    assert route_event("pull_request", base_event("ready_for_review", True)) == []
    assert [j.kind for j in route_event("pull_request", base_event("synchronize", False))] == ["label", "pr"]
    assert route_event("pull_request", base_event("labeled", False)) == []
    assert route_event("pull_request", base_event("converted_to_draft", True)) == []
    assert route_event("pull_request", base_event("edited", False)) == []
    edited = base_event("edited", False)
    edited["changes"] = {"title": {"from": "old"}}
    assert [j.kind for j in route_event("pull_request", edited)] == ["label", "pr"]


def test_merge_queue_push_route():
    payload = {"repository": {"full_name": "org/repo"}, "installation": {"id": 1},
               "after": "d" * 40, "ref": "refs/heads/gh-readonly-queue/main/pr-2"}
    jobs = route_event("push", payload)
    assert len(jobs) == 1 and jobs[0].kind == "queue"
    payload["ref"] = "refs/heads/main"
    assert route_event("push", payload) == []


def test_review_bot_does_not_loop():
    payload = base_event("submitted", False)
    payload["review"] = {"user": {"type": "Bot", "login": "review-app[bot]"}}
    assert route_event("pull_request_review", payload) == []
    payload["review"]["user"] = {"type": "User", "login": "alice"}
    result = route_event("pull_request_review", payload)
    assert len(result) == 1 and result[0].force


def test_deterministic_high_risk_overrides_jev_label():
    assert path_risk([".github/workflows/deploy.yml"]) == "high"
    assert path_risk(["service/auth/permissions.py"]) == "high"
    assert path_risk(["helm/service/values.yaml"]) == "high"
    assert effective_risk("low", ["src/auth/permissions.py"]) == "high"
    assert effective_risk(None, ["README.md"]) == "high"
    assert path_risk(["web/components/Button.tsx"]) == "low"
    assert path_risk(["backend/orders.py"]) == "medium"


def test_llm_cannot_auto_approve_blocking_findings():
    v = ReviewVerdict.model_validate({"verdict": "approve", "summary": "ok",
                                      "findings": [{"severity": "high", "path": "src/a.py",
                                                    "explanation": "bug", "evidence": "trace"}]})
    assert not allowed_to_approve(v, "low")[0]
    assert not allowed_to_approve(ReviewVerdict(verdict="approve", summary="ok"), "high")[0]
