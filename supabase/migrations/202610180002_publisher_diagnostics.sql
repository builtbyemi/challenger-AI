-- Additive, idempotent diagnostic. No changes to bindings, telemetry or RLS.
-- The caller can inspect only whether its OWN publisher binding matches.
create or replace function public.dashboard_publisher_status(p_worker text)
returns jsonb language sql stable security definer set search_path = '' as $$
  select jsonb_build_object(
    'binding_exists', exists(select 1 from challenger_private.worker_bindings
      where worker_user_id = auth.uid()),
    'worker_id_matches', exists(select 1 from challenger_private.worker_bindings
      where worker_user_id = auth.uid() and worker_id = p_worker)
  );
$$;
revoke all on function public.dashboard_publisher_status(text) from public, anon, authenticated;
grant execute on function public.dashboard_publisher_status(text) to authenticated;
notify pgrst, 'reload schema';
