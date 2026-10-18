import {test,expect} from '@playwright/test'
import {execFileSync} from 'node:child_process'
import {readFileSync} from 'node:fs'
import {fileURLToPath} from 'node:url'
import {PGlite} from '@electric-sql/pglite'
const user={id:'11111111-1111-4111-8111-111111111111',email:'owner@example.test',aud:'authenticated',role:'authenticated'}
const token='eyJhbGciOiJIUzI1NiJ9.'+Buffer.from(JSON.stringify({sub:user.id,exp:Math.floor(Date.now()/1000)+3600,role:'authenticated'})).toString('base64url')+'.test'
test('authenticated dashboard, polling refresh, stale state and mobile layout',async({page})=>{
  test.setTimeout(60000)
  const root=fileURLToPath(new URL('../../',import.meta.url))
  const python=fileURLToPath(new URL(process.platform==='win32'?'../../.venv/Scripts/python.exe':'../../.venv/bin/python',import.meta.url))
  const input=JSON.parse(execFileSync(python,['tests/dashboard_fixture.py'],{cwd:root,encoding:'utf8'}))
  const db=new PGlite()
  await db.exec(`create role anon;create role authenticated;create schema auth;create table auth.users(id uuid primary key);
    create function auth.uid() returns uuid language sql stable as $$select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$;
    grant usage on schema auth to authenticated,anon;create publication supabase_realtime;
    insert into auth.users values('${user.id}'),('22222222-2222-4222-8222-222222222222');`)
  await db.exec(readFileSync(fileURLToPath(new URL('../../supabase/migrations/202610180001_dashboard.sql',import.meta.url)),'utf8'))
  await db.query('insert into challenger_private.worker_bindings values($1,$2,$3)',['22222222-2222-4222-8222-222222222222',user.id,'test-worker'])
  await db.exec("select set_config('request.jwt.claim.sub','22222222-2222-4222-8222-222222222222',false);set role authenticated")
  await db.query('select public.publish_dashboard_snapshot($1,$2::jsonb)',[input.p_worker,JSON.stringify(input.p_payload)])
  await db.exec(`reset role;select set_config('request.jwt.claim.sub','${user.id}',false);set role authenticated`)
  let data=(await db.query('select payload from public.dashboard_snapshots')).rows[0].payload, fetches=0
  await db.close()
  const errors=[]
  page.on('pageerror',error=>errors.push(error.message))
  await page.route('https://test.supabase.co/**',async route=>{
    const url=route.request().url()
    if(url.includes('/auth/v1/token')) return route.fulfill({json:{access_token:token,refresh_token:'test-refresh',expires_in:3600,token_type:'bearer',user}})
    if(url.includes('/auth/v1/user'))return route.fulfill({json:user})
    if(url.includes('/rest/v1/dashboard_snapshots')) {fetches++;return route.fulfill({json:{payload:data,received_at:new Date().toISOString()}})}
    return route.fulfill({json:{}})
  })
  await page.goto('/')
  await page.getByLabel('Email',{exact:true}).fill('owner@example.test')
  await page.getByLabel('Password',{exact:true}).fill('test-password')
  await page.getByRole('button',{name:'Open control room'}).click()
  await expect(page.getByRole('heading',{name:'Overview',exact:true})).toBeVisible()
  await expect(page.getByRole('article').filter({hasText:'CONNECTED ACCOUNTS'}).getByText('5 / 5',{exact:true})).toBeVisible()
  await expect(page.getByText('Scoring source not connected')).toBeVisible()
  for(const name of ['Accounts','Message Activity','Points & Analytics','Groq Usage','Errors & Logs','Settings']){
    await page.getByRole('button',{name,exact:true}).click()
    await expect(page.getByRole('heading',{name,exact:true})).toBeVisible()
  }
  data={...data,generated_at:Date.now()/1000-120}
  // Leave refresh untouched: the 15-second fallback must pick up the change.
  await expect(page.getByText('Offline',{exact:true})).toBeVisible({timeout:20000})
  expect(fetches).toBeGreaterThan(1)
  await page.setViewportSize({width:390,height:844})
  await page.getByRole('button',{name:'Open navigation'}).click()
  await page.getByRole('button',{name:'Overview',exact:true}).click()
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
  await page.screenshot({path:'test-results/mobile-dashboard.png',fullPage:true})
  await page.setViewportSize({width:1440,height:1000})
  await page.screenshot({path:'test-results/desktop-dashboard.png',fullPage:true})
  expect(errors).toEqual([])
})
