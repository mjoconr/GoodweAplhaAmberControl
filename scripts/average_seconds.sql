with r as (
  select
    id,
    ts_epoch_ms,
    loop,
    lag(ts_epoch_ms) over (order by id) as prev_ts,
    lag(loop) over (order by id) as prev_loop
  from events
  order by id desc
  limit 500
)
select
  round(avg((ts_epoch_ms - prev_ts)/1000.0), 2) as avg_seconds_between_saved,
  round(avg(loop - prev_loop), 1) as avg_loop_gap,
  max(loop - prev_loop) as max_loop_gap
from r
where prev_ts is not null;

