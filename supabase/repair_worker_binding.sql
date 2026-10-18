-- Run ONLY as administrator in the SAME project used by the worker/dashboard.
-- Replace the two email placeholders. Never put passwords or API keys here.
-- Re-running with the same values is safe. No snapshots or account data deleted.
begin;
do $$
declare
  v_worker_email text := 'emmaanthony08120746423@gmail.com';
  v_owner_email text := 'emmanuel.cryptohub01@gmail.com';
  v_worker uuid;
  v_owner uuid;
begin
  select id into strict v_worker from auth.users where lower(email) = lower(v_worker_email);
  select id into strict v_owner from auth.users where lower(email) = lower(v_owner_email);
  if v_worker = v_owner then
    raise exception 'Owner and publisher must be different users';
  end if;
  if exists(select 1 from challenger_private.worker_bindings
      where worker_user_id = v_worker and owner_id <> v_owner) then
    raise exception 'Publisher already belongs to another owner. Review before reassigning it.';
  end if;
  if exists(select 1 from challenger_private.worker_bindings
      where owner_id = v_owner and worker_id = 'local-worker' and worker_user_id <> v_worker) then
    if exists(select 1 from challenger_private.worker_bindings where worker_user_id = v_worker) then
      raise exception 'Two conflicting publisher bindings exist. Review them before merging.';
    end if;
    update challenger_private.worker_bindings set worker_user_id = v_worker
      where owner_id = v_owner and worker_id = 'local-worker';
  else
    insert into challenger_private.worker_bindings(worker_user_id, owner_id, worker_id)
      values(v_worker, v_owner, 'local-worker')
      on conflict(worker_user_id) do update set worker_id = excluded.worker_id;
  end if;
end $$;
commit;
