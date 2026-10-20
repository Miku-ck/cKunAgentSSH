"""技能注册表 — 内存缓存 + 按模式/触发词查询"""
import json
import re
import threading
import time
import logging
from typing import Optional

import database as db

logger = logging.getLogger(__name__)

# 预编译 _compile_trigger_pattern 内部使用的两个匹配器（避免每次调用 re.compile）
_WORD_RE = re.compile(r'\w+', re.UNICODE)
_ASCII_RE = re.compile(r'[A-Za-z0-9_]+')

# 触发词 → 编译后 Pattern 的缓存（词表冷热不变时复用，消除重复 re_compile）
_COMPILED_TRIGGER_CACHE: dict[str, re.Pattern] = {}


class SkillRegistry:
    """技能注册表（线程安全，内存缓存）"""

    # 修复：reload 失败后的重试间隔（秒），避免对故障数据库反复查询
    _RETRY_INTERVAL = 5.0

    def __init__(self):
        self._lock = threading.RLock()
        self._skills: list[dict] = []  # 缓存所有技能
        self._loaded = False
        self._last_fail_time: float = 0  # 上次失败时间
        # mode → 最近一次成功读到的「该 mode 下被禁用的技能 id」
        self._disabled_cache: dict[str, set] = {}

    def reload(self, force: bool = False) -> bool:
        """从数据库重新加载所有技能。

        Args:
            force: 写侧显式刷新置 True，绕过节流。用户刚禁用/删除技能时，
                即使上一次 reload 失败（DB 抖动）也必须在本次请求内重读，
                否则新状态要等节流窗口结束才生效，而 _loaded 仍为 True，
                读侧 lazy 自愈也不会触发（S8-04）。

        Returns:
            True = 本次调用真的重读了数据库；False = 节流跳过或读取失败。
        """
        with self._lock:
            # 失败后间隔 _RETRY_INTERVAL 秒再重试，避免对故障数据库反复查询
            if (not force and self._last_fail_time > 0
                    and time.monotonic() - self._last_fail_time < self._RETRY_INTERVAL):
                return False  # 未到重试间隔，跳过（继续服务旧缓存）
            try:
                self._skills = db.list_all_skills()  # 返回所有技能（含 disabled）
                # 性能：预解析 trigger_words JSON，避免 match_skills 每次重复 json.loads
                # （Agent 每轮 LLM 调用都走 match_skills → 技能数 × 触发词数 × json.loads 开销）
                for s in self._skills:
                    tw_raw = s.get('trigger_words', '[]')
                    try:
                        words = json.loads(tw_raw) if isinstance(tw_raw, str) else list(tw_raw)
                    except Exception:
                        words = []
                    s['_trigger_words_parsed'] = words
                self._loaded = True
                self._last_fail_time = 0
                return True
            except Exception as e:
                # 审查 MINOR 修复：失败时保留旧缓存。原实现 `self._skills = []` 把
                # 此前加载成功的技能全部清空，DB 故障期间所有技能注入/触发词匹配
                # 静默丢失，直到下次成功 reload；现在降级为"旧数据"而非"无数据"
                self._last_fail_time = time.monotonic()
                logger.warning("[skill_registry] reload 失败，保留旧缓存（%d 条），%.1fs 后重试: %s",
                               len(self._skills), self._RETRY_INTERVAL, e)
                return False

    def _disabled_ids_for_mode(self, mode: str) -> set:
        """某 mode 下被用户禁用的技能 id（每次真读，失败时沿用最近成功结果）

        读不出来不能当成「没人被禁用」：空集会把用户关掉的技能照常注入、
        load_skill 照常放行，而这道过滤存在的唯一目的就是排除它们。
        与 reload 的「降级为旧数据而非无数据」同一取向。调用方须持锁。
        """
        try:
            ids = set(db.list_disabled_skills_for_mode(mode))
        except Exception as e:
            logger.warning("[skill_registry] 读取 mode=%s 禁用清单失败，沿用上次结果: %s",
                           mode, e)
            return self._disabled_cache.get(mode, set())
        self._disabled_cache[mode] = ids
        return ids

    def get_enabled_skills(self, mode: str) -> list[dict]:
        """获取指定模式下启用的技能，按 priority 降序"""
        if not self._loaded:
            self.reload()
        with self._lock:
            disabled_ids = self._disabled_ids_for_mode(mode)
            return [
                s for s in self._skills
                if s.get('enabled') and s['id'] not in disabled_ids
            ]

    @staticmethod
    def _compile_trigger_pattern(word: str) -> re.Pattern:
        r"""构造触发词匹配正则：
        - 纯 ASCII 字母数字：用 ASCII 断言而不是 `\b`（避免 git 命中 digit）
        - 其余（含空格/标点/中文的短语）：转义后直接 substring 匹配
        返回的 Pattern 仅用于 .search()，大小写无关。

        为什么不能用 `\b`：Python 的 `\w` 含中文，所以 `\b502\b` 在
        "网站返回502错误" 里两边都找不到词边界 → 命中不了。本项目消息几乎都是
        中文夹 ASCII（nginx/502/CPU），用 \b 等于内置技能触发词大面积失效。
        改成只把 ASCII 字母数字下划线视为"相邻字符"，中文边界即可命中。

        性能：结果缓存于模块级 _COMPILED_TRIGGER_CACHE，词表冷热不变时零
        编译开销（原实现每次 match_skills 都重新编译所有触发词）。
        """
        cached = _COMPILED_TRIGGER_CACHE.get(word)
        if cached is not None:
            return cached
        w = word.strip()
        if not w:
            pattern: re.Pattern = re.compile(r'(?!)')  # 永不匹配
        # 含非单词字符（空格、标点、中文等）：转义后直接匹配
        elif not _WORD_RE.fullmatch(w):
            pattern = re.compile(re.escape(w), re.IGNORECASE)
        # 纯 ASCII 字母数字：ASCII 邻接断言，避免短词误命中（如 git 命中 digit），
        # 同时不被中文文本卡住边界
        elif _ASCII_RE.fullmatch(w):
            pattern = re.compile(
                rf'(?<![A-Za-z0-9_]){re.escape(w)}(?![A-Za-z0-9_])', re.IGNORECASE)
        else:
            # 含中文等非 ASCII 单词字符：直接 substring 匹配
            pattern = re.compile(re.escape(w), re.IGNORECASE)
        _COMPILED_TRIGGER_CACHE[word] = pattern
        return pattern

    def match_skills(self, message: str, mode: str, max_results: int = 3) -> list[dict]:
        """按触发词智能匹配技能。

        改进点（相比朴素 substring）:
        1. 词边界感知：纯 ASCII 触发词使用 \\b，避免 `git` 误命中 `digit`/`algorithm`。
        2. 多触发词累积计分：不再命中一个就 break，统计该技能所有触发词的命中次数。
        3. 短语长度加权：长触发短语（如 "create react component"）更特异，权重更高。
        4. 综合排序：score = priority * 10 + Σ(命中权重)，按 score 降序取前 max_results。
        5. 零命中不入选：避免无关联技能污染上下文。
        """
        enabled = self.get_enabled_skills(mode)
        if not enabled or not message:
            return []

        scored: list[tuple[float, dict]] = []
        for skill in enabled:
            trigger_words = skill.get('_trigger_words_parsed')
            if trigger_words is None:
                # reload() 未执行或失败时降级：实时解析（冷路径）
                try:
                    tw_raw = skill.get('trigger_words', '[]')
                    trigger_words = json.loads(tw_raw) if isinstance(tw_raw, str) else list(tw_raw)
                except Exception:
                    trigger_words = []
            if not trigger_words:
                continue

            hits = 0
            weight_sum = 0.0
            for word in trigger_words:
                w = word.strip() if isinstance(word, str) else ''
                if not w:
                    continue
                pattern = self._compile_trigger_pattern(w)
                if pattern.search(message):
                    hits += 1
                    # 短语长度加权：每个非空格字符 +1，最少 1 分；多词短语额外 +2
                    length_bonus = max(1, len(w.replace(' ', '')))
                    phrase_bonus = 2.0 if ' ' in w else 0.0
                    weight_sum += length_bonus + phrase_bonus

            if hits == 0:
                continue

            priority = float(skill.get('priority', 0) or 0)
            score = priority * 10.0 + weight_sum + hits * 0.5
            scored.append((score, skill))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[:max_results]]

    def get_skills_by_ids(self, skill_ids: list[str]) -> list[dict]:
        """按 ID 列表获取技能"""
        if not self._loaded:
            self.reload()
        with self._lock:
            return [s for s in self._skills if s['id'] in skill_ids]

    def get_by_slug(self, slug: str, mode: str | None = None) -> dict | None:
        """按 slug 获取技能，默认同时校验 enabled 与 per-mode 禁用"""
        if not self._loaded:
            self.reload()
        with self._lock:
            skill = next((s for s in self._skills if s.get('slug') == slug), None)
            if not skill or not skill.get('enabled'):
                return None
            if mode:
                if skill['id'] in self._disabled_ids_for_mode(mode):
                    return None
            return skill

    def _enabled_skills_sorted(self, skill_ids: list[str], mode: str) -> list[dict]:
        """按 id 取技能 → 只留本模式已启用的 → 按 priority 降序。

        S8-01 / LEGACY #5：这段前导曾在 build_skill_catalog 与 build_skill_preload
        里逐字重复（含 get_enabled_skills 的第二次禁用清单读取），收敛为一处。
        """
        skills = self.get_skills_by_ids(skill_ids)
        enabled_ids = {s['id'] for s in self.get_enabled_skills(mode)}
        picked = [s for s in skills if s['id'] in enabled_ids]
        picked.sort(key=lambda s: s.get('priority', 0), reverse=True)
        return picked

    def build_skill_catalog(self, skill_ids: list[str], mode: str) -> str:
        """构建 LLM 可见的可用技能目录（不含正文）"""
        skills = self._enabled_skills_sorted(skill_ids, mode)
        if not skills:
            return ""

        lines = [
            "[可用技能目录]",
            "当任务涉及以下某个技能的专长时，请先调用 `load_skill` 工具，传入 `skill_slug` 参数加载完整指令，"
            "然后在后续轮次严格遵循返回内容执行。不要臆测技能内容。",
            "",
        ]
        for idx, s in enumerate(skills, start=1):
            # 复用 reload() 预解析的触发词列表（热路径在 match_skills，此处低频但保持一致性）
            trigger_words = s.get('_trigger_words_parsed')
            if trigger_words is None:
                try:
                    raw = s.get('trigger_words') or '[]'
                    trigger_words = json.loads(raw) if isinstance(raw, str) else list(raw)
                except (ValueError, TypeError):
                    trigger_words = []
            tw_str = ', '.join(trigger_words) if trigger_words else '无'
            lines.append(
                f"{idx}. 技能名：{s.get('name', '')}（slug: {s.get('slug', '')}）\n"
                f"   描述：{s.get('description', '无')}\n"
                f"   触发词：{tw_str}"
            )
        return '\n'.join(lines)

    def build_skill_preload(
        self,
        skill_ids: list[str],
        mode: str,
        max_chars: int = 200,
    ) -> str:
        """为显式选择的技能构建简短预加载提示"""
        skills = self._enabled_skills_sorted(skill_ids, mode)
        if not skills:
            return ""
        lines = ["[已预加载技能提示]", ""]
        for s in skills:
            teaser = (s.get('system_prompt', '') or '')[:max_chars]
            lines.append(
                f"- {s.get('name', '')}（{s.get('slug', '')}）：{s.get('description', '无')}\n"
                f"  开头片段：{teaser}..."
            )
        return '\n'.join(lines)


# 全局单例
_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry
