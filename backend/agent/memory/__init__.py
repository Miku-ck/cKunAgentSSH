"""v2 记忆子系统包（spec: docs/superpowers/specs/2026-09-22-agent-memory-redesign-design.md）

模块：
  mstore      数据访问（memories/jobs/audit/blocks + FTS/vec 侧表同步）
  retriever   混合检索（FTS BM25 + 向量 RRF 融合 + 断层截断）
  reconcile   对账管线（唯一写入口，ADD/UPDATE/DELETE/NOOP）
  blocks      画像块（常驻上下文）
  embedding   仅真实 API 的向量化客户端（伪向量路线已废除）
  text / vec  切词与向量索引辅助
"""
