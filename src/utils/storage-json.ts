/**
 * localStorage JSON 读写的公共底座。
 *
 * 只收口无业务语义的 I/O 与损坏容错；结构清洗、旧格式迁移、容量淘汰和错误通知
 * 仍由各调用方决定，避免把 fail-open / fail-closed 的差异抹平。
 */

/** 安全读取并解析 JSON；缺失、损坏或存储不可用时回落默认值。 */
export function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    return raw ? JSON.parse(raw) as T : fallback
  } catch {
    return fallback
  }
}

/**
 * 序列化并写入 JSON。错误不抛给调用方，由 onError 决定日志、告警或容量淘汰策略。
 */
export function writeJson(key: string, value: unknown, onError?: (error: unknown) => void): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch (error) {
    onError?.(error)
  }
}
