import type { ReactNode } from 'react'

// Only presentation syntax is interpreted. Model output cannot create HTML, links or images.
function inline(value: string): ReactNode[] {
  return value.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g).map((part, i) =>
    part.startsWith('**') ? <strong key={i}>{part.slice(2, -2)}</strong>
      : part.startsWith('`') ? <code key={i}>{part.slice(1, -1)}</code> : part)
}
const cells = (line: string) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim())

export function AssistantText({ value }: { value: string }) {
  const lines = value.replace(/\r\n/g, '\n').split('\n')
  const blocks: ReactNode[] = []
  for (let i = 0; i < lines.length;) {
    const line = lines[i]!
    if (!line.trim()) { i++; continue }
    const key = i
    if (line.includes('|') && lines[i + 1]?.includes('|') && cells(lines[i + 1]!).every(cell => /^:?-{3,}:?$/.test(cell))) {
      const headers = cells(line)
      const rows: string[][] = []
      i += 2
      while (i < lines.length && lines[i]!.includes('|') && lines[i]!.trim()) rows.push(cells(lines[i++]!))
      blocks.push(<div className="assistant-table" key={key}><table><thead><tr>{headers.map((cell, n) => <th key={n}>{inline(cell)}</th>)}</tr></thead><tbody>{rows.map((row, n) => <tr key={n}>{headers.map((_, c) => <td key={c}>{inline(row[c] ?? '')}</td>)}</tr>)}</tbody></table></div>)
      continue
    }
    if (/^#{1,6}\s/.test(line)) { blocks.push(<h4 key={key}>{inline(line.replace(/^#{1,6}\s+/, ''))}</h4>); i++; continue }
    const ordered = /^\s*\d+[.)]\s+/.test(line)
    const pattern = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*•]\s+/
    if (pattern.test(line)) {
      const items: ReactNode[] = []
      while (i < lines.length && pattern.test(lines[i]!)) items.push(<li key={i}>{inline(lines[i++]!.replace(pattern, ''))}</li>)
      blocks.push(ordered ? <ol key={key}>{items}</ol> : <ul key={key}>{items}</ul>)
      continue
    }
    blocks.push(<p key={key}>{inline(line)}</p>); i++
  }
  return <div className="assistant-text">{blocks}</div>
}
