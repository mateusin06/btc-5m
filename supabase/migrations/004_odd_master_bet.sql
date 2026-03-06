-- Aposta fixa (USD) para o modo ODD MASTER (últimos 10s, price-to-beat ±$10, maior odd)
alter table public.user_config add column if not exists odd_master_bet double precision;
