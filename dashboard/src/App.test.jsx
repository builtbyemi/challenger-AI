import { fireEvent, render, screen, cleanup, waitFor } from '@testing-library/react'
import { afterEach, it, expect, vi } from 'vitest'
import App from './App'
vi.mock('./client',()=>({supabase:null}))
vi.mock('recharts',()=>({ResponsiveContainer:({children})=><div>{children}</div>,AreaChart:()=>null,Area:()=>null,BarChart:()=>null,Bar:()=>null,CartesianGrid:()=>null,XAxis:()=>null,YAxis:()=>null,Tooltip:()=>null}))
afterEach(cleanup)
function fakeClient(payload, error=null) {
  const query={select:vi.fn().mockReturnThis(),eq:vi.fn().mockReturnThis(),order:vi.fn().mockReturnThis(),limit:vi.fn().mockReturnThis(),maybeSingle:vi.fn().mockResolvedValue({data:payload?{payload}:null,error})}
  const channel={on:vi.fn().mockReturnThis(),subscribe:vi.fn().mockReturnThis()}
  return {query,from:vi.fn(()=>query),channel:vi.fn(()=>channel),removeChannel:vi.fn(),auth:{getSession:vi.fn().mockResolvedValue({data:{session:{user:{id:'owner-id'}}}}),onAuthStateChange:vi.fn(()=>({data:{subscription:{unsubscribe:vi.fn()}}}))}}
}
it('loads owner snapshot and displays account, activity, usage and diagnostic data',async()=>{
  const now=Date.now()/1000
  const client=fakeClient({version:1,generated_at:now,timezone:'UTC',group_cap:25,
    accounts:[{account_id:'account_2',enabled:true,health:'connected',today:1,total:1,remaining:4,events:[{id:'event',account_id:'account_2',at:now,origin:'manual',status:'sent',kind:'REPLY',has_reply_target:true}]}],
    groq:{requests_today:17,tokens_today:1234,unreported_today:0,limits:{rpd:1000},daily:{}},
    errors:[{at:now,level:'WARNING',component:'telemetry',code:'upload_deferred'}]})
  render(<App client={client}/>)
  expect(await screen.findByText('connected',{exact:true})).toBeInTheDocument()
  expect(client.query.eq).toHaveBeenCalledWith('owner_id','owner-id')
  fireEvent.click(screen.getByRole('button',{name:'Message Activity',exact:true}))
  expect(screen.getByText('manual',{exact:true})).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'Groq Usage',exact:true}))
  expect(screen.getByText('1234',{exact:true})).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'Errors & Logs',exact:true}))
  expect(screen.getByText('upload deferred')).toBeInTheDocument()
})
it('shows safe sync errors without rendering Supabase exception contents',async()=>{
  render(<App client={fakeClient(null,{message:'private-provider-detail'})}/>)
  await waitFor(()=>expect(screen.getByText(/Sync unavailable/)).toBeInTheDocument())
  expect(screen.queryByText(/private-provider-detail/)).not.toBeInTheDocument()
})
it('shows setup without exposing fabricated data',()=>{render(<App client={null}/>);expect(screen.getByText('Connect your workspace')).toBeInTheDocument();fireEvent.click(screen.getByText(/View empty dashboard/));expect(screen.getByText(/No messages or points are assumed/)).toBeInTheDocument();expect(screen.getByRole('heading',{name:'Account 5'})).toBeInTheDocument()})
it('navigates all pages and switches themes without a connection',()=>{render(<App client={null}/>);fireEvent.click(screen.getByText(/View empty dashboard/));for(const page of ['Accounts','Message Activity','Points & Analytics','Groq Usage','Errors & Logs','Settings']){fireEvent.click(screen.getByRole('button',{name:page,exact:true}));expect(screen.getByRole('heading',{name:page,level:1})).toBeInTheDocument()}fireEvent.click(screen.getByLabelText('Toggle theme'));expect(document.documentElement.dataset.theme).toBe('light')})
