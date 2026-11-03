from agent_review_engine.config import Settings
from agent_review_engine.engine import ReviewEngine, RetryLater
from agent_review_engine.models import Labels, JobSpec, ReviewVerdict
from agent_review_engine.store import Store


class FakeGithub:
    def __init__(self, risk="low", path="src/a.py"):
        self.labels = []
        self.path = path
        self.reviews_posted = []
        self.checks_posted = []
        self.ci_result = "success"
        self.current_sha = "a" * 40
        self.draft = False
        self.human_approved = False
        self.mutate_after_ci = False
        self.ci_names = []

    async def pr(self, iid, repo, number):
        return {"head": {"sha": self.current_sha}, "state": "open", "draft": self.draft,
                "title": "Fix issue", "body": "Implements change", "user": {"login": "developer"}}

    async def files(self, iid, repo, number):
        return [{"filename": self.path, "patch": "@@ -1 +1 @@\n-old\n+new", "changes": 2}]

    async def replace_review_labels(self, iid, repo, number, labels):
        self.labels = labels

    async def label_names(self, iid, repo, number):
        return self.labels

    async def ci_state(self, iid, repo, sha, names):
        self.ci_names.append(names)
        return self.ci_result, "CI result: " + self.ci_result

    async def check(self, iid, repo, sha, status, title, summary, check_id=None):
        self.checks_posted.append((sha, status, title))
        return 100

    async def reviews(self, iid, repo, number):
        return []

    async def submit_review(self, iid, repo, number, sha, event, body):
        self.reviews_posted.append((sha, event, body))

    async def human_approval(self, iid, repo, number, sha, author):
        return self.human_approved

    async def commit(self, iid, repo, sha):
        return {"parents": [{"sha": "b" * 40}]}

    async def compare_files(self, iid, repo, base, sha):
        return await self.files(iid, repo, 1)


class FakeJev:
    async def classify(self, *, title, description, files):
        return Labels(risk="low", area="backend", confidence=0.95)


class FakeLLM:
    def __init__(self, verdict="approve", github=None):
        self.called = 0
        self.verdict = verdict
        self.github = github

    async def review(self, **kwargs):
        self.called += 1
        if self.github and self.github.mutate_after_ci:
            self.github.current_sha = "c" * 40
        return ReviewVerdict(verdict=self.verdict, summary="No issue detected"), \
            {"prompt_tokens": 100, "completion_tokens": 25}


def make_engine(tmp_path, *, mode="approve", path="src/a.py"):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/jobs.db", approval_mode=mode,
                        jev_enabled=True, light_ci_checks="test-light", heavy_ci_checks="test-heavy")
    store = Store(settings.database_url)
    gh = FakeGithub(path=path)
    llm = FakeLLM(github=gh)
    eng = ReviewEngine(settings, store, gh, FakeJev(), llm)
    pr_spec = JobSpec(kind="pr", repository="org/repo", installation_id=1,
                      sha="a" * 40, pr_number=12, trigger="pull_request.ready_for_review")
    label_spec = pr_spec.model_copy(update={"kind": "label"})
    return eng, gh, llm, pr_spec, label_spec


async def test_label_then_approve_after_light_ci(tmp_path):
    eng, gh, llm, pr, label = make_engine(tmp_path)
    status, state = await eng.execute(label, {})
    assert status == "done" and state["risk"] == "medium"
    eng.store.enqueue("webhook1", [label], 0)
    eng.store.complete(label.key, 0, state)
    status, state = await eng.execute(pr, {})
    assert status == "done"
    assert llm.called == 1
    assert len(gh.reviews_posted) == 1 and gh.reviews_posted[0][1] == "APPROVE"
    assert gh.checks_posted[-1][1] == "success"
    assert gh.ci_names == [["test-light"]]


async def test_ci_pending_never_calls_llm(tmp_path):
    eng, gh, llm, pr, label = make_engine(tmp_path)
    _, classified = await eng.execute(label, {})
    eng.store.enqueue("w", [label], 0)
    eng.store.complete(label.key, 0, classified)
    gh.ci_result = "pending"
    try:
        await eng.execute(pr, {})
        assert False, "Expected RetryLater"
    except RetryLater:
        pass
    assert llm.called == 0
    assert not gh.reviews_posted
    assert gh.checks_posted[-1][1] == "in_progress"


async def test_high_risk_requires_human(tmp_path):
    eng, gh, llm, pr, label = make_engine(tmp_path, path=".github/workflows/deploy.yml")
    _, classified = await eng.execute(label, {})
    assert classified["risk"] == "high"
    eng.store.enqueue("w", [label], 0)
    eng.store.complete(label.key, 0, classified)
    status, state = await eng.execute(pr, {})
    assert status == "blocked" and gh.checks_posted[-1][1] == "action_required"
    assert not any(x[1] == "APPROVE" for x in gh.reviews_posted)
    gh.human_approved = True
    status, state = await eng.execute(pr, state)
    assert status == "done" and gh.checks_posted[-1][1] == "success"
    assert llm.called == 1  # cached verdict for human follow-up


async def test_stale_sha_cannot_be_approved(tmp_path):
    eng, gh, llm, pr, label = make_engine(tmp_path)
    _, classified = await eng.execute(label, {})
    eng.store.enqueue("w", [label], 0)
    eng.store.complete(label.key, 0, classified)
    gh.mutate_after_ci = True
    status, state = await eng.execute(pr, {})
    assert status == "stale"
    assert not gh.reviews_posted
    assert not gh.checks_posted


