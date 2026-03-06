-- Chainlink: usar abertura (Price to Beat) e filtro por delta Binance–Chainlink
alter table public.user_config add column if not exists use_chainlink_open boolean default true;
alter table public.user_config add column if not exists max_delta_open_usd double precision default 0;
