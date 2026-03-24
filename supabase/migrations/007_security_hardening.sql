-- Security hardening: tighter RLS + least-privilege grants + atomic admin RPC

-- Ensure RLS is enabled
alter table public.user_config enable row level security;

-- Remove broad grants
revoke all on table public.user_config from anon;
revoke all on table public.user_config from public;

-- Allow only authenticated users to access their own row (RLS enforces ownership)
grant select, insert, update on table public.user_config to authenticated;

drop policy if exists "user_config_select_own" on public.user_config;
create policy "user_config_select_own"
  on public.user_config
  for select
  using (auth.uid() = user_id);

drop policy if exists "user_config_insert_own" on public.user_config;
create policy "user_config_insert_own"
  on public.user_config
  for insert
  with check (auth.uid() = user_id);

drop policy if exists "user_config_update_own" on public.user_config;
create policy "user_config_update_own"
  on public.user_config
  for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Atomic admin helper (service role only)
create or replace function public.grant_subscription_days(p_user_id uuid, p_days int)
returns void
language plpgsql
security definer
as $$
begin
  if auth.role() <> 'service_role' then
    raise exception 'forbidden';
  end if;
  update public.user_config
  set subscription_ends_at = coalesce(subscription_ends_at, now()) + (p_days || ' days')::interval
  where user_id = p_user_id;
end;
$$;
