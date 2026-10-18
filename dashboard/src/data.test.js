import { describe, it, expect } from 'vitest'
import { summarize, freshness, activitySeries, validatePublicConfig } from './data'
describe('telemetry semantics',()=>{
  it('does not invent points or activity',()=>{expect(summarize(null).verified).toBeNull();expect(summarize(null).today).toBe(0);expect(freshness(null)).toBe('No data')})
  it('distinguishes stale and offline snapshots',()=>{expect(freshness({generated_at:100},110)).toBe('Live');expect(freshness({generated_at:100},130)).toBe('Stale');expect(freshness({generated_at:100},200)).toBe('Offline')})
  it('aggregates real counts without treating sent messages as points',()=>{const s=summarize({accounts:[{enabled:true,total:3,today:7,remaining:2,verified_today:null}]});expect(s.today).toBe(7);expect(s.remaining).toBe(2);expect(s.verified).toBeNull()})
  it('groups actual account activity',()=>{expect(activitySeries({accounts:[{daily:{'2026-10-18':2}},{daily:{'2026-10-18':3}}]},'daily')).toEqual([{label:'2026-10-18',count:5}])})
  it('rejects browser secrets',()=>{expect(()=>validatePublicConfig('https://test.supabase.co','sb_secret_test')).toThrow();expect(()=>validatePublicConfig('https://test.supabase.co',`a.${btoa(JSON.stringify({role:'service_role'}))}.b`)).toThrow();expect(validatePublicConfig('','')).toBe(false)})
})
