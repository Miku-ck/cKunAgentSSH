/** The xterm stylesheet is inlined as a text string by the esbuild `text` loader. */
declare module '@xterm/xterm/css/xterm.css' {
  const cssText: string
  export default cssText
}
