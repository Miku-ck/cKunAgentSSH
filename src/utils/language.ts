const extToLang: Record<string, string> = {
  ts: 'typescript', tsx: 'typescript', js: 'javascript', jsx: 'javascript',
  vue: 'html', py: 'python', java: 'java', go: 'go', rs: 'rust',
  c: 'c', cpp: 'cpp', h: 'cpp', hpp: 'cpp', cs: 'csharp', php: 'php',
  rb: 'ruby', swift: 'swift', kt: 'kotlin', scala: 'scala',
  json: 'json', md: 'markdown', css: 'css', scss: 'scss', less: 'less',
  html: 'html', htm: 'html', xml: 'xml', yaml: 'yaml', yml: 'yaml',
  toml: 'ini', ini: 'ini', conf: 'ini', cfg: 'ini', properties: 'ini',
  sh: 'shell', bash: 'shell', zsh: 'shell', fish: 'shell',
  bat: 'bat', cmd: 'bat', ps1: 'powershell',
  sql: 'sql', graphql: 'graphql', dockerfile: 'dockerfile',
  makefile: 'makefile', txt: 'plaintext', log: 'plaintext',
  env: 'plaintext', gitignore: 'plaintext',
}

const specialNames: Record<string, string> = {
  dockerfile: 'dockerfile',
  makefile: 'makefile',
  '.gitignore': 'plaintext',
  '.env': 'plaintext',
}

export function getLanguage(fileName: string): string {
  const lower = fileName.toLowerCase()
  if (specialNames[lower]) return specialNames[lower]
  const ext = lower.split('.').pop() || ''
  return extToLang[ext] || 'plaintext'
}

export function isPreviewable(fileName: string): boolean {
  const lower = fileName.toLowerCase()
  if (specialNames[lower]) return true
  const ext = lower.split('.').pop() || ''
  return ext in extToLang
}

/**
 * 可预览图片扩展名 → MIME 映射（SFTP 图片预览白名单，唯一来源）。
 *
 * 刻意保守：只放浏览器 `<img>` 通用可渲染、且常见于工作场景的四类。svg 虽是 XML
 * 文本，但走 img 标签加载等同于渲染远端内容，与 CSP/审计口径有耦合，本轮不开。
 */
const imageExtToMime: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp',
}

/** 取图片 MIME；非白名单返回 null（调用方按 null 走文本编辑器分支）。 */
export function imageMime(fileName: string): string | null {
  const ext = (fileName.toLowerCase().split('.').pop() || '')
  return imageExtToMime[ext] ?? null
}

/** 判定文件名是否落在图片预览白名单（扩展名大小写不敏感）。 */
export function isImagePreviewable(fileName: string): boolean {
  return imageMime(fileName) !== null
}
