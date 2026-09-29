"""面板实际提交的键 ⇄ client.ts 声明的 payload 类型（round 23）。

round 22 的键契约比的是「client.ts 的类型」与「后端 pydantic 模型」。那两条规则
抓不到本轮的真实事故：`ModelConfigPayload` 里声明了 reasoning_tokens，类型上
「前端会发」，而 ModelsPanel.vue 组装 payload 时从来没写这一行 —— 于是所有模型
被钉死在建表默认 8192 上，两端各自的测试还都是绿的。

所以这里比第三层：面板那个对象字面量**实际**出现的键。
方向是双向相等：
1) 声明了却没发 → 该字段对运维根本不可达（decorative key）；
2) 发了却没声明 → TS 允许而 request() 的载荷与后端对不上，静默丢字段。

与 round 22 同一套告诫：抽取器是代码，会静默返回空集。每条规则都带变异自检。

运行: python -m pytest backend/tests/test_models_panel_payload_round23.py -v
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_settings_key_contract_round22 import CLIENT_TS, _balanced, _keys_of_object  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PANEL_VUE = ROOT / "src" / "components" / "settings" / "panels" / "ModelsPanel.vue"


def _strip_comments(src: str) -> str:
    """先剥 // 行注释与 /* */ 块注释：注释里出现的 `foo:` 会被当成一个键。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _panel_payload_keys(src: str) -> set[str]:
    """ModelsPanel 组装的 payload 键：对象字面量 + 后续 `payload.x = …` 条件赋值。"""
    src = _strip_comments(src)
    m = re.search(r"const\s+payload\s*:\s*ModelConfigPayload\s*=\s*\{", src)
    if not m:
        raise AssertionError("ModelsPanel.vue 里找不到 `const payload: ModelConfigPayload = {`")
    keys = _keys_of_object(_balanced(src, src.index("{", m.end() - 1)))
    # 条件赋值（is_default_ssh 只在创建时带、api_key 只在改动时带）也算面板发出的键
    keys |= set(re.findall(r"\bpayload\.([A-Za-z_][\w]*)\s*=", src))
    return keys


@pytest.fixture(scope="module")
def panel_src():
    return PANEL_VUE.read_text(encoding="utf-8")


def _payload_block(src: str) -> str:
    """client.ts 里 ModelConfigPayload 接口的整块文本（含两端花括号）。"""
    m = re.search(r"export interface ModelConfigPayload\s*\{", src)
    if not m:
        raise AssertionError("client.ts 里找不到 ModelConfigPayload 接口")
    return _balanced(src, src.index("{", m.end() - 1))


@pytest.fixture(scope="module")
def declared_keys():
    return _keys_of_object(_payload_block(CLIENT_TS.read_text(encoding="utf-8")))


def test_extractors_are_not_silently_empty(panel_src, declared_keys):
    """抽取器一旦读空，下面的相等断言就会对「两边都空」成立 —— 比没有测试更糟。"""
    sent = _panel_payload_keys(panel_src)
    assert len(sent) >= 5, f"面板 payload 抽取只拿到 {sorted(sent)}，解析器八成失效了"
    assert len(declared_keys) >= 5, f"payload 类型抽取只拿到 {sorted(declared_keys)}"
    # 本轮修的两项必须真的在里面，否则后面两条规则是空转
    assert {"reasoning_tokens", "top_p"} <= sent


def test_panel_sends_every_declared_payload_key(panel_src, declared_keys):
    never_sent = declared_keys - _panel_payload_keys(panel_src)
    assert not never_sent, (
        f"ModelConfigPayload 声明了但面板从不提交：{sorted(never_sent)} —— "
        "这些字段对运维不可达，库里只有 seed/默认值")


def test_panel_sends_nothing_undeclared(panel_src, declared_keys):
    extra = _panel_payload_keys(panel_src) - declared_keys
    assert not extra, f"面板提交了类型里没声明的键：{sorted(extra)}"


def test_panel_keys_are_accepted_by_backend_create_and_update(panel_src):
    """面板发的键要同时被两个写入口接受（创建走 Create、编辑走 Update）。"""
    sys.path.insert(0, str(ROOT / "backend"))
    from routes import settings as settings_routes

    sent = _panel_payload_keys(panel_src)
    for model in (settings_routes.CreateModelRequest, settings_routes.UpdateModelRequest):
        accepted = set(model.model_fields)
        drifted = sent - accepted
        assert not drifted, f"{model.__name__} 不收这些键，pydantic 会静默丢弃：{sorted(drifted)}"


# ── 变异自检 ────────────────────────────────────────────────────────────────


def test_guard_fails_when_panel_stops_sending_a_key(panel_src):
    """把面板里的 reasoning_tokens 改个名 → 「声明了却没发」那条必须变红并点名它。"""
    mutated = panel_src.replace("reasoning_tokens: form.reasoning_tokens,",
                                "reasoning_tokensX: form.reasoning_tokens,")
    assert mutated != panel_src, "变异没落到源码上，本自检是空跑"
    sent = _panel_payload_keys(mutated)
    assert "reasoning_tokens" not in sent
    assert "reasoning_tokensX" in sent


def test_guard_fails_when_declaration_drops_a_key():
    """反向：把声明里的 top_p 改名，抽取器必须少拿这一个键。

    变异要落在 payload 这一块上：读侧的 `ModelConfig` 里也有一个同名的
    `top_p?: number`，整文件 replace(…, 1) 会先改掉它，本自检就成了空跑。
    """
    block = _payload_block(CLIENT_TS.read_text(encoding="utf-8"))
    mutated = block.replace("  top_p?: number", "  top_pX?: number")
    assert mutated != block, "ModelConfigPayload 里没找到 `top_p?: number`，本自检是空跑"
    keys = _keys_of_object(mutated)
    assert "top_p" not in keys and "top_pX" in keys
