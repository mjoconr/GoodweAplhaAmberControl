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
  round(avg(((ts_epoch_ms - prev_ts)/1000.0) / (loop - prev_loop)), 4) as sec_per_loop_est
from r
where prev_ts is not null and (loop - prev_loop) > 0;

