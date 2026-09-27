export interface WorkbenchLink { factoryId: string; caseId: string; taskId: string; error: string }
export function workbenchLink(search: string): WorkbenchLink {
  const parameters = new URLSearchParams(search)
  const keys = ['factory_id', 'case_id', 'task_id']
  const empty = { factoryId: '', caseId: '', taskId: '', error: '' }
  if (keys.some((key) => parameters.getAll(key).length > 1 || (parameters.has(key) && !/^\S{1,160}$/u.test(parameters.get(key) ?? '')))) return { ...empty, error: 'The case link is incomplete; check the original link in the email.' }
  const factoryId = parameters.get('factory_id') ?? '', caseId = parameters.get('case_id') ?? '', taskId = parameters.get('task_id') ?? ''
  if ((caseId && !factoryId) || (taskId && (!factoryId || !caseId))) return { ...empty, error: 'The case link is missing the factory or case; check the original link.' }
  return { factoryId, caseId, taskId, error: '' }
}
