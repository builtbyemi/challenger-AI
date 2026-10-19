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
    const data={accounts:[{enabled:true,window:'fixed',resets_at:3600,total,remaining:5-total,today:total}]}
    expect(hourlyDisplay(data,3599).accounts[0].total).toBe(total)
    const expired=hourlyDisplay(data,3600)
    expect(expired.accounts[0].total).toBeNull()
    expect(expired.accounts[0].awaiting_hour).toBe(true)
    expect(expired.accounts[0].today).toBe(total)
    expect(summarize(expired).remaining).toBeNull()
    expect(hourlyDisplay({accounts:[{...data.accounts[0],resets_at:7200,total:0,remaining:5}]},3600).accounts[0].total).toBe(0)
    expect(data.accounts[0].total).toBe(total)
  }
})
