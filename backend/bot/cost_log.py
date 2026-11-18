"""cKunBot 的 LLM usage 记账（O2）。

bot 与运维 Agent 不同：它没有「一次 run」的生命周期边界（陪聊是长驻会话 +
后台主动调度），复用 agent.cost_tracker.CostTracker 的进程内累加会在重启后
归零、也无法按人格归因。这里选择最轻的可观测方案：每次 LLM 调用把 usage
以结构化日志落一行（persona_id / model_id / token 数），日志可被采集聚合。

不接成本金额：bot 用的人格绑定模型多为本地 vLLM，定价未知，报一个猜测的
USD 反而误导。能观测 token 量已足够回答「后台自动烧了多少」。
"""
import logging

logger = logging.getLogger(__name__)


def record_bot_llm_usage(
    persona_id: str,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    scene: str = "chat",
) -> None:
    """记录一次陪聊 LLM 调用的 token 用量。

    scene: chat / ambient / proactive —— 区分被动回复、群顺带、后台主动，
    便于判断无人监督的自动行为烧了多少。
    """
    logger.info(
        "bot.llm.usage scene=%s persona=%s model=%s prompt=%d completion=%d",
        scene, persona_id or "-", model_id or "-",
        max(0, int(prompt_tokens or 0)), max(0, int(completion_tokens or 0)),
    )
