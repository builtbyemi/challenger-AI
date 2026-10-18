-- Run as project administrator. Browser access is read-only and owner-scoped.
create schema if not exists challenger_private;
revoke all on schema challenger_private from public, anon, authenticated;
create table if not exists challenger_private.worker_bindings (
  worker_user_id uuid primary key references auth.users(id) on delete cascade,
  owner_id uuid not null references auth.users(id) on delete cascade,
  worker_id text not null check (length(worker_id) between 1 and 80),
  unique(owner_id, worker_id), check(worker_user_id <> owner_id)
);
revoke all on challenger_private.worker_bindings from public, anon, authenticated;

create table if not exists public.dashboard_snapshots (
  owner_id uuid not null references auth.users(id) on delete cascade,
  worker_id text not null,
  generated_at timestamptz not null,
  received_at timestamptz not null default now(),
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  primary key(owner_id, worker_id)
);
alter table public.dashboard_snapshots enable row level security;
revoke all on public.dashboard_snapshots from public, anon, authenticated;
grant select on public.dashboard_snapshots to authenticated;
drop policy if exists "Owner can read telemetry" on public.dashboard_snapshots;
create policy "Owner can read telemetry" on public.dashboard_snapshots
  for select to authenticated using (owner_id = (select auth.uid()));

create or replace function public.publish_dashboard_snapshot(p_worker text, p_payload jsonb)
returns void language plpgsql security definer set search_path = '' as $$
declare v_owner uuid; v_generated timestamptz;
begin
  select owner_id into v_owner from challenger_private.worker_bindings
    where worker_user_id = auth.uid() and worker_id = p_worker;
  if v_owner is null then raise exception 'Worker not authorized' using errcode='42501'; end if;
  if jsonb_typeof(p_payload) is distinct from 'object' or octet_length(p_payload::text) > 1000000
     or p_payload->>'version' is distinct from '1' or jsonb_typeof(p_payload->'accounts') is distinct from 'array'
     or jsonb_array_length(p_payload->'accounts') > 5
     or not (p_payload ? 'generated_at') then
    raise exception 'Invalid telemetry';
  end if;
  v_generated := to_timestamp((p_payload->>'generated_at')::double precision);
  if v_generated > now() + interval '60 seconds' or v_generated < now() - interval '7 days' then
    raise exception 'Invalid telemetry timestamp';
  end if;
  insert into public.dashboard_snapshots(owner_id,worker_id,generated_at,payload)
    values(v_owner,p_worker,v_generated,p_payload)
    on conflict(owner_id,worker_id) do update
      set generated_at=excluded.generated_at, received_at=now(), payload=excluded.payload
      where dashboard_snapshots.generated_at <= excluded.generated_at;
end $$;
revoke all on function public.publish_dashboard_snapshot(text,jsonb) from public, anon, authenticated;
grant execute on function public.publish_dashboard_snapshot(text,jsonb) to authenticated;

do $$ begin
  if not exists(select 1 from pg_publication_tables where pubname='supabase_realtime'
                and schemaname='public' and tablename='dashboard_snapshots') then
    alter publication supabase_realtime add table public.dashboard_snapshots;
  end if;
end $$;
