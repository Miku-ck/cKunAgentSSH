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
