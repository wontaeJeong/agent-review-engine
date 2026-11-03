from agent_review_engine.config import Settings
from agent_review_engine.events import route_event
from agent_review_engine.models import JobSpec
from agent_review_engine.store import Store


def test_ready_from_draft_and_first_open_schedules_once(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/flow.db")
    obj = {"action": "opened", "repository": {"full_name": "org/repo"},
           "installation": {"id": 12}, "number": 4,
           "pull_request": {"number": 4, "draft": True, "state": "open",
                            "head": {"sha": "a" * 40}}}
    specs = route_event("pull_request", obj)
    assert [s.kind for s in specs] == ["label"]
    assert store.enqueue("1", specs, 0) == 1
    obj["action"], obj["pull_request"]["draft"] = "ready_for_review", False
    specs = route_event("pull_request", obj)
    assert [s.kind for s in specs] == ["pr"]
    assert store.enqueue("2", specs, 0) == 1
    assert store.enqueue("2", specs, 0) == 0


def test_ci_completed_and_queue_push_retries():
    sha = "a" * 40
    obj = {"action": "completed", "repository": {"full_name": "org/repo"},
           "installation": {"id": 1}, "check_run": {"name": "test-light", "head_sha": sha,
           "pull_requests": [{"number": 2, "head": {"sha": sha}}]}}
    jobs = route_event("check_run", obj, light_checks=["test-light"], heavy_checks=["test-heavy"])
    assert len(jobs) == 1 and jobs[0].force and jobs[0].kind == "pr"
    obj["check_run"]["name"] = "agent-review-gate"  # our own check never loops
    assert route_event("check_run", obj, light_checks=["test-light"]) == []
    push = {"action": "", "repository": {"full_name": "org/repo"},
            "installation": {"id": 1}, "ref": "refs/heads/gh-readonly-queue/main/pr-2",
            "after": sha}
    jobs = route_event("push", push)
    assert jobs[0].base_ref == "heads/main"
    assert jobs[0].head_ref == "heads/gh-readonly-queue/main/pr-2"


def test_recheck_resets_cached_verdict(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/db.sqlite")
    spec = JobSpec(kind="pr", repository="org/repo", installation_id=5,
                   pr_number=2, sha="a" * 40, trigger="pull_request.opened")
    store.enqueue("one", [spec], 0)
    first = store.claim()
    assert first is not None
    store.complete(first[0], first[1], {"verdict": {"verdict": "approve"}}, "blocked")
    new_spec = spec.model_copy(update={"trigger": "admin.recheck", "force": True})
    store.enqueue("two", [new_spec], 0)
    result = store.claim()
    assert result is not None and result[3] == {}


def test_default_jev_disabled():
    assert Settings(_env_file=None).jev_enabled is False
