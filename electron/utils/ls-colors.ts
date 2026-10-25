/**
 * LS_COLORS 配置：保留前景颜色（目录/可执行文件等仍有彩色文字），
 * 但去掉所有 ANSI 背景色块，避免终端输出出现彩色长方形背景。
 *
 * 对应类型：
 * - di 目录、ln 符号链接、pi 管道、so socket
 * - bd 块设备、cd 字符设备、or 孤儿链接、mi 缺失文件
 * - su setuid、sg setgid、tw 粘滞位可写、ow 其他用户可写、st 粘滞位
 * - ex 可执行文件
 */
export const LS_COLORS_NO_BACKGROUND =
  'rs=0:di=34:ln=36:mh=00:pi=33:so=35:do=35:bd=33:cd=33:or=31:mi=00:su=31:sg=36:ca=00:tw=32:ow=34:st=34:ex=32'
