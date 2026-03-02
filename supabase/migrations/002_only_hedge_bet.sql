-- Adiciona coluna only_hedge_bet para o modo Only Hedge+
alter table public.user_config add column if not exists only_hedge_bet double precision;
