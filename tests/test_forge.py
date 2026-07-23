"""forge.py 单测：URL 解析 / 绑定推断 / 纯 shaper / gh 后端（monkeypatch 子进程）。

不打真 gh/git——把 _run 与 _resolve_exe 换成假实现，只验证参数拼装与结果归一。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from feishu_dispatcher import forge


def _proj(repo="", path="/tmp/x"):
    return SimpleNamespace(name="demo", path=Path(path), repo=repo)


# --------------------------- URL 解析 --------------------------- #


def test_parse_https_url():
    assert forge._parse_remote_url("https://github.com/owner/name") == (
        "github.com",
        "owner/name",
    )


def test_parse_https_url_strips_dot_git():
    assert forge._parse_remote_url("https://github.com/owner/name.git") == (
        "github.com",
        "owner/name",
    )


def test_parse_ssh_scp_form():
    assert forge._parse_remote_url("git@github.com:owner/name.git") == (
        "github.com",
        "owner/name",
    )


def test_parse_selfhosted_nested_path():
    assert forge._parse_remote_url("https://git.corp.com/group/sub/proj") == (
        "git.corp.com",
        "group/sub/proj",
    )


def test_parse_rejects_garbage():
    assert forge._parse_remote_url("") is None
    assert forge._parse_remote_url("not-a-url") is None
    assert forge._parse_remote_url("https://github.com/onlyowner") is None  # 无 /


# --------------------------- 绑定推断 --------------------------- #


async def test_resolve_forge_from_config_repo_github():
    ref = await forge.resolve_forge(
        _proj(repo="https://github.com/WSQS/feishu-dispatcher")
    )
    assert ref is not None
    assert ref.kind == "github"
    assert ref.slug == "WSQS/feishu-dispatcher"
    assert ref.host == "github.com"


async def test_resolve_forge_selfhosted_is_gitlab():
    ref = await forge.resolve_forge(_proj(repo="https://git.corp.com/grp/proj"))
    assert ref is not None
    assert ref.kind == "gitlab"
    assert ref.host == "git.corp.com"


async def test_resolve_forge_falls_back_to_git_remote(monkeypatch):
    async def fake_detect(path):
        return "git@github.com:auto/detected.git"

    monkeypatch.setattr(forge, "_detect_remote", fake_detect)
    ref = await forge.resolve_forge(_proj(repo=""))
    assert ref is not None
    assert ref.slug == "auto/detected"
    assert ref.kind == "github"


async def test_resolve_forge_none_when_no_binding(monkeypatch):
    async def fake_detect(path):
        return None

    monkeypatch.setattr(forge, "_detect_remote", fake_detect)
    assert await forge.resolve_forge(_proj(repo="")) is None


# --------------------------- 纯 shaper --------------------------- #


def test_shape_list_item_marks_pr():
    it = {
        "number": 55,
        "title": "feat",
        "state": "open",
        "labels": [{"name": "enhancement"}],
        "updated_at": "2026-07-23T12:00:00Z",
        "html_url": "u",
        "pull_request": {"url": "p"},
    }
    shaped = forge._shape_gh_list_item(it)
    assert shaped["type"] == "pr"
    assert shaped["labels"] == ["enhancement"]
    assert shaped["updated"] == "2026-07-23"


def test_shape_list_item_plain_issue():
    shaped = forge._shape_gh_list_item({"number": 1, "title": "t", "state": "open"})
    assert shaped["type"] == "issue"


def test_summarize_checks_counts_states():
    rollup = [
        {"conclusion": "SUCCESS"},
        {"conclusion": "FAILURE"},
        {"state": "PENDING"},
        {"status": "IN_PROGRESS"},
        {"conclusion": "SKIPPED"},
    ]
    assert forge._summarize_checks(rollup) == {"passed": 2, "failed": 1, "pending": 2}


def test_clip_truncates():
    assert forge._clip("x" * 10, 5) == "xxxxx…"
    assert forge._clip("short", 100) == "short"


def test_shape_detail_pr_includes_pr_fields():
    ref = forge.ForgeRef("github", "o/r", "github.com", "u")
    d = {
        "number": 55,
        "title": "feat",
        "state": "MERGED",
        "author": {"login": "WSQS"},
        "body": "b",
        "labels": [],
        "url": "u",
        "createdAt": "2026-07-20T00:00:00Z",
        "updatedAt": "2026-07-23T00:00:00Z",
        "comments": [],
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        "reviewDecision": "APPROVED",
        "mergeable": "MERGEABLE",
        "additions": 238,
        "deletions": 13,
        "files": [{}, {}, {}],
        "isDraft": False,
    }
    out = forge._shape_gh_detail("pr", ref, d)
    assert out["checks"] == {"passed": 1, "failed": 0, "pending": 0}
    assert out["review_decision"] == "APPROVED"
    assert out["changes"] == {"files": 3, "additions": 238, "deletions": 13}
    assert out["author"] == "WSQS"


def test_shape_detail_issue_has_no_pr_fields():
    ref = forge.ForgeRef("github", "o/r", "github.com", "u")
    out = forge._shape_gh_detail("issue", ref, {"number": 1, "title": "t"})
    assert "checks" not in out
    assert out["kind"] == "issue"


def test_shape_detail_clips_body_and_comments():
    ref = forge.ForgeRef("github", "o/r", "github.com", "u")
    d = {
        "number": 1,
        "body": "x" * 5000,
        "comments": [
            {
                "author": {"login": "a"},
                "body": "c1",
                "createdAt": "2026-01-01T00:00:00Z",
            }
            for _ in range(10)
        ],
    }
    out = forge._shape_gh_detail("issue", ref, d)
    assert out["body"].endswith("…") and len(out["body"]) <= forge._BODY_CLIP + 1
    assert len(out["comments"]) == forge._MAX_COMMENTS  # 只留最近几条


# --------------------------- gh 后端（monkeypatch _run） --------------------------- #


@pytest.fixture
def gh_env(monkeypatch):
    """假装 gh 存在；调用方设置 _run 返回值。"""
    monkeypatch.setattr(forge, "_resolve_exe", lambda name: name)
    calls = []

    def set_run(rc, out, err=""):
        async def fake_run(argv, *, cwd=None, timeout=forge._TIMEOUT):
            calls.append(argv)
            return rc, out, err

        monkeypatch.setattr(forge, "_run", fake_run)

    return SimpleNamespace(calls=calls, set_run=set_run)


async def test_gh_list_parses_and_caps(gh_env):
    items = [{"number": i, "title": f"t{i}", "state": "open"} for i in range(30)]
    gh_env.set_run(0, json.dumps(items))
    ref = forge.ForgeRef("github", "o/r", "github.com", "u")
    result = await forge.list_items(ref, state="open", limit=5)
    assert result["count"] == 5
    assert result["repo"] == "o/r"
    # 用的是统一 issues 端点
    assert gh_env.calls[0][1] == "api"
    assert "repos/o/r/issues" in gh_env.calls[0][2]


async def test_gh_list_missing_gh_raises(monkeypatch):
    monkeypatch.setattr(forge, "_resolve_exe", lambda name: None)
    ref = forge.ForgeRef("github", "o/r", "github.com", "u")
    with pytest.raises(forge.ForgeError, match="gh"):
        await forge.list_items(ref)


async def test_gh_list_surfaces_cli_error(gh_env):
    gh_env.set_run(1, "", "gh: Not Found (HTTP 404)")
    ref = forge.ForgeRef("github", "o/r", "github.com", "u")
    with pytest.raises(forge.ForgeError, match="404"):
        await forge.list_items(ref)


async def test_gh_get_pr_uses_pr_subcommand(gh_env):
    gh_env.set_run(0, json.dumps({"number": 55, "title": "feat", "state": "MERGED"}))
    ref = forge.ForgeRef("github", "o/r", "github.com", "u")
    out = await forge.get_item(ref, "pr", 55)
    assert out["number"] == 55
    assert gh_env.calls[0][1] == "pr"
    assert "--repo" in gh_env.calls[0]


async def test_get_item_rejects_bad_kind():
    ref = forge.ForgeRef("github", "o/r", "github.com", "u")
    with pytest.raises(forge.ForgeError, match="kind"):
        await forge.get_item(ref, "mr", 1)


# --------------------------- GitLab 未实现（1b） --------------------------- #


async def test_gitlab_not_implemented():
    ref = forge.ForgeRef("gitlab", "grp/proj", "git.corp.com", "u")
    with pytest.raises(forge.ForgeError, match="#57"):
        await forge.list_items(ref)
    with pytest.raises(forge.ForgeError, match="#57"):
        await forge.get_item(ref, "issue", 1)
