-- Which labels each signed-in person follows
create table if not exists public.follows (
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  label_key text not null,
  label_name text not null,
  created_at timestamptz not null default now(),
  primary key (user_id, label_key)
);

alter table public.follows enable row level security;

drop policy if exists "read own follows" on public.follows;
drop policy if exists "add own follows" on public.follows;
drop policy if exists "remove own follows" on public.follows;
create policy "read own follows" on public.follows for select using (auth.uid() = user_id);
create policy "add own follows" on public.follows for insert with check (auth.uid() = user_id);
create policy "remove own follows" on public.follows for delete using (auth.uid() = user_id);

-- For the daily job: which labels does anyone follow? (names and counts only, no user details)
create or replace function public.followed_labels()
returns table (lkey text, lname text, followers bigint)
language sql
security definer
set search_path = public
as $$
  select label_key, min(label_name), count(*) from public.follows group by label_key
$$;

revoke all on function public.followed_labels() from public;
grant execute on function public.followed_labels() to anon, authenticated;

-- Basket and wishlist: one row per saved record, each person sees only their own
create table if not exists public.saved_items (
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  item_key text not null,
  list text not null check (list in ('basket', 'wishlist')),
  title text,
  created_at timestamptz not null default now(),
  primary key (user_id, item_key)
);

alter table public.saved_items enable row level security;

drop policy if exists "read own saved" on public.saved_items;
drop policy if exists "add own saved" on public.saved_items;
drop policy if exists "change own saved" on public.saved_items;
drop policy if exists "remove own saved" on public.saved_items;
create policy "read own saved" on public.saved_items for select using (auth.uid() = user_id);
create policy "add own saved" on public.saved_items for insert with check (auth.uid() = user_id);
create policy "change own saved" on public.saved_items for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "remove own saved" on public.saved_items for delete using (auth.uid() = user_id);