async def test_report_only_never_approves_and_fails_closed(tmp_path):
    eng, gh, llm, pr, label = make_engine(tmp_path, mode="report_only")
    _, classified = await eng.execute(label, {})
    eng.store.enqueue("w", [label], 0)
    eng.store.complete(label.key, 0, classified)
    status, state = await eng.execute(pr, {})
    assert status == "done" and not gh.reviews_posted
    assert gh.checks_posted[-1][1] == "action_required"


async def test_queue_requires_heavy_ci_and_posts_on_group_sha(tmp_path):
    eng, gh, llm, _, _ = make_engine(tmp_path)
    spec = JobSpec(kind="queue", repository="org/repo", installation_id=1,
                   sha="d" * 40, base_sha="b" * 40, trigger="push.event")
    status, state = await eng.execute(spec, {})
    assert status == "done"
    assert llm.called == 1
    assert gh.ci_names == [["test-heavy"]]
    assert gh.checks_posted[-1][0] == "d" * 40
    assert gh.checks_posted[-1][1] == "success"
    assert not gh.reviews_posted


async def test_untrusted_label_does_not_override_classification_record(tmp_path):
    eng, gh, llm, pr, label = make_engine(tmp_path, path=".github/workflows/deploy.yml")
    _, classified = await eng.execute(label, {})
    eng.store.enqueue("w", [label], 0)
    eng.store.complete(label.key, 0, classified)
    gh.labels = ["risk:low", "area:docs"]  # tampered by repo collaborator
    status, state = await eng.execute(pr, {})
    assert status == "blocked" and state["risk"] == "high"


async def test_disabled_jev_uses_fallback_and_does_not_block_review(tmp_path):
    class UnavailableJev:
        async def classify(self, **kwargs):
            raise AssertionError("Disabled Jev must not be invoked")

    eng, gh, llm, pr, label = make_engine(tmp_path)
    eng.settings.jev_enabled = False
    eng.classifier = UnavailableJev()
    status, labels = await eng.execute(label, {})
    assert status == "done" and labels["jev_status"] == "disabled"
    assert "review:jev-degraded" not in gh.labels
    status, result = await eng.execute(pr, {})
    assert status == "done"
    assert llm.called == 1 and gh.reviews_posted[0][1] == "APPROVE"


async def test_enabled_broken_jev_gracefully_degrades(tmp_path):
    class BadJev:
        async def classify(self, **kwargs):
            raise ValueError("Local endpoint is incomplete")

    eng, gh, llm, pr, label = make_engine(tmp_path)
    eng.classifier = BadJev()
    status, labels = await eng.execute(label, {})
    assert status == "done"
    assert labels["jev_status"] == "degraded"
    assert "review:jev-degraded" in gh.labels
    # LLM approval never waits for the optional classifier.
    assert (await eng.execute(pr, {}))[0] == "done"
    assert gh.reviews_posted[0][1] == "APPROVE"


async def test_repo_override_jev_disable_and_queue_flag(tmp_path):
    class BadJev:
        async def classify(self, **kwargs):
            raise AssertionError("Repo override should bypass Jev")

    eng, gh, llm, pr, label = make_engine(tmp_path)
    eng.store.update_repo_settings("org/repo", {"jev_enabled": False, "auto_enqueue": True})
    eng.classifier = BadJev()
    gh.enqueued = []

    async def enqueue(iid, node_id, sha):
        gh.enqueued.append((node_id, sha))

    gh.enqueue_pull_request = enqueue
    orig_pr = gh.pr

    async def with_node(iid, repo, number):
        result = await orig_pr(iid, repo, number)
        result["node_id"] = "PR_123"
        return result

    gh.pr = with_node
    label_result = await eng.execute(label, {})
    assert label_result[1]["jev_status"] == "disabled"
    status, state = await eng.execute(pr, {})
    assert status == "done" and state["enqueue_status"] == "enqueued"
    assert gh.enqueued == [("PR_123", "a" * 40)]


async def test_fork_requires_authorized_human(tmp_path):
    eng, gh, llm, pr, label = make_engine(tmp_path)
    orig_pr = gh.pr

    async def fork_pr(iid, repo, number):
        result = await orig_pr(iid, repo, number)
        result["head"]["repo"] = {"full_name": "outsider/repo"}
        return result

    gh.pr = fork_pr
    status, state = await eng.execute(pr, {})
    assert status == "blocked" and state["risk"] == "high"
    assert not any(event == "APPROVE" for _, event, _ in gh.reviews_posted)


async def test_queue_stale_head_does_not_publish_success(tmp_path):
    eng, gh, llm, _, _ = make_engine(tmp_path)

    async def ref_sha(iid, repo, ref):
        return "f" * 40

    gh.ref_sha = ref_sha
    spec = JobSpec(kind="queue", repository="org/repo", installation_id=1,
                   sha="d" * 40, base_sha="b" * 40,
                   head_ref="heads/gh-readonly-queue/main/pr-1", trigger="bridge.merge_group")
    status, _ = await eng.execute(spec, {})
    assert status == "stale" and not gh.checks_posted and llm.called == 0
