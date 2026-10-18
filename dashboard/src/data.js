export const pages = ['Overview', 'Accounts', 'Message Activity', 'Points & Analytics', 'Groq Usage', 'Errors & Logs', 'Settings']
export async function loadDashboardSnapshot(client, ownerId) {
  const {data, error} = await client.from('dashboard_snapshots').select('payload,received_at')
    .eq('owner_id', ownerId).order('received_at', {ascending:false}).limit(1).maybeSingle()
  if (error) throw error
  if (!data) return null
  if (data.payload?.version !== 1 || !Array.isArray(data.payload.accounts)) throw new Error('Unsupported snapshot format')
  return data.payload
}
export function freshness(data, now = Date.now() / 1000) {
  if (!data?.generated_at) return 'No data'
  if (now - data.generated_at > 90) return 'Offline'
  if (now - data.generated_at > 20) return 'Stale'
  return 'Live'
}
export function summarize(data) {
  const accounts = (data?.accounts || []).filter(a => a.enabled)
  return {
    connected: accounts.filter(a => a.health === 'connected').length,
    today: accounts.reduce((n, a) => n + (a.today || 0), 0),
    total: accounts.reduce((n, a) => n + (a.total || 0), 0),
    remaining: accounts.reduce((n, a) => n + (a.remaining || 0), 0),
    verified: accounts.some(a => a.verified_today != null) ? accounts.reduce((n, a) => n + (a.verified_today || 0), 0) : null,
    events: accounts.flatMap(a => a.events || []).sort((a,b) => b.at - a.at),
  }
}
export function activitySeries(data, field) {
  const result = {}
  for (const a of data?.accounts || []) for (const [key,value] of Object.entries(a[field] || {})) result[key] = (result[key] || 0) + value
  return Object.entries(result).sort(([a],[b]) => a.localeCompare(b)).map(([key,count]) => ({ label: field === 'hourly' ? new Date(Number(key)*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit',timeZone:data.timezone}) : key, count }))
}
export function validatePublicConfig(url, key) {
  if (!url || !key) return false
  if (key.startsWith('sb_secret_')) throw new Error('Use a publishable key, never a secret key')
  try {
    const payload = key.split('.')[1]
    if (payload && JSON.parse(atob(payload.replace(/-/g,'+').replace(/_/g,'/'))).role === 'service_role') throw new Error('Service-role keys are forbidden in the browser')
  } catch (e) { if (e.message.includes('forbidden')) throw e }
  return new URL(url).protocol === 'https:'
}
