"""POST /agent/commit-message —— 提交信息生成端点的请求模型与输出清洗。

端点本体是一次无工具 LLM 调用（与 /agent/structure 同管线，真实模型不在测试面内）；
这里钉住两件不依赖模型的事：请求字段的上限约束、_sanitize_commit_message 的
围栏/引号/多行/超长清洗（与 shared/git-repos-core.isValidCommitMsg 同口径）。"""
import pytest
from pydantic import ValidationError

from routes.agent import CommitMessageRequest, _sanitize_commit_message


class TestCommitMessageRequest:
    def test_正常载荷通过(self):
        req = CommitMessageRequest(repo="cKunAgentSSH", branch="main", files=["M src/a.ts", "?? b.log"])
        assert req.repo == "cKunAgentSSH"
        assert req.files == ["M src/a.ts", "?? b.log"]

    def test_diff可选且默认为空(self):
        assert CommitMessageRequest(repo="r", files=["M a"]).diff == ""
        req = CommitMessageRequest(repo="r", files=["M a"], diff="diff --git a/x b/x\n+line")
        assert req.diff.startswith("diff --git")

    def test_diff超长被拒_渲染层clampDiffText先截(self):
        with pytest.raises(ValidationError):
            CommitMessageRequest(repo="r", files=["M a"], diff="x" * 8001)

    def test_空仓库名与空文件清单被拒(self):
        with pytest.raises(ValidationError):
            CommitMessageRequest(repo="", files=["M a"])
        with pytest.raises(ValidationError):
            CommitMessageRequest(repo="r", files=[])

    def test_字段上限_repo_200_branch_256_files_500(self):
        with pytest.raises(ValidationError):
            CommitMessageRequest(repo="x" * 201, files=["M a"])
        with pytest.raises(ValidationError):
            CommitMessageRequest(repo="r", branch="b" * 257, files=["M a"])
        with pytest.raises(ValidationError):
            CommitMessageRequest(repo="r", files=[f"M f{i}" for i in range(501)])


class TestSanitizeCommitMessage:
    def test_纯文本原样保留(self):
        assert _sanitize_commit_message("feat(git): 提交弹窗重排") == "feat(git): 提交弹窗重排"

    def test_剥代码围栏与语言标注(self):
        assert _sanitize_commit_message("```\nfeat: x\n```") == "feat: x"
        assert _sanitize_commit_message("```text\nfix: y\n```") == "fix: y"

    def test_取首行并去引号与空白(self):
        assert _sanitize_commit_message('"fix: crash"\n解释第二行') == "fix: crash"
        assert _sanitize_commit_message("  \n chore: deps  ") == "chore: deps"
        # 中文引号也剥：模型偶尔会整套带上
        assert _sanitize_commit_message("“docs: 更新”") == "docs: 更新"

    def test_超长截断到200(self):
        out = _sanitize_commit_message("feat: " + "很" * 300)
        assert len(out) == 200

    def test_空输入返回空串(self):
        assert _sanitize_commit_message("") == ""
        assert _sanitize_commit_message(None) == ""
