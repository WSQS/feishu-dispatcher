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


def test_shape_detail_body_limit_none_keeps_full_body():
    # brief 用途：body_limit=None 不裁剪，取全文（#63）
    ref = forge.ForgeRef("github", "o/r", "github.com", "u")
    out = forge._shape_gh_detail(
        "issue", ref, {"number": 1, "body": "y" * 5000}, body_limit=None
    )
    assert out["body"] == "y" * 5000


def test_clip_none_limit_no_truncation():
    assert forge._clip("z" * 9000, None) == "z" * 9000


# --------------------------- gh 后端（monkeypatch _run） --------------------------- #


@pytest.fixture
def gh_env(monkeypatch):
    """假装 gh 存在；调用方设置 _run 返回值。"""
    monkeypatch.setattr(forge, "_resolve_exe", lambda name: name)
    calls = []

    def set_run(rc, out, err=""):
        async def fake_run(argv, *, cwd=None, env=None, timeout=forge._TIMEOUT):
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


# --------------------------- GitLab 后端（glab，1b/#57） --------------------------- #
#
# mock 单测覆盖命令拼装与形状归一；另已对真实自建 GitLab 实测通过（见 PR）。
# 评论未拉、MR 增删留 0，见 forge._shape_glab_detail 说明。

_GL_REF = forge.ForgeRef("gitlab", "grp/sub/proj", "git.corp.com", "u")


def test_shape_glab_list_item_uses_iid_and_string_labels():
    it = {
        "iid": 5,
        "title": "iss",
        "state": "opened",
        "labels": ["bug", "p1"],  # GitLab 标签是纯字符串数组
        "updated_at": "2026-07-23T00:00:00Z",
        "web_url": "w",
    }
    shaped = forge._shape_glab_list_item(it, "issue")
    assert shaped["number"] == 5  # iid，非 id
    assert shaped["type"] == "issue"
    assert shaped["labels"] == ["bug", "p1"]
    assert shaped["url"] == "w"


def test_shape_glab_detail_mr_maps_pipeline_draft_merge():
    d = {
        "iid": 7,
        "title": "mr",
        "state": "opened",
        "author": {"username": "me"},
        "description": "b",
        "labels": ["feat"],
        "web_url": "w",
        "created_at": "2026-07-20T00:00:00Z",
        "updated_at": "2026-07-22T00:00:00Z",
        "head_pipeline": {"status": "failed"},
        "merge_status": "can_be_merged",
        "draft": True,
        "changes_count": "3",
    }
    out = forge._shape_glab_detail("pr", _GL_REF, d)
    assert out["number"] == 7
    assert out["author"] == "me"  # username，非 login
    assert out["body"] == "b"  # description → body
    assert out["checks"] == {"passed": 0, "failed": 1, "pending": 0}
    assert out["mergeable"] == "can_be_merged"
    assert out["is_draft"] is True
    assert out["changes"]["files"] == 3


def test_shape_glab_detail_issue_has_no_pr_fields():
    out = forge._shape_glab_detail("issue", _GL_REF, {"iid": 5, "title": "t"})
    assert "checks" not in out
    assert out["comments"] == []  # glab MVP 不拉评论


def test_summarize_gitlab_pipeline_variants():
    assert forge._summarize_gitlab_pipeline("success")["passed"] == 1
    assert forge._summarize_gitlab_pipeline("failed")["failed"] == 1
    assert forge._summarize_gitlab_pipeline("running")["pending"] == 1
    assert forge._summarize_gitlab_pipeline(None) == {
        "passed": 0,
        "failed": 0,
        "pending": 0,
    }


def test_glab_changes_count_parses():
    assert forge._glab_changes_count(3) == 3
    assert forge._glab_changes_count("3") == 3
    assert forge._glab_changes_count("3+") == 3
    assert forge._glab_changes_count(None) == 0


async def test_glab_api_passes_host_env(monkeypatch):
    captured = {}

    async def fake_run(argv, *, cwd=None, env=None, timeout=forge._TIMEOUT):
        captured["argv"] = argv
        captured["env"] = env
        return 0, "[]", ""

    monkeypatch.setattr(forge, "_resolve_exe", lambda name: name)
    monkeypatch.setattr(forge, "_run", fake_run)
    await forge._glab_api(_GL_REF, "projects/x/issues")
    assert captured["argv"][:2] == ["glab", "api"]
    assert captured["env"]["GITLAB_HOST"] == "git.corp.com"  # 自建实例定位


async def test_glab_api_missing_glab_raises(monkeypatch):
    monkeypatch.setattr(forge, "_resolve_exe", lambda name: None)
    with pytest.raises(forge.ForgeError, match="glab"):
        await forge._glab_api(_GL_REF, "projects/x/issues")


async def test_glab_list_merges_issues_and_mrs_sorted(monkeypatch):
    async def fake_api(ref, path):
        if "merge_requests" in path:
            return [
                {
                    "iid": 7,
                    "title": "mr",
                    "state": "opened",
                    "updated_at": "2026-07-22T0",
                }
            ]
        return [
            {"iid": 5, "title": "iss", "state": "opened", "updated_at": "2026-07-23T0"}
        ]

    monkeypatch.setattr(forge, "_glab_api", fake_api)
    result = await forge.list_items(_GL_REF, state="open", limit=10)
    types = {(i["type"], i["number"]) for i in result["items"]}
    assert ("issue", 5) in types and ("pr", 7) in types
    # 合并后按 updated 降序：issue(07-23) 排在 MR(07-22) 前
    assert result["items"][0]["number"] == 5


async def test_glab_list_state_open_maps_to_opened(monkeypatch):
    seen = []

    async def fake_api(ref, path):
        seen.append(path)
        return []

    monkeypatch.setattr(forge, "_glab_api", fake_api)
    await forge.list_items(_GL_REF, state="open", limit=10)
    assert all("state=opened" in p for p in seen)
    # slug 里的斜杠被百分号编码
    assert all("grp%2Fsub%2Fproj" in p for p in seen)


async def test_glab_list_state_all_omits_param(monkeypatch):
    seen = []

    async def fake_api(ref, path):
        seen.append(path)
        return []

    monkeypatch.setattr(forge, "_glab_api", fake_api)
    await forge.list_items(_GL_REF, state="all", limit=10)
    assert all("state=" not in p for p in seen)


async def test_glab_get_pr_uses_mr_endpoint(monkeypatch):
    seen = {}

    async def fake_api(ref, path):
        seen["path"] = path
        return {"iid": 7, "title": "mr", "state": "merged"}

    monkeypatch.setattr(forge, "_glab_api", fake_api)
    out = await forge.get_item(_GL_REF, "pr", 7)
    assert "merge_requests/7" in seen["path"]
    assert "grp%2Fsub%2Fproj" in seen["path"]
    assert out["number"] == 7


async def test_glab_get_issue_uses_issue_endpoint(monkeypatch):
    seen = {}

    async def fake_api(ref, path):
        seen["path"] = path
        return {"iid": 5, "title": "iss", "state": "opened"}

    monkeypatch.setattr(forge, "_glab_api", fake_api)
    await forge.get_item(_GL_REF, "issue", 5)
    assert "issues/5" in seen["path"]
