import {it,expect} from 'vitest'
import {hourlyDisplay,resetLabel,summarize} from './data'

it('shows UTC reset independent of browser timezone and restarts countdown on the hour',()=>{
  const a={window:'fixed'}
  expect(resetLabel(a,3599)).toBe('Resets 01:00 UTC · 0:01')
  expect(resetLabel(a,3600)).toBe('Resets 02:00 UTC · 60:00')
  expect(resetLabel(a,86399)).toBe('Resets 00:00 UTC · 0:01')
})
it('does not carry a partial or full old count into the new UTC hour or invent fresh availability',()=>{
  for(const total of [2,5]) {
    const data={generated_at:3599,accounts:[{enabled:true,window:'fixed',window_start:0,resets_at:3600,total,qualified:total,qualified_remaining:5-total,remaining:5-total,today:total}]}
    expect(hourlyDisplay(data,3599).accounts[0].total).toBe(total)
    const expired=hourlyDisplay(data,3600)
    expect(expired.accounts[0].total).toBeNull()
    expect(expired.accounts[0].qualified).toBeNull()
    expect(expired.accounts[0].qualified_remaining).toBeNull()
    expect(expired.accounts[0].awaiting_hour).toBe(true)
    expect(expired.accounts[0].today).toBe(total)
    expect(summarize(expired).remaining).toBeNull()
    expect(hourlyDisplay({generated_at:3600,accounts:[{...data.accounts[0],window_start:3600,resets_at:7200,total:0,remaining:5}]},3600).accounts[0].total).toBe(0)
    expect(data.accounts[0].total).toBe(total)
  }
})
it('rejects old rolling counters and mislabeled previous-hour snapshots for all five accounts',()=>{
  const accounts=Array.from({length:5},(_,i)=>({account_id:`account_${i+1}`,enabled:true,window:'rolling',total:5,remaining:0}))
  const now=3600+18*60
  const legacy=hourlyDisplay({generated_at:now,accounts},now)
  expect(legacy.accounts.every(a=>a.total===null&&a.awaiting_hour)).toBe(true)
  const mislabeled=hourlyDisplay({generated_at:3599,accounts:accounts.map(a=>({...a,window:'fixed',window_start:3600,resets_at:7200}))},now)
  expect(mislabeled.accounts.every(a=>a.total===null)).toBe(true)
  const current=hourlyDisplay({generated_at:now,accounts:accounts.map(a=>({...a,window:'fixed',window_start:3600,resets_at:7200,total:2,remaining:3}))},now)
  expect(current.accounts.every(a=>a.total===2&&a.remaining===3)).toBe(true)
})
