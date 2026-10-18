// @vitest-environment node
import { beforeAll, afterAll, it, expect } from 'vitest'
import { PGlite } from '@electric-sql/pglite'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
let db
const owner='11111111-1111-4111-8111-111111111111', worker='22222222-2222-4222-8222-222222222222', outsider='33333333-3333-4333-8333-333333333333'
const payload={version:1,generated_at:Date.now()/1000,accounts:[]}
async function asUser(id) { await db.exec(`reset role; select set_config('request.jwt.claim.sub','${id}',false); set role authenticated;`) }
beforeAll(async()=>{
  db=new PGlite()
  await db.exec(`create role anon; create role authenticated; create schema auth;
    create table auth.users(id uuid primary key, email text);
    create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
    grant usage on schema auth to authenticated,anon;
    insert into auth.users values('${owner}','owner@example.test'),('${worker}','worker@example.test'),('${outsider}','outsider@example.test');
    create publication supabase_realtime;`)
  const migration=readFileSync(fileURLToPath(new URL('../../supabase/migrations/202610180001_dashboard.sql',import.meta.url)),'utf8')
  await db.exec(migration)
  const diagnostics=readFileSync(fileURLToPath(new URL('../../supabase/migrations/202610180002_publisher_diagnostics.sql',import.meta.url)),'utf8')
  await db.exec(diagnostics)
  await db.exec(diagnostics)
  await db.query('insert into challenger_private.worker_bindings values($1,$2,$3)',[worker,owner,'test-worker'])
},30000)
afterAll(async()=>{await db?.close()})
it('diagnostics reveal only the calling publishers binding and reject anonymous access',async()=>{
  await asUser(worker)
  const check=async id=>(await db.query('select public.dashboard_publisher_status($1) as status',[id])).rows[0].status
  expect(await check('test-worker')).toEqual({binding_exists:true,worker_id_matches:true})
  expect(await check('wrong-worker')).toEqual({binding_exists:true,worker_id_matches:false})
  await asUser(owner)
  expect(await check('test-worker')).toEqual({binding_exists:false,worker_id_matches:false})
  await db.exec('reset role;set role anon')
  await expect(check('test-worker')).rejects.toThrow(/permission denied/)
})
it('worker can publish only its bound snapshot; owner can read it',async()=>{
  await asUser(worker)
  await db.query('select public.publish_dashboard_snapshot($1,$2::jsonb)',['test-worker',JSON.stringify(payload)])
  expect((await db.query('select * from public.dashboard_snapshots')).rows).toHaveLength(0)
  await asUser(owner)
  expect((await db.query('select * from public.dashboard_snapshots')).rows).toHaveLength(1)
  await expect(db.query('select public.publish_dashboard_snapshot($1,$2::jsonb)',['test-worker',JSON.stringify(payload)])).rejects.toThrow(/not authorized/)
})
it('outsider cannot read, insert, or publish another owners telemetry',async()=>{
  await asUser(outsider)
  expect((await db.query('select * from public.dashboard_snapshots')).rows).toHaveLength(0)
  await expect(db.query('insert into public.dashboard_snapshots values($1,$2,now(),now(),$3::jsonb)',[outsider,'evil',JSON.stringify(payload)])).rejects.toThrow(/permission denied/)
  await expect(db.query('select public.publish_dashboard_snapshot($1,$2::jsonb)',['test-worker',JSON.stringify(payload)])).rejects.toThrow(/not authorized/)
})
it('anonymous role cannot read or execute publisher RPC',async()=>{
  await db.exec('reset role;set role anon')
  await expect(db.query('select * from public.dashboard_snapshots')).rejects.toThrow(/permission denied/)
  await expect(db.query('select public.publish_dashboard_snapshot($1,$2::jsonb)',['test-worker',JSON.stringify(payload)])).rejects.toThrow(/permission denied/)
})
it('publisher refuses stale and future timestamps, wrong worker and malformed data',async()=>{
  await asUser(worker)
  for(const value of [{...payload,generated_at:1},{...payload,generated_at:Date.now()/1000+3600},{...payload,accounts:'wrong'}])
    await expect(db.query('select public.publish_dashboard_snapshot($1,$2::jsonb)',['test-worker',JSON.stringify(value)])).rejects.toThrow()
  await expect(db.query('select public.publish_dashboard_snapshot($1,$2::jsonb)',['unknown',JSON.stringify(payload)])).rejects.toThrow(/not authorized/)
})
it('administrator repair corrects a wrong publisher UUID and is idempotent without deleting snapshots',async()=>{
  await db.exec('reset role')
  const before=(await db.query('select count(*)::int as n from public.dashboard_snapshots')).rows[0].n
  await db.query("update challenger_private.worker_bindings set worker_user_id=$1,worker_id='local-worker' where worker_user_id=$2",[outsider,worker])
  const repair=readFileSync(fileURLToPath(new URL('../../supabase/repair_worker_binding.sql',import.meta.url)),'utf8')
    .replace('REPLACE_WITH_WORKER_LOGIN_EMAIL','worker@example.test').replace('REPLACE_WITH_DASHBOARD_OWNER_EMAIL','owner@example.test')
  await db.exec(repair)
  await db.exec(repair)
  expect((await db.query('select worker_user_id from challenger_private.worker_bindings')).rows).toEqual([{worker_user_id:worker}])
  expect((await db.query('select count(*)::int as n from public.dashboard_snapshots')).rows[0].n).toBe(before)
  await asUser(worker)
  await db.query('select public.publish_dashboard_snapshot($1,$2::jsonb)',['local-worker',JSON.stringify(payload)])
  await asUser(owner)
  expect((await db.query("select * from public.dashboard_snapshots where worker_id='local-worker'")).rows).toHaveLength(1)
})
