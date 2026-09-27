export function integer(data: FormData, name: string, minimum: number, maximum = Number.MAX_SAFE_INTEGER): number {
  const input = data.get(name)
  if (typeof input !== 'string' || !input.trim()) throw new Error('Enter a value; unknown values cannot be treated as zero.')
  const value = Number(input)
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) throw new Error('Enter a whole number within range.')
  return value
}
