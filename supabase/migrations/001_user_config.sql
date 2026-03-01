-- Polymarket Bot Dashboard: tabela de config por usuário e RLS
-- Execute este SQL no Supabase: SQL Editor > New query > Cole e Run

-- Tabela: uma linha por usuário (auth.uid())
create table if not exists public.user_config (
  user_id uuid primary key references auth.users(id) on delete cascade,
  email text,
  -- Polymarket
  private_key text,
  funder_address text,
  api_key text,
  api_secret text,
  api_passphrase text,
  signature_type int default 1,
  -- Bot
  starting_bankroll double precision default 10,
  min_bet double precision default 5,
  bot_mode text default 'safe',
  aggressive_bet_pct double precision default 25,
  max_token_price double precision default 0.9,
  arb_min_profit_pct double precision default 0.04,
  safe_bet double precision,
  arbitragem_pct double precision,
  --
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- RLS: usuário só acessa a própria linha
alter table public.user_config enable row level security;

drop policy if exists "user_config_select_own" on public.user_config;
create policy "user_config_select_own" on public.user_config
  for select using (auth.uid() = user_id);

drop policy if exists "user_config_insert_own" on public.user_config;
create policy "user_config_insert_own" on public.user_config
  for insert with check (auth.uid() = user_id);

drop policy if exists "user_config_update_own" on public.user_config;
create policy "user_config_update_own" on public.user_config
  for update using (auth.uid() = user_id);

-- Trigger para updated_at
create or replace function public.set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists user_config_updated_at on public.user_config;
create trigger user_config_updated_at
  before update on public.user_config
  for each row execute function public.set_updated_at();

comment on table public.user_config is 'Config e credenciais Polymarket por usuário (dashboard bot)';
