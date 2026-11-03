# MVP release procedure

The MVP source is on `main` (Python package version 0.1.0). Release tags must be made by a repository owner with a GitHub credential authorized to create tags on commits containing workflow files.

## Tag v0.1.0

1. Check that the most recent `main` validation workflow has succeeded and that the SHA is the intended release.
2. From an authenticated machine:

```bash
git clone https://github.com/wontaeJeong/agent-review-engine.git
cd agent-review-engine
git fetch origin main --tags
git checkout main
git pull --ff-only
git tag -a v0.1.0 HEAD -m 'Agent Review Engine MVP v0.1.0'
git push origin refs/tags/v0.1.0
```

If a tag already exists, **never retag or force-push**. The initial automated tagging attempt was blocked because the GitHub Actions token lacked workflow-write permission. Do not grant broad-lived PAT secrets to an automatically running release job just to bypass this constraint.

The `feat/production-hardening` branch contains version `0.2.0.dev0` and must not be tagged as the MVP.
